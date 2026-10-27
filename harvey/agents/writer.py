"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import json
import logging
import re
from datetime import datetime, timezone

from harvey.brain import SKILLS_DIR, Brain
from harvey.config import HarveyConfig
from harvey.humanize import humanize, humanizer_enabled
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.models.campaign import Campaign, EmailStep
from harvey.state import FREE_MAIL_DOMAINS, WRITABLE_EMAIL_STATUSES, StateManager
from harvey.sending_mode import sync_sending_mode
from harvey.collectors.discover import clean_industry
from harvey.language import FRENCH_RULES, company_language
from harvey.startups import hiring_facts, startup_facts
from harvey.textio import read_user_text
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.writer")

# writer.first_email: offer. Overrides rules 11 and 15 of prompts/writer.md
# for the first email, the way a service is sold: who is writing, and why them.
OFFER_FIRST_EMAIL = """FIRST EMAIL: OFFER (overrides rules 6, 11 and 15 for the first email).
Four short sentences at most, in this order:
1. The trigger: one observable fact from the FACTS that shows you looked
   ("you've been hiring a CTO since September", "you announced a raise in August").
2. The problem that fact implies, in the reader's words, not yours. A
   non-technical founder doesn't think in frameworks; they think "I can't tell
   whether what my agency bills me is right". Name a technology only when the
   reader is technical (a CTO or a tech lead).
3. Half a sentence on who you are, from the product info. Not a CV: no years
   of experience, no list of clients.
4. One question that asks for a reply, not a meeting, with an easy way to say
   no ("worth a quick exchange, or not a topic for you right now?"). Never ask
   for a call, a visio or an amount of time in this email.
Pick the angle that fits their situation. When the product info lists offers by
situation, use the one the FACTS point to. Never offer them something they are
past: a CTO to a company that has one, a first version to a company that has
raised a round or has a product on the market.
It must fit on a phone screen: under 60 words. Never start with yourself."""

# writer.first_email: offer. Follow-ups that each bring something new.
OFFER_FOLLOW_UPS = (
    "- Email 2: 4 days later. Something new: a short case or proof point from the product\n"
    "  info, or a second observation. Never \"just following up\" or a recap of email 1.\n"
    "  Under 60 words, one question that asks for a reply.\n"
    "- Email 3: 6 days after that. Under 40 words. An easy way out (\"no worries if it's\n"
    "  not a topic\"), no guilt, no \"one last time\"."
)

# writer.critique: the review call's rubric, best first.
CRITIQUE_RUBRIC = """1. Specific: built on one real fact from the FACTS that matters to them now.
2. Relevant: what is offered or asked follows from that fact.
3. Respectful: never implies they are failing or can't do their job.
4. Clear: a stranger understands in five seconds who writes and why.
5. Easy to answer: one question they can reply to in one line.
6. Human: reads like a person typed it; follows every STRICT EMAIL RULE."""
VARIANTS = 3
# Emails the operator wrote or likes, in their own words: skills/voice_samples.md.
VOICE_SKILL = "voice_samples"

# How many times the model may fail to write a usable sequence for a
# prospect before Harvey stops trying (status 'write_failed'). A provider
# outage never counts; only a reply that came back unusable does.
MAX_WRITE_ATTEMPTS = 3
WRITE_ATTEMPTS_KEY = "write_attempts:"

# Merge variables that name a person; a company inbox has none of them.
PERSON_VAR = re.compile(r"\{\{\s*(first_?name|last_?name|title)\s*\}\}", re.IGNORECASE)

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in WRITABLE_EMAIL_STATUSES
        ]
        # Someone at their company already replied: no cold email to write.
        prospects_with_email = [
            p for p in prospects_with_email
            if not await self.state.colleague_who_replied(p.id)
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Only so many people per company at a time (contacts_per_company).
        cap = int(getattr(self.config.channels.email, "contacts_per_company", 0) or 0)
        if cap:
            prospects_with_email = await self._within_company_cap(prospects_with_email, cap)
            if not prospects_with_email:
                logger.info("Writer: every company with a new prospect already has its contacts in outreach.")
                return

        # The language each one is written to in: French to French companies.
        languages = {p.id: await self._language(p) for p in prospects_with_email}

        # Batch prospects into campaign groups (by industry/title for relevance,
        # and by language: one template goes to the whole batch)
        batches = self._group_prospects(prospects_with_email, languages)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            language = languages.get(prospects[0].id, "en")
            self._no_reply = False
            sequence = await self._write_sequence(prospects, language)
            if not sequence:
                paused = getattr(self.brain, "paused_reason", "") or (
                    "no reply from the model" if self._no_reply else "")
                if paused:
                    # The provider failed (outage, logout, rate limit,
                    # timeout), not the draft: no attempt is counted, and the
                    # other batches would fail the same way.
                    logger.warning(f"Writer: {paused}; leaving '{batch_name}' for later.")
                    return
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                await self._count_failed_attempt(batch_name, prospects)
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.queue_prospect_if_unsent(p.id)
                await self.state.delete_setting(WRITE_ATTEMPTS_KEY + p.id)

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects, language)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    async def _count_failed_attempt(self, batch_name: str, prospects: list):
        """Count one unusable sequence against each prospect in the batch.

        A prospect left 'new' makes the heartbeat choose write_campaign every
        cycle, so after MAX_WRITE_ATTEMPTS it moves to 'write_failed': visible
        in status and the dashboard, and out of the way of prospecting.
        """
        given_up = []
        for p in prospects:
            key = WRITE_ATTEMPTS_KEY + p.id
            try:
                attempts = int(await self.state.get_setting(key) or 0) + 1
            except ValueError:
                attempts = 1
            if attempts >= MAX_WRITE_ATTEMPTS:
                await self.state.update_prospect_status(p.id, "write_failed")
                await self.state.delete_setting(key)
                given_up.append(p.email)
            else:
                await self.state.set_setting(key, str(attempts))
        if given_up:
            await self.state.log_action(
                action_type="write_failed",
                agent="writer",
                details={"campaign_name": batch_name, "prospects": given_up,
                         "attempts": MAX_WRITE_ATTEMPTS},
            )
            logger.warning(
                f"Writer: Gave up on {len(given_up)} prospect(s) in '{batch_name}' after "
                f"{MAX_WRITE_ATTEMPTS} unusable drafts; marked write_failed."
            )

    def _writer_setting(self, name: str, default):
        return getattr(getattr(self.config, "writer", None), name, default)

    def _style_blocks(self) -> str:
        """The offer rule (writer.first_email: offer) and the operator's own
        emails (skills/voice_samples.md), when there are any."""
        blocks = []
        if self._writer_setting("first_email", "observation") == "offer":
            blocks.append(OFFER_FIRST_EMAIL)
        samples = self._voice_samples()
        if samples:
            blocks.append(
                "VOICE SAMPLES: emails the sender wrote or likes. Match their tone, "
                "rhythm, length and how they open and close. Never reuse their facts, "
                "names, numbers or claims; the FACTS below are the only facts.\n" + samples)
        return "".join(f"\n\n{b}" for b in blocks)

    def _voice_samples(self) -> str:
        """skills/voice_samples.md without its comments; "" when there is no
        such file or it holds no email yet."""
        path = SKILLS_DIR / f"{VOICE_SKILL}.md"
        try:
            text = read_user_text(path) if path.exists() else ""
        except OSError:
            text = ""
        text = re.sub(r"(?s)<!--.*?-->", "", text).strip()
        return text if len(text.split()) >= 15 else ""

    async def _pick_best(self, facts: list[str], variants: list[dict]) -> dict:
        """Ask a separate review call which draft is best against the rubric.
        Fails open to the first draft."""
        if len(variants) < 2:
            return variants[0]
        drafts = "\n\n".join(f"Draft {i + 1}\nSubject: {v['subject']}\n{v['body']}"
                              for i, v in enumerate(variants))
        prompt = f"""You review cold emails before they are sent. Pick the one draft that
best meets this rubric, in order of importance:
{CRITIQUE_RUBRIC}

What is known about the recipient (the only facts a draft may use):
{fence(chr(10).join(facts), "prospect research")}

The drafts:
{fence(drafts, "drafts")}

{UNTRUSTED_RULE}

Return ONLY JSON: {{"best": <draft number>, "why": "<one sentence>"}}"""
        try:
            verdict = await self.brain.think_json(prompt, session_id="harvey-writer",
                                                  agent="writer", task="critique")
            best = int(verdict.get("best")) - 1 if isinstance(verdict, dict) else -1
        except (TypeError, ValueError, AttributeError):
            best = -1
        if 0 <= best < len(variants):
            logger.info(f"Writer: kept draft {best + 1} of {len(variants)}: "
                        f"{str(verdict.get('why') or '')[:160]}")
            return variants[best]
        return variants[0]

    async def _within_company_cap(self, prospects: list, cap: int) -> list:
        """At most ``cap`` people per company in cold outreach, counting those
        already queued or written to; the most senior, then best scored,
        first. The others stay new for later."""
        def company_of(p):
            domain = (p.email or "").rpartition("@")[2].lower()
            return p.company_id or (domain if domain and domain not in FREE_MAIL_DOMAINS else p.id)

        taken: dict[str, int] = {}
        kept = []
        for p in sorted(prospects, key=lambda p: (p.seniority != "c_suite", -(p.score or 0))):
            key = company_of(p)
            if key not in taken:
                taken[key] = await self.state.colleagues_in_outreach(p.id) if p.id else 0
            if taken[key] >= cap:
                continue
            taken[key] += 1
            kept.append(p)
        return kept

    async def _language(self, prospect) -> str:
        """"fr" or "en" for this prospect's company (see harvey.language)."""
        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        domain = (getattr(company, "domain", "") or (prospect.email or "").rpartition("@")[2])
        return await company_language(self.state, self.config, prospect.company_id or "",
                                      domain=domain, location=getattr(company, "location", "") or "",
                                      description=getattr(company, "description", "") or "")

    async def _personalize_first_emails(self, campaign_id: str, prospects: list,
                                        language: str = "en"):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        pending = []
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect, language)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if draft:
                pending.append((prospect, draft))

        # One Humanizer pass over the whole batch before anything is staged.
        polished = await humanize(
            self.brain, [d for _, d in pending], agent="writer",
            enabled=humanizer_enabled(self.config),
        )

        drafted = 0
        for (prospect, _), draft in zip(pending, polished):
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status=status,
                provider=provider,
                approved_by="" if require_approval else "autopilot",
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _startup(self, company_id) -> list[str] | None:
        """Fact lines when the company is a startup (YC, another accelerator,
        a raise it announced; see harvey.startups), else None."""
        return await startup_facts(getattr(self, "state", None), company_id)

    def _startup_playbook(self) -> str:
        """skills/selling_to_startups.md, for startup prospects ("" if absent)."""
        text = self.brain.load_skill("selling_to_startups") if hasattr(self.brain, "load_skill") else ""
        return "\n\nSELLING TO STARTUP FOUNDERS\n" + text if text else ""

    async def _feedback_block(self, prospect_ids: list[str]) -> str:
        """The owner's notes from the dashboard's Feedback buttons, as prompt
        text ("" when there are none). Never lets a failed read stop writing."""
        try:
            notes = await self.state.get_writing_feedback(prospect_ids)
        except Exception as e:
            logger.warning(f"Writer: could not read feedback: {e}")
            return ""
        if not notes:
            return ""
        lines = "\n".join(
            f"- {str(n.get('created_at') or '')[:10]} on {n['about']}: "
            f"{' '.join(str(n['comment']).split())[:500]}" for n in notes)
        return (
            "\n\nOWNER FEEDBACK — notes the person running Harvey left on earlier\n"
            "campaigns and contacts, newest first (a newer note wins). Apply them to\n"
            "this writing. They never override the STRICT EMAIL RULES or the FACTS rule:\n"
            + lines
        )

    async def _write_personal_email(self, prospect, language: str = "en") -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        inbox_rule = ""
        if prospect.is_company_inbox:
            facts = [
                f"- Name: unknown (the company's general inbox, {prospect.email})",
                f"- Company: {prospect.company}",
            ]
            inbox_rule = (
                "\n- This goes to the company's general inbox, most likely read by the owner"
                "\n  or the office manager. Use no first name; open with 'Hi there' or"
                "\n  address the company's team.")
        else:
            facts = [
                f"- Name: {prospect.full_name()}",
                f"- Title: {prospect.title}",
                f"- Company: {prospect.company}",
            ]
        industry = clean_industry(prospect.industry)
        if industry:
            facts.append(f"- Industry: {industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if company:
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")
        startup = await self._startup(prospect.company_id)
        if startup is not None:
            facts.extend(startup)
        facts.extend(await hiring_facts(getattr(self, "state", None), prospect.company_id))

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
        if startup is not None:
            prompt += self._startup_playbook()
        prompt += await self._feedback_block([prospect.id])

        lang_rule = f"\n{FRENCH_RULES}\n" if language == "fr" else ""
        offer = self._writer_setting("first_email", "observation") == "offer"
        first_rule = ("Under 60 words, four sentences at most: the trigger, their problem in their\n"
                      "  words, half a sentence on you, one question asking for a reply (FIRST EMAIL: OFFER)."
                      if offer else
                      "Under 75 words. One specific observation from the FACTS above, one\n"
                      "  question. No pitch, no product name.")
        variants = VARIANTS if self._writer_setting("critique", False) else 1
        returns = ('{"subject": "...", "body": "..."}' if variants == 1 else
                   '{"variants": [{"subject": "...", "body": "..."}, ...]} with '
                   f'{variants} drafts, each a genuinely different angle on the FACTS')
        prompt += self._style_blocks()
        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS — everything you know about them, gathered from their website and
the web. Every claim about the prospect in your email must come from these
facts. If a fact isn't listed here, you don't know it — do not invent
funding rounds, mutual contacts, metrics, or anything else:
{fence(chr(10).join(facts), "prospect research")}

{UNTRUSTED_RULE}

Requirements:
- {first_rule}
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-5 words.
- No postal address, unsubscribe or opt-out line: Harvey adds them when it sends.
- Follow every STRICT EMAIL RULE above.{inbox_rule}
{lang_rule}
Return ONLY JSON: {returns}"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="personal_email",
        )
        drafts = []
        for raw in (result.get("variants") if isinstance(result, dict) and isinstance(
                result.get("variants"), list) else [result]):
            if not isinstance(raw, dict):
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if subject and body:
                drafts.append({"subject": subject[:120], "body": body[:2000]})
        if not drafts:
            return None
        return await self._pick_best(facts, drafts[:VARIANTS])

    async def _write_sequence(self, prospects: list, language: str = "en") -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )
        inbox_batch = all(p.is_company_inbox for p in prospects)
        if inbox_batch:
            merge_rule = (
                "- These go to each company's general inbox, so no names are known.\n"
                "  Use {{company}} as the merge variable in every email and NEVER\n"
                "  {{first_name}} or {{title}}. Greet with 'Hi there' or the company's team."
            )
        else:
            merge_rule = (
                "- Use {{first_name}}, {{company}}, {{title}} as merge variables — every email\n"
                "  must use at least one, and email 1 must reference something specific to\n"
                "  these prospects' industry or role (use the notes above)."
            )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        # Inject email framework skills
        if self.skills:
            prompt += "\n\n" + self.skills
        # One template goes to the whole batch: founder framing only when
        # every prospect in it is a startup.
        backed = [await self._startup(p.company_id) for p in prospects]
        if prospects and all(b is not None for b in backed):
            prompt += self._startup_playbook()
        # Campaign notes only: this template goes to the whole batch, so a
        # note about one person ("met her at the expo") must never reach it.
        prompt += await self._feedback_block([])
        prompt += self._style_blocks()
        offer = self._writer_setting("first_email", "observation") == "offer"
        email1_rule = ("Email 1: the trigger, their problem in their words, half a sentence on you, "
                       "one question asking for a reply (FIRST EMAIL: OFFER). Under 60 words."
                       if offer else
                       "Email 1: Personalized cold observation + one question. Under 75 words. No pitch.")
        follow_ups = OFFER_FOLLOW_UPS if offer else (
            "- Email 2: Follow-up 3 days later. Different angle, one concrete proof point. Under 75 words.\n"
            "- Email 3: Break-up email 4 days after that. Under 40 words. Gracious, not guilt-tripping.")
        delays = (4, 6) if offer else (3, 4)

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{fence(prospect_summary, "sample prospects")}

{UNTRUSTED_RULE}

Requirements:
- {email1_rule}
{follow_ups}
{merge_rule}
- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words.
- No postal address, unsubscribe or opt-out line: Harvey adds them when it sends.
- Follow every rule in the STRICT EMAIL RULES above. No exceptions.
{FRENCH_RULES + chr(10) if language == "fr" else ""}
Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": {delays[0]}}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": {delays[1]}}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="write_sequence",
        )
        # Read right after the call, before anything else can use the brain.
        self._no_reply = result is None and bool(getattr(self.brain, "last_reply_empty", False))
        steps = self._parse_sequence(result)
        if inbox_batch and any(
            PERSON_VAR.search(s.subject + s.body) for s in steps
        ):
            # Nobody's name is known: "Hi {{first_name}}" or "as {{title}}"
            # would render as "Hi there" at best and "as General inbox" at worst.
            logger.warning("Writer: inbox sequence used a person merge variable; discarding it.")
            return []
        polished = await humanize(
            self.brain, [{"subject": s.subject, "body": s.body} for s in steps],
            agent="writer", enabled=humanizer_enabled(self.config),
        )
        for step, draft in zip(steps, polished):
            step.subject, step.body = draft["subject"], draft["body"]
        return steps

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list, languages: dict | None = None) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo, and
        by the language they're written to in (``languages``: prospect id ->
        "fr" | "en"; English batches keep their names)."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = clean_industry(prospect.industry) or "general"
            # Inbox prospects get their own campaign: their templates can't
            # greet anyone by name.
            kind = "inbox-outreach" if prospect.is_company_inbox else "outreach"
            key = f"{industry}-{kind}"
            if (languages or {}).get(prospect.id) == "fr":
                key += "-fr"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}

"""What the writer is given and how it drafts: the industry behind a preset
search, the site's language, one contact per company, the offer-style first
email, the operator's voice and the review pass. Everything is invented."""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents import writer as W
from harvey.agents.writer import Writer
from harvey.collectors.discover import clean_industry
from harvey.collectors.profile import ProfileCollector
from harvey.config import WriterConfig, load_config
from harvey.language import company_language
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


# ── the industry inside a preset's search ──

@pytest.mark.parametrize("stored, industry", [
    ('Early-stage startups intitle:lève ("M€" OR "millions d\'euros") after:2025-04-26 -site:example.com',
     "Early-stage startups"),
    ('B2B SaaS ("we raised" OR raises) ("seed round" OR "Series A") after:2025-04-26 -site:example.com',
     "B2B SaaS"),
    ('"CTO" careers healthtech -site:example.com', "healthtech"),
    ('"Lead Dev" (recrutement OR carrières) proptech -site:example.com', "proptech"),
    ("roofing contractor", "roofing contractor"),
])
def test_clean_industry(stored, industry):
    assert clean_industry(stored) == industry


# ── the site's language ──

@pytest.mark.asyncio
async def test_a_french_site_or_description_gets_french(state):
    await state.add_company(Company(name="Lumenhexa", domain="lumenhexa.io"))
    cid = (await state.get_company_by_domain("lumenhexa.io")).id
    config = load_config()
    assert await company_language(state, config, cid, domain="lumenhexa.io") == "en"
    await state.add_observations([{"company_id": cid, "signal_code": "SITE_LANGUAGE", "value_text": "fr"}])
    assert await company_language(state, config, cid, domain="lumenhexa.io") == "fr"
    assert await company_language(state, config, domain="lumenhexa.io", description=(
        "Nous aidons les équipes à planifier leurs tournées avec une application simple")) == "fr"


@pytest.mark.asyncio
async def test_profile_records_the_declared_language():
    c = ProfileCollector(None, {"SITE_LANGUAGE"})

    async def fake_get(url):
        return '<html lang="fr-FR"><body>' + "x" * 600 + "</body></html>"

    c._get = fake_get
    obs = await c.profile_company("c1", "lumenhexa.io")
    assert [(o["signal_code"], o["value_text"]) for o in obs] == [("SITE_LANGUAGE", "fr")]


# ── one contact per company ──

def _writer(state=None, **writer_settings):
    config = load_config()
    config = config.model_copy(update={"writer": WriterConfig(**writer_settings)})
    w = Writer.__new__(Writer)
    w.state, w.config, w.skills = state, config, ""
    return w


@pytest.mark.asyncio
async def test_one_cold_contact_per_company_the_most_senior_first(state):
    await state.add_company(Company(name="Lumenhexa", domain="lumenhexa.example"))
    cid = (await state.get_company_by_domain("lumenhexa.example")).id
    ids = []
    for first, seniority, email in (("Hugo", "", "hugo@lumenhexa.example"),
                                    ("Claire", "c_suite", "claire@lumenhexa.example")):
        ids.append(await state.add_prospect(Prospect(
            first_name=first, last_name="Duval", title="x", email=email, company="Lumenhexa",
            company_id=cid, seniority=seniority, email_status="verified")))
    prospects = [await state.get_prospect(i) for i in ids]
    kept = await _writer(state)._within_company_cap(prospects, 1)
    assert [p.first_name for p in kept] == ["Claire"]
    await state.queue_prospect_if_unsent(ids[1])
    assert await _writer(state)._within_company_cap([prospects[0]], 1) == []


# ── how the first email is asked for ──

class FakeBrain:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def load_prompt(self, *a, **kw):
        return "PROMPT"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else None


def _prospect():
    return Prospect(id="p1", first_name="Claire", last_name="Duval", title="Présidente",
                    company="Lumenhexa", email="claire@lumenhexa.example", email_status="verified")


@pytest.mark.asyncio
async def test_offer_mode_asks_who_and_why(state):
    w = _writer(state, first_email="offer")
    w.brain = FakeBrain([{"subject": "s", "body": "b"}])
    await w._write_personal_email(_prospect())
    assert "FIRST EMAIL: OFFER" in w.brain.prompts[0]
    assert "No pitch, no product name" not in w.brain.prompts[0]
    w = _writer(state)
    w.brain = FakeBrain([{"subject": "s", "body": "b"}])
    await w._write_personal_email(_prospect())
    assert "FIRST EMAIL: OFFER" not in w.brain.prompts[0]


@pytest.mark.asyncio
async def test_voice_samples_are_matched_once_written(state, tmp_path, monkeypatch):
    monkeypatch.setattr(W, "SKILLS_DIR", tmp_path)
    w = _writer(state)
    assert w._voice_samples() == ""
    (tmp_path / "voice_samples.md").write_text("<!-- paste emails below -->\n", encoding="utf-8")
    assert w._voice_samples() == ""
    sample = "Bonjour Paul, j'ai vu que vous recrutiez un premier développeur. Je fais ça depuis dix ans, ça vous dit d'en parler jeudi ?"
    (tmp_path / "voice_samples.md").write_text(f"<!-- intro -->\n{sample}\n", encoding="utf-8")
    assert w._voice_samples() == sample
    assert "VOICE SAMPLES" in w._style_blocks()


@pytest.mark.asyncio
async def test_review_pass_keeps_the_best_draft(state):
    drafts = {"variants": [{"subject": f"s{i}", "body": f"draft {i}"} for i in (1, 2, 3)]}
    w = _writer(state, critique=True)
    w.brain = FakeBrain([drafts, {"best": 2, "why": "most specific"}])
    assert (await w._write_personal_email(_prospect()))["body"] == "draft 2"
    assert "3 drafts" in w.brain.prompts[0] and "Respectful" in w.brain.prompts[1]
    w.brain = FakeBrain([drafts, {"best": 9}])
    assert (await w._write_personal_email(_prospect()))["body"] == "draft 1"
    w.brain = FakeBrain([drafts, None])
    assert (await w._write_personal_email(_prospect()))["body"] == "draft 1"


@pytest.mark.asyncio
async def test_no_review_call_without_critique(state):
    w = _writer(state)
    w.brain = FakeBrain([{"subject": "s", "body": "only"}])
    assert (await w._write_personal_email(_prospect()))["body"] == "only"
    assert len(w.brain.prompts) == 1


def test_first_email_setting_is_checked():
    assert WriterConfig(first_email="Offer").first_email == "offer"
    with pytest.raises(Exception):
        WriterConfig(first_email="pitch")


def test_the_prompt_forbids_talking_down_to_the_reader():
    text = (Path(__file__).resolve().parent.parent / "prompts" / "writer.md").read_text(encoding="utf-8")
    assert "never their shortcoming" in text

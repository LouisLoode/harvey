"""Selling a service: the four-sentence first email, follow-ups that bring
something new, the angle that fits the company, and company names that
aren't headlines. Everything here is invented."""

import os
import tempfile

import pytest
import pytest_asyncio

from harvey.agents.writer import OFFER_FIRST_EMAIL, Writer
from harvey.collectors.discover import business_name, headline_name, site_root
from harvey.config import WriterConfig, load_config
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.startups import hiring_facts
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


class FakeBrain:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def load_prompt(self, *a, **kw):
        return "PROMPT"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _writer(state, **settings):
    config = load_config()
    w = Writer.__new__(Writer)
    w.state, w.skills = state, ""
    w.config = config.model_copy(update={"writer": WriterConfig(**settings)})
    return w


# ── the first email ──

def test_the_offer_asks_for_a_reply_not_a_meeting():
    text = OFFER_FIRST_EMAIL
    for part in ("The trigger", "in the reader's words", "Half a sentence on who you are",
                 "asks for a reply, not a meeting", "easy way to say", "Never ask",
                 "under 60 words", "a CTO to a company that has one"):
        assert part in text, part


@pytest.mark.asyncio
async def test_follow_ups_bring_something_new_and_wait_longer(state):
    steps = [{"step": i, "subject": "s", "body": "b", "delay_days": d} for i, d in ((1, 0), (2, 4), (3, 6))]
    w = _writer(state, first_email="offer")
    w.brain = FakeBrain(steps)
    await w._write_sequence([Prospect(first_name="Claire", last_name="Duval", title="CEO", company="Lumenhexa")])
    prompt = w.brain.prompts[0]
    assert "Something new" in prompt and '"delay_days": 4' in prompt and '"delay_days": 6' in prompt
    w = _writer(state)
    w.brain = FakeBrain(steps)
    await w._write_sequence([Prospect(first_name="Claire", last_name="Duval", title="CEO", company="Lumenhexa")])
    assert '"delay_days": 3' in w.brain.prompts[0] and "Something new" not in w.brain.prompts[0]


# ── a company that has a CTO ──

@pytest.mark.asyncio
async def test_the_writer_is_told_when_they_have_a_cto(state):
    await state.add_company(Company(name="Lumenhexa", domain="lumenhexa.example"))
    cid = (await state.get_company_by_domain("lumenhexa.example")).id
    await state.add_observations([{"company_id": cid, "signal_code": "NO_TECH_LEADER",
                                   "value_num": 0, "value_text": "CTO & co-founder"}])
    facts = await hiring_facts(state, cid)
    assert facts == ["- They have a technical lead: their team page lists a CTO & co-founder. "
                     "Don't offer to be their CTO or to build their first version"]


# ── names and websites ──

@pytest.mark.parametrize("name, domain, expected", [
    ("Lumenhexa lève 2,7 millions d'euros pour devenir le leader", "lumenhexa.example", "Lumenhexa"),
    ("Lumenhexa raises $12M Series A", "lumenhexa.example", "Lumenhexa"),
    ("La startup toulousaine annonce 3 M€", "lumenhexa.example", "Lumenhexa"),
    ("Northvale Roofing", "northvale.example", "Northvale Roofing"),
])
def test_a_headline_is_not_a_name(name, domain, expected):
    assert headline_name(name, domain) == expected


def test_search_results_and_websites():
    assert business_name("Lumenhexa lève 1,7 M€ | Blog Lumenhexa", "lumenhexa.example") == "Lumenhexa"
    assert site_root("https://www.lumenhexa.example/blog/lumenhexa-leve") == "https://www.lumenhexa.example"
    assert site_root("https://lumenhexa.example/") == "https://lumenhexa.example/"


@pytest.mark.asyncio
async def test_existing_headline_names_are_repaired_on_start(state):
    await state.add_company(Company(name="Lumenhexa lève 1,7 M€ pour grandir", domain="lumenhexa.example",
                                    website="https://lumenhexa.example/blog/levee"))
    cid = (await state.get_company_by_domain("lumenhexa.example")).id
    await state.add_prospect(Prospect(first_name="Claire", last_name="Duval", title="CEO",
                                      email="claire@lumenhexa.example", company_id=cid,
                                      company="Lumenhexa lève 1,7 M€ pour grandir"))
    await state.init_db()
    company = await state.get_company(cid)
    assert (company.name, company.website) == ("Lumenhexa", "https://lumenhexa.example")
    assert (await state.get_prospects_by_status("new"))[0].company == "Lumenhexa"

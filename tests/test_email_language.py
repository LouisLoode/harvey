"""French to French companies, English to everyone else: the language a
company is written to in, the writer's prompts, the footer, and French
opt-outs. Every business and person here is invented."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.handler import _opt_out_reason
from harvey.agents.writer import Writer
from harvey.config import ComplianceConfig, compliance_footer_lines, load_config
from harvey.language import company_language, text_language
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


def _config(language="auto"):
    config = load_config()
    email = config.channels.email.model_copy(update={"language": language})
    return config.model_copy(update={"channels": config.channels.model_copy(update={"email": email})})


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


# ── which language ──

@pytest.mark.asyncio
async def test_french_companies_get_french(state):
    config = _config()
    assert await company_language(state, config, domain="lumenhexa.fr") == "fr"
    assert await company_language(state, config, domain="lumenhexa.io",
                                  location="Toulouse, Occitanie, France") == "fr"
    assert await company_language(state, config, domain="lumenhexa.io", location="Genève, Suisse romande") == "fr"
    assert await company_language(state, config, domain="lumenhexa.io", location="Denver, CO") == "en"
    assert await company_language(state, config) == "en"


@pytest.mark.asyncio
async def test_the_registry_makes_a_company_french(state):
    await state.add_company(Company(name="Lumenhexa", domain="lumenhexa.io"))
    cid = (await state.get_company_by_domain("lumenhexa.io")).id
    assert await company_language(state, _config(), cid, domain="lumenhexa.io") == "en"
    await state.add_observations([{"company_id": cid, "signal_code": "NAF_CODE", "value_text": "62.01Z"}])
    assert await company_language(state, _config(), cid, domain="lumenhexa.io") == "fr"


@pytest.mark.asyncio
async def test_a_fixed_language_wins(state):
    assert await company_language(state, _config("en"), domain="lumenhexa.fr") == "en"
    assert await company_language(state, _config("fr"), domain="lumenhexa.io", location="Denver, CO") == "fr"


def test_unknown_language_setting_is_refused():
    config = load_config()
    with pytest.raises(Exception):
        config.channels.email.model_validate({**config.channels.email.model_dump(), "language": "de"})


def test_text_language():
    assert text_language("Bonjour Claire, votre équipe recrute un lead dev depuis deux mois ?") == "fr"
    assert text_language("Hi Claire, is your team still hiring a lead developer?") == "en"


# ── the writer ──

class FakeBrain:
    def __init__(self):
        self.prompts = []

    def load_prompt(self, *a, **kw):
        return "PROMPT"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "votre lead dev", "body": "Bonjour Claire, ..."}


def _prospect(**kw):
    base = dict(first_name="Claire", last_name="Duval", title="Présidente", company="Lumenhexa",
                email="claire@lumenhexa.fr", email_status="verified", industry="saas")
    return Prospect(**{**base, **kw})


@pytest.mark.asyncio
async def test_a_french_draft_is_asked_for_in_french(state):
    writer = Writer.__new__(Writer)
    writer.brain, writer.state, writer.config, writer.skills = FakeBrain(), state, _config(), ""
    await writer._write_personal_email(_prospect(), "fr")
    await writer._write_personal_email(_prospect(), "en")
    assert "write this email in French" in writer.brain.prompts[0]
    assert "in French" not in writer.brain.prompts[1]


def test_batches_split_by_language():
    writer = Writer.__new__(Writer)
    writer.config = _config()
    fr, en = _prospect(id="p1"), _prospect(id="p2", first_name="Dana", email="dana@northvale.example")
    batches = writer._group_prospects([fr, en], {fr.id: "fr", en.id: "en"})
    assert {k: [p.first_name for p in v] for k, v in batches.items()} == {
        "saas-outreach-fr": ["Claire"], "saas-outreach": ["Dana"]}


# ── footer and opt-outs ──

def test_footer_follows_the_email_language():
    config = load_config()
    fr = compliance_footer_lines(config, "fr")[-1]
    assert "non merci" in fr and compliance_footer_lines(config)[-1] != fr


def test_a_french_opt_out_line_must_ask_for_a_french_opt_out():
    assert ComplianceConfig(opt_out_line_fr="Répondez STOP et je n'écrirai plus.").opt_out_line_fr
    with pytest.raises(Exception):
        ComplianceConfig(opt_out_line_fr="Bonne journée à vous")


@pytest.mark.parametrize("reply", [
    "Non merci.", "non, merci", "Merci de me désinscrire", "Je ne souhaite plus recevoir vos emails",
    "Désinscription", "Ne me contactez plus svp",
])
def test_french_opt_outs_are_honoured(reply):
    assert _opt_out_reason(reply, reply) is not None


@pytest.mark.parametrize("reply", [
    "Non merci pour le lien, je regarde ça demain", "Oui ou non, merci de préciser",
    "Avec plaisir, quand êtes-vous dispo ?",
])
def test_french_replies_that_are_not_opt_outs(reply):
    assert _opt_out_reason(reply, reply) is None

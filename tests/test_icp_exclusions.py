"""ICP exclusions (competitors, recruiters, funds) and the contacts backfill
order. Every business here is invented."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.collectors.profile import ProfileCollector
from harvey.config import load_config
from harvey.icp_filter import excluded_by, has_exclusions, naf_excluded
from harvey.models.company import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

ICP = SimpleNamespace(exclude_keywords=["ESN", "cabinet de recrutement", "agence web"],
                      exclude_naf=["78.", "64.30", "62.02A"])


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


# ── matching ──

def test_keywords_match_whole_words_ignoring_case_and_accents():
    assert excluded_by(ICP, "Talentrive — Cabinet de Recrutement IT") == "cabinet de recrutement"
    assert excluded_by(ICP, "Nous sommes une ESN à taille humaine") == "ESN"
    assert excluded_by(ICP, "AGENCE WEB à Lyon") == "agence web"
    assert excluded_by(ICP, "Notre agence webinaire") == ""
    assert excluded_by(ICP, "Lesnoria, logiciel de planning") == ""
    assert excluded_by(SimpleNamespace(exclude_keywords=["chasseur de têtes"]),
                       "Chasseur de tetes tech") == "chasseur de têtes"


def test_naf_prefixes():
    assert naf_excluded(ICP, "78.10Z") == "78."
    assert naf_excluded(ICP, "64.30Z") == "64.30"
    assert naf_excluded(ICP, "62.02A") == "62.02A"
    assert naf_excluded(ICP, "62.01Z") == ""
    assert naf_excluded(ICP, "") == ""


def test_no_lists_no_exclusions():
    empty = SimpleNamespace()
    assert not has_exclusions(empty)
    assert excluded_by(empty, "ESN") == "" and naf_excluded(empty, "78.10Z") == ""
    assert has_exclusions(ICP)


# ── discovery never stores an excluded business ──

class FakeProvider(D.DiscoveryProvider):
    key = "fake"
    label = "Fake"
    cost_note = "free"

    def __init__(self, found):
        self.found = found

    def configured(self, env):
        return True

    def estimate(self, queries):
        return 0.0

    async def fetch(self, client, env, query):
        self.last_cost = 0.0
        return self.found


@pytest.mark.asyncio
async def test_discovery_leaves_out_what_the_icp_excludes(state, monkeypatch):
    found = [D.Business(provider="fake", domain="lumenhexa.example", name="Lumenhexa",
                        raw_title="Lumenhexa — logiciel de planning"),
             D.Business(provider="fake", domain="talentrive.example", name="Talentrive",
                        raw_title="Talentrive | Cabinet de recrutement tech")]
    monkeypatch.setitem(D.PROVIDERS, "fake", FakeProvider(found))
    config = load_config()
    config = config.model_copy(update={"icp": config.icp.model_copy(
        update={"exclude_keywords": ["cabinet de recrutement"]})})
    report = await D.run_discovery(state, config, "fake", [D.DiscoveryQuery(term="saas", location="Toulouse")])
    assert report.icp_excluded == 1 and report.new_companies == 1
    assert report.as_dict()["icp_excluded"] == 1
    assert report.excluded == 0          # do-not-contact skips are counted apart
    assert await state.get_company_by_domain("talentrive.example") is None


# ── profiling records ICP_EXCLUDED ──

def _profiler(pages, icp, confirmed=()):
    c = ProfileCollector(None, set(confirmed), icp=icp)

    async def fake_get(url):
        return pages.get(url, "")

    c._get = fake_get
    return c


@pytest.mark.asyncio
async def test_profile_marks_a_company_its_own_site_rules_out():
    home = ("<html><head><title>Talentrive</title><meta name=\"description\" content=\"Cabinet de "
            "recrutement spécialisé tech\"></head><body>" + "x" * 600 + "</body></html>")
    obs = await _profiler({"https://talentrive.example": home}, ICP).profile_company("c1", "talentrive.example")
    rows = [(o["value_num"], o["value_text"]) for o in obs if o["signal_code"] == "ICP_EXCLUDED"]
    assert rows == [(1, 'its site says "cabinet de recrutement"')]


@pytest.mark.asyncio
async def test_profile_says_not_excluded_and_stays_quiet_without_lists():
    home = "<html><head><title>Lumenhexa — planning</title></head><body>" + "x" * 600 + "</body></html>"
    obs = await _profiler({"https://lumenhexa.example": home}, ICP).profile_company("c1", "lumenhexa.example")
    assert [(o["signal_code"], o["value_num"]) for o in obs] == [("ICP_EXCLUDED", 0)]
    obs = await _profiler({"https://lumenhexa.example": home}, SimpleNamespace()).profile_company(
        "c1", "lumenhexa.example")
    assert obs == []


# ── the contacts backfill ──

async def _company(state, domain):
    await state.add_company(Company(name=domain.split(".")[0].title(), domain=domain))
    return (await state.get_company_by_domain(domain)).id


@pytest.mark.asyncio
async def test_backfill_skips_excluded_and_puts_intent_first(state):
    plain = await _company(state, "plainco.example")
    hiring = await _company(state, "hiringco.example")
    excluded = await _company(state, "recruitco.example")
    lapsed = await _company(state, "lapsedco.example")
    await state.add_observations([
        {"company_id": hiring, "signal_code": "HIRING_ROLE", "value_text": "CTO"},
        {"company_id": hiring, "signal_code": "FUNDING_ROUND", "value_num": 1},
        {"company_id": excluded, "signal_code": "ICP_EXCLUDED", "value_num": 1},
        {"company_id": excluded, "signal_code": "HIRING_ROLE", "value_text": "CTO"},
        # Excluded once, not any more: the latest reading wins.
        {"company_id": lapsed, "signal_code": "ICP_EXCLUDED", "value_num": 1,
         "observed_at": "2026-01-01 00:00:00"},
        {"company_id": lapsed, "signal_code": "ICP_EXCLUDED", "value_num": 0},
        {"company_id": lapsed, "signal_code": "HIRING_ROLE", "value_num": 0},
    ])
    ids = [c["id"] for c in await state.companies_needing_prospects(limit=10)]
    assert excluded not in ids
    assert ids[0] == hiring
    assert set(ids) == {plain, hiring, lapsed}

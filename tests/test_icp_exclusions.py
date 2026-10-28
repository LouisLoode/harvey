"""ICP exclusions (competitors, recruiters, funds, whole kinds of site) and
the contacts backfill order. Every business here is invented."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

from harvey.agents.scout import Scout
from harvey.agents.writer import Writer
from harvey.collectors import discover as D
from harvey.collectors import profile as profile_mod
from harvey.collectors.profile import ProfileCollector
from harvey.config import ICPConfig, load_config
from harvey.icp_filter import (
    excluded_by, has_exclusions, kind_excluded, naf_excluded, site_excluded, site_kind,
)
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

ICP = SimpleNamespace(exclude_keywords=["ESN", "cabinet de recrutement", "agence web"],
                      exclude_naf=["78.", "64.30", "62.02A"])
KINDS = SimpleNamespace(exclude_kinds=["job_boards", "recruiters", "media"])


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


def test_curly_apostrophes_match():
    icp = SimpleNamespace(exclude_keywords=["fonds d'investissement"])
    assert excluded_by(icp, "Un fonds d’investissement early-stage") == "fonds d'investissement"


def test_kinds_recognise_job_boards_recruiters_and_media():
    assert kind_excluded(KINDS, "Postelia", "Trouvez votre emploi parmi 40 000 offres d’emploi") == (
        "job_boards", "offres d'emploi")
    assert kind_excluded(KINDS, "Hirefold — the remote job platform") == ("job_boards", "job platform")
    assert kind_excluded(KINDS, "Talentrive : cabinet de recrutement tech") == (
        "recruiters", "cabinet de recrutement")
    assert kind_excluded(KINDS, "Northpeak Executive Search") == ("recruiters", "executive search")
    assert kind_excluded(KINDS, "Le média des fondateurs qui lèvent") == ("media", "le média des")
    # A careers page, an HR software and a newsletter tool are buyers.
    assert kind_excluded(KINDS, "Careers at Lumenhexa — 4 open jobs") == ("", "")
    assert kind_excluded(KINDS, "Lumenhexa, le logiciel de recrutement des PME") == ("", "")
    assert kind_excluded(KINDS, "Quillmark — send your newsletter in minutes") == ("", "")
    # Only the kinds the ICP names.
    assert kind_excluded(SimpleNamespace(exclude_kinds=["media"]),
                         "Talentrive : cabinet de recrutement") == ("", "")


def test_site_excluded_says_why():
    icp = SimpleNamespace(exclude_keywords=["ESN"], exclude_kinds=["job_boards"])
    assert site_excluded(icp, "Nous sommes une ESN") == 'its site says "ESN"'
    assert site_excluded(icp, "Postelia", "40 000 offres d'emploi") == (
        "its site reads like a job board (\"offres d'emploi\")")
    assert site_excluded(icp, "Lumenhexa — planning") == ""
    assert has_exclusions(SimpleNamespace(exclude_kinds=["media"]))


def test_config_knows_the_kinds():
    base = dict(industries=[], company_size="", titles=[], geography=[])
    assert ICPConfig(**base, exclude_kinds=["Job_Boards", "media", "media"]).exclude_kinds == [
        "job_boards", "media"]
    with pytest.raises(ValidationError, match="exclude_kinds"):
        ICPConfig(**base, exclude_kinds=["agencies"])


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
async def test_profile_rules_out_a_job_board_by_its_homepage():
    home = ("<html><head><title>Postelia — 40 000 offres d’emploi</title></head><body>"
            + "x" * 600 + "</body></html>")
    obs = await _profiler({"https://postelia.example": home}, KINDS).profile_company("c1", "postelia.example")
    rows = [(o["value_num"], o["value_text"]) for o in obs if o["signal_code"] == "ICP_EXCLUDED"]
    assert rows == [(1, "its site reads like a job board (\"offres d'emploi\")")]


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


# ── no contacts, no drafts at a ruled-out company ──

def _gated_scout(state, home, icp):
    """A Scout whose site reads and team page are stand-ins."""
    scout = Scout.__new__(Scout)
    scout.config = SimpleNamespace(icp=icp)
    scout.state = state
    scout.reads = 0

    async def read_site(domain):
        scout.reads += 1
        return {"pages": [(f"https://{domain}", home)]}

    async def team_page(domain):
        raise AssertionError("looked for people at a ruled-out company")

    scout._read_site = read_site
    scout._scrape_team_page = team_page
    return scout


@pytest.mark.asyncio
async def test_scout_looks_for_nobody_at_a_company_its_homepage_rules_out(state):
    cid = await _company(state, "talentrive.example")
    home = "<html><head><title>Talentrive | Cabinet de recrutement tech</title></head></html>"
    scout = _gated_scout(state, home, KINDS)
    assert await scout._contacts_from_company(cid, "talentrive.example", "Talentrive", "SaaS") == []
    assert await state.company_ruled_out(cid) == "its site reads like a recruiter (\"cabinet de recrutement\")"
    # On record now: the homepage isn't read again.
    assert await scout._ruled_out(cid, "talentrive.example")
    assert scout.reads == 1


@pytest.mark.asyncio
async def test_scout_reads_nothing_more_for_a_buyer_or_without_lists(state):
    cid = await _company(state, "lumenhexa.example")
    home = "<html><head><title>Lumenhexa — logiciel de planning</title></head></html>"
    scout = _gated_scout(state, home, KINDS)
    assert await scout._ruled_out(cid, "lumenhexa.example") == ""
    assert await state.company_ruled_out(cid) == ""
    quiet = _gated_scout(state, home, SimpleNamespace())
    assert await quiet._ruled_out(cid, "lumenhexa.example") == "" and quiet.reads == 0


@pytest.mark.asyncio
async def test_people_at_a_ruled_out_company_are_not_written_to(state):
    cid = await _company(state, "talentrive.example")
    await state.add_prospect(Prospect(first_name="Ana", last_name="Vidal", email="ana@talentrive.example",
                                      email_status="verified", company="Talentrive", company_id=cid))
    assert await state.count_writable_prospects() == 1
    await state.add_observation("ICP_EXCLUDED", company_id=cid, value_num=1,
                                value_text="its site reads like a recruiter",
                                observed_at="2026-01-01 00:00:00")
    assert await state.count_writable_prospects() == 0

    writer = Writer.__new__(Writer)
    writer.state = state
    writer.brain = SimpleNamespace(load_skills_for_agent=lambda agent: "")

    def drafted(*args, **kw):
        raise AssertionError("drafted for a ruled-out company")

    writer._group_prospects = drafted
    await writer.run()

    # Ruled back in by a later reading: writable again.
    await state.add_observation("ICP_EXCLUDED", company_id=cid, value_num=0)
    assert await state.count_writable_prospects() == 1


def test_scout_search_skips_what_discovery_calls_junk():
    scout = Scout.__new__(Scout)
    assert scout._is_noise_domain("welcometothejungle.com")      # a French job board
    assert scout._is_noise_domain("news.maddyness.com")           # French tech press
    assert not scout._is_noise_domain("lumenhexa.example")



# ── a kind the ICP keeps is written to on its own ──

def test_site_kind_names_any_kind():
    assert site_kind("Talentrive : cabinet de recrutement tech") == ("recruiters", "cabinet de recrutement")
    assert site_kind("Bordage, le média communautaire des passionnés de voile") == (
        "media", "média communautaire")
    assert site_kind("L'annuaire des start-up de France") == ("media", "annuaire des startup")
    assert site_kind("Lumenhexa — logiciel de planning") == ("", "")
    assert site_kind("Talentrive : cabinet de recrutement", kinds=["media"]) == ("", "")


@pytest.mark.asyncio
async def test_profile_keeps_the_homepage_description_when_there_is_none(state, monkeypatch):
    monkeypatch.setattr(profile_mod.random, "uniform", lambda a, b: 0)
    cid = await _company(state, "bordage.example")
    home = ('<html><head><title>Bordage</title><meta name="description" content="Bordage, '
            'le média communautaire des passionnés de voile"></head><body>' + "x" * 600 + "</body></html>")
    collector = ProfileCollector(state, set(), icp=SimpleNamespace())

    async def fake_get(url):
        return home if url == "https://bordage.example" else ""

    collector._get = fake_get
    await collector.run([{"id": cid, "domain": "bordage.example", "name": "Bordage"}])
    company = await state.get_company(cid)
    assert company.description == "Bordage, le média communautaire des passionnés de voile"
    # One it already has is kept.
    await state.describe_company(cid, "something else")
    assert (await state.get_company(cid)).description.startswith("Bordage,")


def _writer(state):
    writer = Writer.__new__(Writer)
    writer.state = state
    writer.config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(provider="smtp")))
    return writer


@pytest.mark.asyncio
async def test_writer_batches_recruiters_and_media_apart_from_startups(state):
    people = []
    for domain, description in (("talentrive.example", "Cabinet de recrutement tech à Lyon"),
                                ("bordage.example", "Le média communautaire des passionnés de voile"),
                                ("lumenhexa.example", "Logiciel de planning pour les PME")):
        await state.add_company(Company(name=domain.split(".")[0].title(), domain=domain,
                                        description=description))
        cid = (await state.get_company_by_domain(domain)).id
        people.append(Prospect(id=domain, first_name="Ana", last_name="Vidal", industry="SaaS B2B",
                               company=domain, company_id=cid))
    writer = _writer(state)
    kinds = {p.id: (await writer._site_kind(p))[0] for p in people}
    assert kinds == {"talentrive.example": "recruiters", "bordage.example": "media",
                     "lumenhexa.example": ""}
    batches = writer._group_prospects(people, {p.id: "fr" for p in people}, kinds)
    assert {k: [p.id for p in v] for k, v in batches.items()} == {
        "recruiters-outreach-fr": ["talentrive.example"],
        "media-outreach-fr": ["bordage.example"],
        "SaaS B2B-outreach-fr": ["lumenhexa.example"],
    }

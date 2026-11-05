"""The French company registry: SIREN on the company's own site, one lookup,
directors kept only when they are people who run the company.

Every company, person and number here is invented (000 000 018 passes the
check digit and belongs to no one).
"""

import json
import os
import tempfile
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey.agents.scout import Scout, _merge_known_people
from harvey.collectors import profile as profile_module
from harvey.collectors import registry as R
from harvey.collectors.profile import ProfileCollector
from harvey.models.company import Company
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

SIREN = "000000018"
SIEGE = {"siret": SIREN + "00011", "est_siege": True, "adresse": "12 RUE DE L'EXEMPLE 31000 TOULOUSE",
         "code_postal": "31000", "libelle_commune": "TOULOUSE", "statut_diffusion_etablissement": "O"}
RESULT = {
    "siren": SIREN, "nom_complet": "LUMENHEXA", "date_creation": "2022-03-14",
    "tranche_effectif_salarie": "02", "annee_tranche_effectif_salarie": "2024",
    "activite_principale": "62.01Z", "etat_administratif": "A", "statut_diffusion": "O",
    "dirigeants": [
        {"nom": "DUVAL", "prenoms": "Claire Marie", "qualite": "Président de SAS",
         "type_dirigeant": "personne physique", "date_de_naissance": "1990-01", "annee_de_naissance": "1990"},
        {"denomination": "AUDIT EXAMPLE", "qualite": "Commissaire aux comptes titulaire",
         "type_dirigeant": "personne morale"},
        {"nom": "MARTIN-ROUX", "prenoms": "Paul", "qualite": "Commissaire aux comptes suppléant",
         "type_dirigeant": "personne physique"},
        {"nom": "GIRARD", "prenoms": "Hugo", "qualite": "Directeur Général",
         "type_dirigeant": "personne physique"},
    ],
    "siege": SIEGE,
}


# ── finding the SIREN ──

@pytest.mark.parametrize("text", [
    "SIREN : 000 000 018", "RCS Toulouse B 000 000 018", "R.C.S. Paris 000000018",
    "SIRET 000 000 018 00011", "N° TVA intracommunautaire FR 32 000000018",
    "Immatriculée sous le numéro 000.000.018",
])
def test_a_published_siren_is_found(text):
    assert R.find_siren(f"<footer><p>Lumenhexa SAS · {text}</p></footer>") == SIREN


@pytest.mark.parametrize("text", [
    "Appelez le 05 61 00 00 18", "Capital social : 10 000 €", "Commande n° 000000018",
    "SIREN : 000 000 019",          # wrong check digit
    "SIREN : 000 000 000",
])
def test_numbers_that_are_not_a_siren(text):
    assert R.find_siren(f"<p>{text}</p>") == ""


def test_luhn():
    assert R.luhn_ok("000000018") and not R.luhn_ok("000000019")


def test_legal_links_and_french_sites():
    home = ('<html lang="fr-FR"><a href="/mentions-legales">Mentions légales</a>'
            '<a href="https://elsewhere.example/legal">Legal</a><a href="/blog">Blog</a></html>')
    assert R.legal_links(home, "https://lumenhexa.example") == ["https://lumenhexa.example/mentions-legales"]
    assert R.looks_french(home, "lumenhexa.example")
    assert R.looks_french("<html></html>", "lumenhexa.fr")
    assert not R.looks_french('<html lang="en"></html>', "lumenhexa.example")


# ── reading the record ──

def test_parse_keeps_directors_who_run_it_and_never_birth_dates():
    company = R.parse_company(RESULT, SIREN)
    assert [d.label for d in company.directors] == ["Claire Duval — Président", "Hugo Girard — Directeur Général"]
    assert company.headcount == ("3–5 salariés", 3)
    assert company.age_years(date(2026, 3, 14)) == 4.0
    assert "1990" not in json.dumps(company.__dict__, default=lambda o: o.__dict__)


def test_a_private_record_gives_no_directors_and_another_siren_nothing():
    private = R.parse_company({**RESULT, "statut_diffusion": "P"}, SIREN)
    assert private.directors == [] and private.naf == "62.01Z"
    assert R.parse_company(RESULT, "000000026") is None
    assert R.parse_company({**RESULT, "etat_administratif": "C"}, SIREN).closed


def test_parse_keeps_the_head_office_town_and_never_its_address():
    company = R.parse_company(RESULT, SIREN)
    assert company.city == "Toulouse" and company.location == "Toulouse, France"
    kept = json.dumps(company.__dict__, default=lambda o: o.__dict__)
    assert "31000" not in kept and "EXEMPLE" not in kept


@pytest.mark.parametrize("commune, city", [
    ("LA ROCHE-SUR-YON", "La Roche-sur-Yon"), ("VILLENEUVE-D'ASCQ", "Villeneuve-d'Ascq"),
    ("L'ISLE-D'ABEAU", "L'Isle-d'Abeau"), ("PARIS 15", "Paris"), ("LYON 3EME", "Lyon"),
    ("Saint-Malo", "Saint-Malo"),
])
def test_town_names_read_like_towns(commune, city):
    result = {**RESULT, "siege": {**SIEGE, "libelle_commune": commune}}
    assert R.parse_company(result, SIREN).city == city


@pytest.mark.parametrize("change", [
    {"statut_diffusion": "P"},                                     # the company's record isn't public
    {"siege": {**SIEGE, "statut_diffusion_etablissement": "P"}},    # its head office isn't
    {"siege": {**SIEGE, "libelle_commune": None, "code_postal": None, "code_pays_etranger": "99131",
               "libelle_commune_etranger": "EXAMPLEVILLE", "libelle_pays_etranger": "BELGIQUE"}},
    {"siege": {**SIEGE, "libelle_commune": "[ND]"}},
    {"siege": None},
])
def test_no_town_unless_a_public_head_office_in_france(change):
    company = R.parse_company({**RESULT, **change}, SIREN)
    assert company.city == "" and company.location == ""


def _client(responses):
    calls = []

    def handler(request):
        calls.append(request)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


@pytest.mark.asyncio
async def test_lookup_retries_once_when_rate_limited(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(R.asyncio, "sleep", no_sleep)
    client, calls = _client([(429, {}), (200, {"results": [RESULT]})])
    company = await R.lookup(client, SIREN)
    assert company.name == "LUMENHEXA" and len(calls) == 2
    assert calls[0].url.params["q"] == SIREN


@pytest.mark.asyncio
async def test_lookup_accepts_only_that_siren():
    client, _ = _client([(200, {"results": [{**RESULT, "siren": "000000026"}]})])
    assert await R.lookup(client, SIREN) is None
    client, _ = _client([(404, {})])
    assert await R.lookup(client, SIREN) is None


# ── the profile stage ──

HOME = ('<html lang="fr"><body>' + "x" * 600 + '<footer><a href="/mentions-legales">Mentions légales</a>'
        "</footer></body></html>")
LEGAL = "<html><body>" + "x" * 600 + "<p>Lumenhexa SAS, RCS Toulouse 000 000 018</p></body></html>"


def _profiler(confirmed, icp=None, result=RESULT):
    c = ProfileCollector(None, set(confirmed), icp=icp)
    pages = {"https://lumenhexa.example": HOME, "https://lumenhexa.example/mentions-legales": LEGAL}

    async def fake_get(url):
        return pages.get(url, "")

    def handler(request):
        if request.url.host == "recherche-entreprises.api.gouv.fr":
            return httpx.Response(200, json={"results": [result]})
        return httpx.Response(404)

    c._get = fake_get
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_profile_records_the_registry():
    c = _profiler({"COMPANY_AGE", "HEADCOUNT_BAND", "NAF_CODE", "REGISTRY_VERIFIED"})
    obs = await c.profile_company("c1", "lumenhexa.example")
    got = {(o["signal_code"], o["value_text"]) for o in obs}
    assert ("NAF_CODE", "62.01Z") in got and ("HEADCOUNT_BAND", "3–5 salariés (2024)") in got
    assert ("REGISTRY_VERIFIED", "Claire Duval — Président") in got
    assert ("REGISTRY_VERIFIED", "Hugo Girard — Directeur Général") in got
    assert all(o["evidence_url"] == R.annuaire_url(SIREN) for o in obs)
    assert "1990" not in json.dumps(obs)


@pytest.mark.asyncio
async def test_registry_exclusions():
    icp = SimpleNamespace(exclude_naf=["62.01"])
    obs = await _profiler(set(), icp=icp).profile_company("c1", "lumenhexa.example")
    assert [(o["signal_code"], o["value_num"], o["value_text"]) for o in obs] == [
        ("ICP_EXCLUDED", 1, "its activity code 62.01Z is under 62.01")]
    closed = {**RESULT, "etat_administratif": "C"}
    obs = await _profiler(set(), icp=SimpleNamespace(exclude_naf=["78."]), result=closed).profile_company(
        "c1", "lumenhexa.example")
    assert [o["value_text"] for o in obs if o["signal_code"] == "ICP_EXCLUDED"] == [
        "the registry says it has closed"]


@pytest.mark.asyncio
async def test_no_registry_call_when_nothing_asks_for_it():
    c = _profiler({"TECH_STACK"})
    requested = []
    original = c._get

    async def spy(url):
        requested.append(url)
        return await original(url)

    c._get = spy
    await c.profile_company("c1", "lumenhexa.example")
    assert requested == ["https://lumenhexa.example"]


# ── Scout: directors become contacts ──

def test_directors_merge_with_the_team_page():
    site = [{"first_name": "Hélène", "last_name": "Marchand", "title": "Directrice Marketing", "owner": False,
             "rank": 40}]
    known = [{"first_name": "Helene", "last_name": "Marchand", "title": "Présidente", "owner": True},
             {"first_name": "Hugo", "last_name": "Girard", "title": "Directeur Général", "owner": True}]
    merged = _merge_known_people(site, known)
    assert [(m["first_name"], m["owner"]) for m in merged] == [("Hélène", True), ("Hugo", True)]
    assert merged[0]["title"] == "Directrice Marketing" and merged[1]["rank"] >= 90


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


@pytest.mark.asyncio
async def test_scout_reads_only_the_latest_registry_reading(state):
    await state.add_company(Company(name="Lumenhexa", domain="lumenhexa.example"))
    cid = (await state.get_company_by_domain("lumenhexa.example")).id
    await state.add_observations([
        {"company_id": cid, "signal_code": "REGISTRY_VERIFIED", "value_text": "Paul Lefort — Gérant",
         "observed_at": "2025-01-01 00:00:00"},
        {"company_id": cid, "signal_code": "REGISTRY_VERIFIED", "value_text": "Claire Duval — Président",
         "observed_at": "2026-09-01 00:00:00", "evidence_url": R.annuaire_url(SIREN)},
    ])
    scout = Scout.__new__(Scout)
    scout.state = state
    found = await scout._registry_directors(cid)
    assert [(p["first_name"], p["last_name"], p["title"], p["owner"]) for p in found] == [
        ("Claire", "Duval", "Président", True)]


# ── the head office's town fills companies.location ──

@pytest.mark.asyncio
async def test_profile_notes_the_head_office_town():
    c = _profiler({"NAF_CODE"})
    await c.profile_company("c1", "lumenhexa.example")
    assert c.locations == {"c1": "Toulouse, France"}


@pytest.mark.asyncio
@pytest.mark.parametrize("before, after", [
    ("", "Toulouse, France"),
    ("France", "Toulouse, France"),
    ("Lyon, France", "Lyon, France"),           # a town discovery found
    ("Côte basque", "Côte basque"),             # what the operator typed
])
async def test_profiling_fills_only_a_missing_location(state, monkeypatch, before, after):
    monkeypatch.setattr(profile_module.random, "uniform", lambda a, b: 0)
    await state.add_company(Company(name="Lumenhexa", domain="lumenhexa.example", location=before))
    company = await state.get_company_by_domain("lumenhexa.example")
    c = _profiler({"NAF_CODE"})
    c.state = state
    await c.run([{"id": company.id, "name": company.name, "domain": company.domain}])
    assert (await state.get_company(company.id)).location == after

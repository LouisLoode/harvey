"""French startups: the presets search in French (icp.search_languages), and
the profile reads a raise or a careers page written in French.

Every business here is fictional (acme.fr, example.fr).
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from harvey.collectors import discover as D
from harvey.collectors.discover import build_queries, is_junk
from harvey.collectors.profile import _CAREERS_ANCHOR, _CAREERS_SEGMENTS, funding_round
from harvey.config import ICPConfig


def _config(languages=None, industries=("fintech",)):
    icp = SimpleNamespace(industries=list(industries), geography=["France"], geo_coordinates={},
                          hiring_signals=["CTO"], titles=[])
    if languages is not None:
        icp.search_languages = languages
    return SimpleNamespace(icp=icp, product=SimpleNamespace(name="P"))


# ── a raise, announced in French ──


@pytest.mark.parametrize("html,round_,amount,when", [
    ("<p>Nous avons levé 2 M€ auprès de Foo Ventures en mars 2025.</p>", "", "€2M", "2025-03"),
    ("<h1>Acme lève 3 millions d'euros pour accélérer en Europe</h1>", "", "€3M", ""),
    ("<p>Acme annonce une levée de fonds de 1,5 M€.</p>", "", "€1.5M", ""),
    ("<p>Acme boucle un tour de table de 5 M€ en série A.</p>", "Series A", "€5M", ""),
    ("<p>Nous sommes ravis d'annoncer notre levée de fonds de 2 millions d'euros.</p>", "", "€2M", ""),
    ("<p>Acme vient de lever 800 k€ en amorçage.</p>", "Seed", "€800K", ""),
    ("<p>Acme a réalisé une levée de fonds de 2 500 000 € le 12 février 2026.</p>", "", "€2.5M", "2026-02-12"),
    ("<p>Notre levée de fonds de 4 M€ a été menée par Foo Capital.</p>", "", "€4M", ""),
    ("<p>Annonce de notre levée de fonds seed de 1 M€</p>", "Seed", "€1M", ""),
    ("<p>Aujourd'hui, Acme annonce avoir levé 10 M€ en série B.</p>", "Series B", "€10M", ""),
    ("<p>Nous avons bouclé notre pré-amorçage de 500 000 €.</p>", "Pre-seed", "€500K", ""),
])
def test_a_company_announcing_its_own_raise_in_french(html, round_, amount, when):
    got = funding_round(html, domain="acme.fr")
    assert got is not None
    assert (got["round"], got["amount"], got["date"]) == (round_, amount, when)


@pytest.mark.parametrize("html", [
    "<p>Nous accompagnons les startups dans leur levée de fonds.</p>",
    "<p>Nous avons aidé nos clients à lever plus de 50 M€.</p>",
    "<p>Nous avons levé 50 M€ pour nos clients.</p>",
    "<p>Acme lève un fonds de 100 M€ pour les startups.</p>",
    "<p>Acme annonce un partenariat avec Foo.</p>",
    "<p>Acme a réalisé 2 M€ de chiffre d'affaires en 2024.</p>",
    "<p>Acme annonce 3 M€ de CA.</p>",
    "<p>Nous avons levé 40 000 € pour une association.</p>",
    "<p>Comment réussir sa levée de fonds en 2026</p>",
    "<p>Acme a obtenu une subvention de 200 k€.</p>",
    "<p>Acme a investi 2 M€ dans la startup Foo.</p>",
    "<p>Acme annonce un contrat de 5 M€.</p>",
])
def test_raising_talk_in_french_that_isnt_their_own_raise(html):
    assert funding_round(html, domain="acme.fr") is None


def test_the_english_detector_is_unchanged_by_french():
    got = funding_round("<p>We raised a $4.5 million seed round led by Foo Ventures in March 2025.</p>",
                        domain="acme.com")
    assert (got["round"], got["amount"], got["date"]) == ("Seed", "$4.5M", "2025-03")
    assert funding_round("<p>We raised $40,000 for charity last year.</p>", domain="acme.com") is None


# ── a careers page in French ──


@pytest.mark.parametrize("anchor", ["Recrutement", "Carrières", "Nous rejoindre", "Rejoignez-nous",
                                    "On recrute", "Offres d'emploi"])
def test_french_careers_links(anchor):
    assert _CAREERS_ANCHOR.match(anchor)


def test_french_careers_paths():
    assert {"recrutement", "carrieres", "nous-rejoindre"} <= _CAREERS_SEGMENTS


# ── the presets, in French ──


def test_presets_search_in_english_by_default():
    queries = build_queries(_config(), preset="funding")
    assert [q.language for q in queries] == [""]
    assert "we raised" in queries[0].term


def test_french_adds_its_own_searches():
    queries = build_queries(_config(["en", "fr"]), preset="funding")
    assert [q.language for q in queries] == ["", "fr"]
    assert "intitle:lève" in queries[1].term and "after:" in queries[1].term
    hiring = build_queries(_config(["fr"]), preset="hiring")
    assert [q.language for q in hiring] == ["fr"]
    assert hiring[0].term.startswith('"cto" (recrutement')


def test_a_preset_with_no_french_version_searches_in_english():
    queries = build_queries(_config(["fr"]), preset="yc")
    assert queries and all(q.language == "" for q in queries)


def test_french_searches_stay_within_googles_32_words():
    for preset in ("funding", "hiring"):
        [q] = build_queries(_config(["fr"], industries=["B2B SaaS startups"]), preset=preset)
        assert len(q.term.split()) <= 32


def test_search_languages_are_checked():
    base = dict(industries=["x"], company_size="1-10", titles=["CEO"], geography=["France"])
    assert ICPConfig(**base).search_languages == ["en"]
    assert ICPConfig(**base, search_languages=["FR", "en", "fr"]).search_languages == ["fr", "en"]
    with pytest.raises(ValueError):
        ICPConfig(**base, search_languages=["de"])


@pytest.mark.parametrize("domain", ["maddyness.com", "www.welcometothejungle.com", "malt.fr",
                                    "pappers.fr", "lesechos.fr"])
def test_french_press_and_job_boards_are_junk(domain):
    assert is_junk(domain)


def test_a_french_startup_is_not_junk():
    assert not is_junk("acme.fr")


# ── Serper ──


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


SERPER_HIT = {"organic": [{"title": "Acme", "link": "https://acme.fr/presse", "position": 1}]}


@pytest.mark.asyncio
async def test_serper_searches_google_france_in_french():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=SERPER_HIT)

    async with _client(handler) as client:
        await D.Serper().fetch(client, {"serper_api_key": "k"},
                               D.DiscoveryQuery(term="fintech levée", language="fr"))
        await D.Serper().fetch(client, {"serper_api_key": "k"}, D.DiscoveryQuery(term="fintech"))
    assert sent[0]["gl"] == "fr" and sent[0]["hl"] == "fr"
    assert "gl" not in sent[1] and "hl" not in sent[1]


@pytest.mark.asyncio
async def test_serper_free_accounts_get_the_first_ten_results():
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body["num"])
        if body["num"] > 10:
            return httpx.Response(400, json={"message": "Query pattern not allowed for free accounts.",
                                             "statusCode": 400})
        return httpx.Response(200, json=SERPER_HIT)

    async with _client(handler) as client:
        out = await D.Serper().fetch(client, {"serper_api_key": "k"},
                                     D.DiscoveryQuery(term="x after:2025-01-01", depth=30))
    assert sent == [30, 10]
    assert out[0].domain == "acme.fr"


@pytest.mark.asyncio
async def test_serper_other_bad_requests_still_fail():
    async with _client(lambda r: httpx.Response(400, json={"message": "Invalid query"})) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await D.Serper().fetch(client, {"serper_api_key": "k"}, D.DiscoveryQuery(term="x", depth=30))


def test_dataforseo_searches_france_for_a_french_query():
    assert D._dfs_location(D.DiscoveryQuery(term="x", language="fr"), "") == {"location_code": 2250}
    assert D._dfs_location(D.DiscoveryQuery(term="x"), "") == {"location_code": D.DEFAULT_LOCATION_CODE}

"""Signals a freelance tech lead sells on: a role open for weeks, remote,
the stack the company uses, and a team with nobody technical.

Every business and person here is invented.
"""

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey.collectors.ats import parse_feed
from harvey.collectors.profile import ProfileCollector, stack_matches, tech_leadership
from harvey.models.company import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.startups import hiring_facts
from harvey.state import StateManager

ICP = SimpleNamespace(stack_keywords=["NestJS", "Next.js", "Kafka", "Kubernetes", "MongoDB"])
HOME = ('<html><body>' + "x" * 600 + '<a href="https://jobs.lever.co/lumenhexa">Jobs</a></body></html>')
ALL = {"HIRING_ROLE", "OPEN_ROLES", "ROLE_OPEN_DAYS", "REMOTE_ROLE", "STACK_MATCH", "NO_TECH_LEADER"}


def _days_ago(n: int) -> int:
    """Epoch milliseconds n days ago, as Lever dates a posting."""
    return int((datetime.now(timezone.utc) - timedelta(days=n)).timestamp() * 1000)


def _lever(*jobs):
    return [{"id": str(i), "text": title, "categories": {"location": place}, "createdAt": _days_ago(age),
             "hostedUrl": f"https://jobs.lever.co/lumenhexa/{i}", "workplaceType": kind,
             "descriptionPlain": text, "lists": [{"text": "Stack", "content": "<li>Kafka</li>"}]}
            for i, (title, place, age, kind, text) in enumerate(jobs)]


def _collector(feed, pages=None, confirmed=ALL, keywords=("lead developer", "cto"), icp=ICP):
    c = ProfileCollector(None, set(confirmed), role_keywords=list(keywords), icp=icp)
    pages = {"https://lumenhexa.example": HOME, **(pages or {})}

    async def fake_get(url):
        return pages.get(url, "")

    def handler(request):
        if str(request.url) == "https://api.lever.co/v0/postings/lumenhexa?mode=json":
            return httpx.Response(200, json=feed)
        return httpx.Response(404, json={})

    c._get = fake_get
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _rows(obs, code):
    return [(o["value_num"], o["value_text"]) for o in obs if o["signal_code"] == code]


# ── job-board feeds carry text and remote ──

def test_feeds_keep_description_text_and_remote():
    lever = parse_feed("lever", _lever(("Lead Developer", "Toulouse", 3, "remote", "Node, NestJS")))
    assert lever[0].remote and "NestJS" in lever[0].text and "Kafka" in lever[0].text
    ashby = parse_feed("ashby", {"jobs": [{"title": "CTO", "isRemote": True,
                                           "descriptionPlain": "Next.js et Kafka"}]})
    assert ashby[0].remote and ashby[0].text == "Next.js et Kafka"
    greenhouse = parse_feed("greenhouse", {"departments": [{"name": "Tech", "jobs": [
        {"id": 1, "title": "Tech Lead (télétravail)", "location": {"name": "Paris"},
         "content": "&lt;p&gt;MongoDB&lt;/p&gt;"}]}]})
    assert greenhouse[0].remote and greenhouse[0].text == "MongoDB"
    office = parse_feed("workable", {"jobs": [{"title": "Lead Developer", "city": "Lyon"}]})
    assert not office[0].remote


# ── stack matching ──

def test_stack_spellings_and_aliases():
    texts = ["We use next js, NodeJS and k8s", "Mongo + Kafka"]
    assert stack_matches(ICP.stack_keywords, texts) == ["Next.js", "Kafka", "Kubernetes", "MongoDB"]
    assert stack_matches(["NestJS"], ["Nest.js on the backend"]) == ["NestJS"]
    assert stack_matches(["Node.js"], ["we go to the next node of the graph"]) == []
    assert stack_matches(["Go"], ["Go and Rust"]) == ["Go"]


# ── tech leadership on a team page ──

def test_tech_leadership_verdicts():
    founders = [{"title": "Co-fondatrice & Présidente"}, {"title": "Directeur Général"}]
    assert tech_leadership(founders)[0] == 1
    assert tech_leadership(founders + [{"title": "Lead Developer"}]) == (0, "Lead Developer")
    assert tech_leadership([{"title": "Founder & CTO"}]) == (0, "Founder & CTO")
    assert tech_leadership([{"title": "Office Manager"}]) is None
    assert tech_leadership([]) is None


# ── the profile stage ──

@pytest.mark.asyncio
async def test_a_role_open_for_weeks_remote_in_your_stack():
    feed = _lever(("Lead Developer", "Remote", 63, "remote", "NestJS and Kafka"),
                  ("Lead Developer Mobile", "Toulouse", 10, "onsite", ""),
                  ("Sales Manager", "Paris", 200, "onsite", "Kubernetes"))
    obs = await _collector(feed).profile_company("c1", "lumenhexa.example")
    days = _rows(obs, "ROLE_OPEN_DAYS")
    assert len(days) == 1 and days[0][0] == 63 and days[0][1].startswith("Lead Developer · Remote")
    assert _rows(obs, "REMOTE_ROLE")[0][0] == 1
    # Every role's text counts for the stack, not only the matching ones.
    assert _rows(obs, "STACK_MATCH") == [(3, "NestJS, Kafka, Kubernetes")]


@pytest.mark.asyncio
async def test_no_relevant_role_says_nothing_about_age_or_remote():
    obs = await _collector(_lever(("Sales Manager", "Paris", 90, "remote", ""))).profile_company(
        "c1", "lumenhexa.example")
    assert _rows(obs, "ROLE_OPEN_DAYS") == [] and _rows(obs, "REMOTE_ROLE") == []
    assert _rows(obs, "STACK_MATCH") == [(1, "Kafka")]   # from the Lever list


@pytest.mark.asyncio
async def test_an_onsite_role_is_not_remote():
    obs = await _collector(_lever(("CTO", "Lyon", 5, "onsite", ""))).profile_company("c1", "lumenhexa.example")
    assert _rows(obs, "REMOTE_ROLE") == [(0, "")]


@pytest.mark.asyncio
async def test_stack_from_the_homepage_framework_with_no_board():
    home = '<html><script id="__NEXT_DATA__"></script>' + "x" * 600 + "</html>"
    c = _collector([], pages={"https://lumenhexa.example": home}, confirmed={"STACK_MATCH", "TECH_STACK"})
    obs = await c.profile_company("c1", "lumenhexa.example")
    assert _rows(obs, "STACK_MATCH") == [(1, "Next.js")]


@pytest.mark.asyncio
async def test_no_stack_keywords_no_stack_row():
    obs = await _collector(_lever(("CTO", "Lyon", 5, "onsite", "Kafka")), icp=SimpleNamespace()).profile_company(
        "c1", "lumenhexa.example")
    assert _rows(obs, "STACK_MATCH") == []


@pytest.mark.asyncio
async def test_team_page_with_founders_and_no_one_technical():
    team = ("<html>" + "x" * 600 + "<h3>Hélène MARCHAND</h3><p>Co-fondatrice &amp; Présidente</p>"
            "<h3>Paul Girard</h3><p>Directeur Général</p></html>")
    c = _collector([], pages={"https://lumenhexa.example/equipe": team}, confirmed={"NO_TECH_LEADER"})
    obs = await c.profile_company("c1", "lumenhexa.example")
    rows = [(o["value_num"], o["confidence"]) for o in obs if o["signal_code"] == "NO_TECH_LEADER"]
    assert rows == [(1, 0.7)]
    assert "Hélène" not in obs[0]["value_text"]          # no names in the verdict


# ── what the writer is told ──

@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


@pytest.mark.asyncio
async def test_hiring_facts_for_the_writer(state):
    await state.add_company(Company(name="Lumenhexa", domain="lumenhexa.example"))
    cid = (await state.get_company_by_domain("lumenhexa.example")).id
    assert await hiring_facts(state, cid) == []
    await state.add_observations([
        {"company_id": cid, "signal_code": "ROLE_OPEN_DAYS", "value_num": 50,
         "value_text": "Lead Developer · Remote", "observed_at": "2026-09-01 08:00:00"},
        {"company_id": cid, "signal_code": "REMOTE_ROLE", "value_num": 1},
        {"company_id": cid, "signal_code": "STACK_MATCH", "value_num": 2, "value_text": "NestJS, Kafka"},
        {"company_id": cid, "signal_code": "NO_TECH_LEADER", "value_num": 1},
    ])
    facts = await hiring_facts(state, cid, today=date(2026, 9, 11))
    assert facts[0] == ("- Hiring: Lead Developer · Remote; open about 60 day(s) on their job board, "
                        "as the board says")
    assert any("remote" in f for f in facts[1:])
    assert any("NestJS, Kafka" in f for f in facts)
    assert any("nobody in a technical role" in f for f in facts)


def test_catalog_has_the_new_signals():
    codes = {s["code"] for s in SIGNAL_CATALOG}
    assert {"ROLE_OPEN_DAYS", "REMOTE_ROLE", "STACK_MATCH", "NO_TECH_LEADER", "ICP_EXCLUDED"} <= codes

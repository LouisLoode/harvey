"""Company names without what a page wraps around them: press-release tags,
taglines and headlines. Every business here is invented."""

import os
import tempfile

import pytest
import pytest_asyncio

from harvey.collectors.discover import business_name, clean_company_name, site_name_from_html
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest.mark.parametrize("name, domain, expected", [
    ("[Communiqué de Presse] Lumenhexa", "lumenhexa.example", "Lumenhexa"),
    ("[CP] [Exclusif] Lumenhexa", "lumenhexa.example", "Lumenhexa"),
    ("Press release: Lumenhexa", "lumenhexa.example", "Lumenhexa"),
    ("Lumenhexa (communiqué)", "lumenhexa.example", "Lumenhexa"),
    ("Lumenhexa, the data API for verified records", "lumenhexa.example", "Lumenhexa"),
    ("Brindille, le média de la performance commerciale", "brindille.example", "Brindille"),
    ("Lumenhexa lève 2,7 M€ pour devenir le leader", "lumenhexa.example", "Lumenhexa"),
])
def test_wrappers_come_off(name, domain, expected):
    assert clean_company_name(name, domain) == expected


@pytest.mark.parametrize("name, domain", [
    ("Smith, Jones & Co", "smithjones.example"),          # a comma in a real name
    ("Corvane, the data API", "otherdomain.example"),      # the domain doesn't back "Corvane"
    ("Tech:NYC Blog", "technyc.example"),
    ("[Communiqué]", "lumenhexa.example"),                 # nothing would be left
])
def test_real_names_stay(name, domain):
    assert clean_company_name(name, domain) == name


def test_search_titles_and_homepages_are_cleaned():
    assert business_name("[Communiqué de Presse] Lumenhexa", "lumenhexa.example") == "Lumenhexa"
    html = '<meta property="og:site_name" content="Lumenhexa, le planning des tournées en un clic">'
    assert site_name_from_html(html, "lumenhexa.example") == "Lumenhexa"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


@pytest.mark.asyncio
async def test_existing_names_are_repaired_on_start(state):
    await state.add_company(Company(name="[Communiqué de Presse] Lumenhexa", domain="lumenhexa.example"))
    cid = (await state.get_company_by_domain("lumenhexa.example")).id
    await state.add_prospect(Prospect(first_name="Claire", last_name="Duval", title="CEO",
                                      email="claire@lumenhexa.example", company_id=cid,
                                      company="[Communiqué de Presse] Lumenhexa"))
    await state.add_company(Company(name="Smith, Jones & Co", domain="smithjones.example"))
    await state.init_db()
    assert (await state.get_company(cid)).name == "Lumenhexa"
    assert (await state.get_prospects_by_status("new"))[0].company == "Lumenhexa"
    assert (await state.get_company_by_domain("smithjones.example")).name == "Smith, Jones & Co"

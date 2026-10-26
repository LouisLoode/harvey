"""French team pages: accented names, surnames in capitals, French titles.

Every person and business here is invented.
"""

from harvey.collectors import people
from harvey.collectors.profile import ProfileCollector, title_looks_like_role
from harvey.integrations.email_finder import _clean_name_part, build_email


def test_accented_names_are_names():
    assert people.split_person_name("Hélène Marchand") == ("Hélène", "Marchand")
    assert people.split_person_name("Éric Lefèvre-Dumas") == ("Éric", "Lefèvre-Dumas")


def test_a_surname_in_capitals_is_read_and_tidied():
    assert people.split_person_name("Claire DUVAL") == ("Claire", "Duval")
    assert people.split_person_name("Jean-Pierre MARTIN-ROUX") == ("Jean-Pierre", "Martin-Roux")


def test_acronyms_and_initials_in_capitals_are_still_not_surnames():
    assert people.split_person_name("Ray HVAC") is None
    assert people.split_person_name("RAY T") is None
    assert people.split_person_name("Agence SARL") is None


def test_french_page_headings_are_not_names():
    for heading in ("Notre Équipe", "Nos Valeurs", "Mentions Légales", "Rejoignez Nous"):
        assert people.split_person_name(heading) is None, heading


def test_french_titles_read_as_roles_and_owners():
    for title in ("Co-fondatrice & Présidente", "Gérant", "Fondateur et PDG", "Directrice Générale"):
        assert title_looks_like_role(title), title
        assert people.is_owner_title(title), title
    assert title_looks_like_role("Directeur Technique")
    assert not people.is_owner_title("Directeur Technique")
    assert not people.is_owner_title("Vice-présidente des ventes")
    assert not people.is_owner_title("Assistante de direction")


def test_a_french_icp_title_matches_any_owner_title():
    assert people.title_matches("Co-fondatrice & Présidente", ["Fondateur", "Président"])
    assert people.title_matches("Founder & CEO", ["Fondateur"])


def test_a_french_title_names_another_company_only_through_chez():
    assert not people.title_names_other_org("Directeur Technique", "lumenhexa.example")
    assert not people.title_names_other_org("Président Directeur Général", "lumenhexa.example")
    assert people.title_names_other_org("Fondatrice chez Brindille Studio", "lumenhexa.example")


def test_team_page_with_french_cards():
    html = ("<h3>Hélène MARCHAND</h3><p>Co-fondatrice &amp; Présidente</p>"
            "<h3>Hugo Bernard</h3><p>Directeur Technique</p>"
            "<h3>Notre Équipe</h3><p>Responsable de tout</p>")
    found = ProfileCollector(None).detect_people(html, domain="lumenhexa.example")
    assert [(p["first_name"], p["last_name"], p["is_decision_maker"]) for p in found] == [
        ("Hélène", "Marchand", True), ("Hugo", "Bernard", True)]
    ranked = people.find_people([("https://lumenhexa.example/equipe", html)], "lumenhexa.example")
    assert ranked[0]["first_name"] == "Hélène" and ranked[0]["owner"]


def test_a_founder_named_in_a_french_sentence():
    found = people.people_from_prose(
        "<p>Lumenhexa a été fondée en 2021 par Claire Duval et son associé.</p>", "lumenhexa.example")
    assert [(p["first_name"], p["last_name"], p["owner"]) for p in found] == [("Claire", "Duval", True)]


def test_email_addresses_drop_accents_not_letters():
    assert _clean_name_part("François") == "francois"
    assert build_email("{f}.{l}", "Hélène", "Lefèvre", "lumenhexa.example") == "helene.lefevre@lumenhexa.example"

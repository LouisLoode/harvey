"""Which language to write to a company in: French when it is French.

``channels.email.language`` is "auto" (the default), "en" or "fr". On auto,
a company is written to in French when:
  - its domain is .fr;
  - its location is in France or a French-speaking region
    (discovery stores the place it searched, e.g. "Toulouse, France");
  - the French company registry knows it (its site published a SIREN);
  - its site declares French (SITE_LANGUAGE, <html lang="fr">);
  - or what it says about itself (its description) is in French.
Everyone else gets English, as before.

``text_language`` reads which language an email already is in, so its
footer (the opt-out line) is in the same one.
"""

import re
import unicodedata

LANGUAGES = ("en", "fr")
_REGISTRY_CODES = ("NAF_CODE", "COMPANY_AGE", "REGISTRY_VERIFIED", "HEADCOUNT_BAND")
_FRENCH_PLACES = re.compile(
    r"\b(?:france|belgique|wallonie|bruxelles|suisse romande|romandie|geneve|lausanne|"
    r"quebec|montreal|luxembourg|monaco|francophone|paris|lyon|marseille|toulouse|"
    r"bordeaux|lille|nantes|nice|strasbourg|montpellier|rennes|grenoble|occitanie|"
    r"ile-de-france|auvergne|bretagne|normandie|provence)\b")

# Words that are common in one language and rare in the other.
_FR_WORDS = frozenset("""vous votre vos nous est les des une pour avec dans pas que qui
sur sont mais aussi cette ces leur bonjour merci""".split())
_EN_WORDS = frozenset("""you your the and is are for with this that have not but our
their would could hi thanks""".split())

# What the writer is told when the email is to be in French.
FRENCH_RULES = """LANGUAGE: write this email in French — subject and body.
- Vouvoiement ("vous"), never "tu". Natural spoken French, as one founder writes
  to another; not translated English.
- No stock French cold-email phrases: "Je me permets de vous contacter",
  "N'hésitez pas à", "Dans un monde où", "Je reviens vers vous", "Suite à",
  "Cordialement" as a closing, "J'espère que vous allez bien", "Je me permets de
  vous relancer", "Suite à notre échange" when there was none.
- An easy way out reads naturally in French: "ça vaut un échange rapide, ou ce
  n'est pas un sujet chez vous ?"
- Greeting: "Bonjour <first name>," ("Bonjour," for a company's general inbox).
- Keep the same rules as above: short, one observation, one question, no pitch.
- French punctuation: a space before ? ! : ; and « » for quotes."""


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return plain.lower()


def configured_language(config) -> str:
    email = getattr(getattr(config, "channels", None), "email", None)
    value = str(getattr(email, "language", "auto") or "auto").lower()
    return value if value in LANGUAGES else "auto"


async def company_language(state, config, company_id: str = "", domain: str = "",
                           location: str = "", description: str = "") -> str:
    """"fr" or "en" for one company (see the module docstring)."""
    chosen = configured_language(config)
    if chosen != "auto":
        return chosen
    if (domain or "").lower().rstrip(".").endswith(".fr"):
        return "fr"
    if _FRENCH_PLACES.search(_fold(location)):
        return "fr"
    lookup = getattr(state, "get_observations", None)
    if company_id and lookup is not None:
        try:
            for code in _REGISTRY_CODES:
                if await lookup(company_id=company_id, signal_code=code, latest_only=True, limit=1):
                    return "fr"
            declared = await lookup(company_id=company_id, signal_code="SITE_LANGUAGE",
                                    latest_only=True, limit=1)
            if declared and (declared[0].get("value_text") or "").lower() == "fr":
                return "fr"
        except Exception:
            pass
    if len((description or "").split()) >= 8 and text_language(description) == "fr":
        return "fr"
    return "en"


def text_language(text: str) -> str:
    """"fr" when the text reads as French, else "en"."""
    words = re.findall(r"[a-zà-ÿœ']+", (text or "").lower())
    fr = sum(1 for w in words if w in _FR_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    return "fr" if fr > en else "en"

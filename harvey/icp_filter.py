"""What the ICP rules out: competitors, and businesses that never buy.

``icp.exclude_keywords`` are words that disqualify a company when its own
name, page title or description uses them ("ESN", "cabinet de recrutement",
"venture capital"). ``icp.exclude_naf`` are French activity-code prefixes
("78." for staffing, "64.30" for investment funds), read from the company
registry. ``icp.exclude_kinds`` names whole kinds of site (job boards,
recruiters, media), each recognised by how its homepage describes itself.
All three are the user's own choice, so applying them needs no confirmed
signal: setting them is the confirmation.

A kind the ICP doesn't rule out is still worth knowing: a recruiter or a
media site is written to in a campaign of its own (see the Writer), since
what you offer a recruitment firm isn't what you offer a startup.

Matching is on whole words, ignoring case and accents: "agence web" matches
"Agence Web" and "AGENCE WEB", never "agence webinaire".
"""

import re
import unicodedata

# How a site that is one of these kinds describes itself, in its homepage
# title or description. Phrases, not single words: a careers page says "jobs"
# and an HR software sells "recrutement", and neither is a job board. Kinds
# are matched against the homepage only, never a search result's title,
# which for a hiring search is a real company's careers page.
SITE_KINDS = {
    "job_boards": (
        "job board", "jobboard", "job portal", "job site", "job listings",
        "job platform", "job search engine", "find jobs", "find a job",
        "find your next job", "find your dream job", "search jobs",
        "remote jobs", "tech jobs", "apply to jobs", "applies to jobs",
        "resume builder", "cv builder",
        "offres d'emploi", "offre d'emploi", "site d'emploi", "site emploi",
        "trouvez un emploi", "trouver un emploi", "trouvez votre emploi",
        "trouvez un job", "trouvez votre job", "trouver un job",
        "recherche d'emploi", "premier emploi", "emploi, recrutement",
        "emploi et recrutement", "entreprises recrutent", "faites-vous chasser",
    ),
    "recruiters": (
        "recruitment agency", "recruitment firm", "recruiting agency",
        "recruiting firm", "staffing agency", "staffing firm",
        "talent acquisition agency", "headhunter", "headhunters",
        "headhunting", "executive search", "tech recruiters", "it recruitment",
        "cabinet de recrutement", "agence de recrutement",
        "chasseur de tête", "chasseurs de tête", "chasseur de têtes",
        "chasseurs de têtes", "chasse de têtes", "chasse de talents",
        "recrutement tech", "recrutement it", "recrutement informatique",
        "agence d'intérim", "société d'intérim", "travail temporaire",
    ),
    "media": (
        "magazine", "online magazine", "news site", "media company",
        "startup media", "startup news", "tech news", "funding news",
        "funded startups", "newly funded", "startup database",
        "startup intelligence", "startup interviews", "startups to watch",
        "trending topics", "growth community",
        "le média des", "le média de", "média en ligne", "média communautaire",
        "portail d'information", "portail d'informations", "toute l'actualité",
        "actualité des startups", "actualités des startups",
        "base de données des startups", "base de données startup",
        "annuaire des startups", "annuaire des startup",
    ),
}

# How each kind reads in a reason: "its site reads like a job board".
KIND_LABELS = {"job_boards": "a job board", "recruiters": "a recruiter", "media": "a media site"}


def _fold(text: str) -> str:
    # Curly apostrophes first: "offres d’emploi" is how most sites write it,
    # and NFKD would drop the ’ rather than make it a '.
    text = re.sub(r"[’‘ʼ]", "'", text or "")
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    # "start-up" and "start up" are how French sites spell it.
    plain = re.sub(r"\bstart[\s-]+up", "startup", plain.lower())
    return re.sub(r"\s+", " ", plain).strip()


def _words(icp, name: str) -> list[str]:
    return [w.strip() for w in (getattr(icp, name, None) or [])
            if isinstance(w, str) and w.strip()]


def _first_used(phrases, texts) -> str:
    """The first of ``phrases`` any of the texts uses as whole words, or ""."""
    blob = " | ".join(_fold(t) for t in texts if t)
    if not blob:
        return ""
    for phrase in phrases:
        folded = _fold(phrase)
        if folded and re.search(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])", blob):
            return phrase
    return ""


def has_exclusions(icp) -> bool:
    return bool(_words(icp, "exclude_keywords") or _words(icp, "exclude_naf")
                or _words(icp, "exclude_kinds"))


def excluded_by(icp, *texts: str) -> str:
    """The first exclude_keywords entry any of the texts uses, or ""."""
    return _first_used(_words(icp, "exclude_keywords"), texts)


def site_kind(*texts: str, kinds=None) -> tuple[str, str]:
    """(kind, phrase) when the texts describe a site of one of ``kinds``
    (default: every kind in SITE_KINDS), else ("", "")."""
    for kind in kinds if kinds is not None else SITE_KINDS:
        phrase = _first_used(SITE_KINDS.get(kind, ()), texts)
        if phrase:
            return kind, phrase
    return "", ""


def kind_excluded(icp, *texts: str) -> tuple[str, str]:
    """(kind, phrase) when the texts describe a site of one of
    icp.exclude_kinds, else ("", "")."""
    return site_kind(*texts, kinds=_words(icp, "exclude_kinds"))


def site_excluded(icp, *texts: str) -> str:
    """Why a company's own homepage (its name, title, description) rules it
    out under the ICP, or "". The reason ends up in ICP_EXCLUDED."""
    hit = excluded_by(icp, *texts)
    if hit:
        return f"its site says \"{hit}\""
    kind, phrase = kind_excluded(icp, *texts)
    if kind:
        return f"its site reads like {KIND_LABELS[kind]} (\"{phrase}\")"
    return ""


def _naf_key(code: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (code or "").upper())


def naf_excluded(icp, naf: str) -> str:
    """The exclude_naf prefix this activity code falls under, or ""."""
    key = _naf_key(naf)
    if not key:
        return ""
    for prefix in _words(icp, "exclude_naf"):
        wanted = _naf_key(prefix)
        if wanted and key.startswith(wanted):
            return prefix
    return ""

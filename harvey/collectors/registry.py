"""The French company registry: who legally runs a company, and what it is.

Cost: $0, one request. ``recherche-entreprises.api.gouv.fr`` is the French
State's open search over the SIRENE and RNE registries (open licence, no
key, about 7 requests a second). Harvey asks it about a company only when
the company's own site publishes its SIREN, which French law requires in
the mentions légales, and accepts the answer only for that exact number. A
name search could return a namesake, and its directors would be someone
else's.

What it gives: creation date, headcount band, activity code (NAF), whether
the company has closed, and its legal directors. Directors are kept only
when they are people (not another company), only in a director's role (not
the auditor), and only when the company's record is public
(``statut_diffusion`` "O"). Birth dates are never read into Harvey.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger("harvey.collectors.registry")

API_URL = "https://recherche-entreprises.api.gouv.fr/search"
REGISTRY_SIGNALS = ("COMPANY_AGE", "HEADCOUNT_BAND", "NAF_CODE", "REGISTRY_VERIFIED")
# Where a French site keeps its legal notice, when the homepage doesn't link it.
LEGAL_PATHS = ("/mentions-legales", "/mentions-legales/", "/legal", "/mentions")
_LEGAL_LINK = re.compile(r"(?i)mentions[\s-]*l[ée]gales|legal[\s-]*notice|\blegal\b|informations l[ée]gales")

# A SIREN is announced: "SIREN 123 456 789", "RCS Toulouse B 123 456 789",
# "SIRET : 123 456 789 00012", "N° TVA FR12 123456789".
_ANNOUNCED = re.compile(
    r"(?i)\b(?:siren|siret|r\.?\s?c\.?\s?s\.?|immatricul\w*|num[ée]ro\s+d['’]identification)\b"
    r"[^0-9]{0,40}?(\d(?:[\s  .]?\d){8}(?:[\s  .]?\d{5})?)(?!\d)")
_VAT = re.compile(r"(?i)\bFR\s?[0-9A-Z]{2}\s?(\d{3}\s?\d{3}\s?\d{3})(?!\d)")

# INSEE headcount bands: code -> (label, lowest headcount in the band)
HEADCOUNT_BANDS = {
    "00": ("0 salarié", 0), "01": ("1–2 salariés", 1), "02": ("3–5 salariés", 3),
    "03": ("6–9 salariés", 6), "11": ("10–19 salariés", 10), "12": ("20–49 salariés", 20),
    "21": ("50–99 salariés", 50), "22": ("100–199 salariés", 100), "31": ("200–249 salariés", 200),
    "32": ("250–499 salariés", 250), "41": ("500–999 salariés", 500), "42": ("1 000–1 999 salariés", 1000),
    "51": ("2 000–4 999 salariés", 2000), "52": ("5 000–9 999 salariés", 5000),
    "53": ("10 000 salariés et plus", 10000),
}
# The roles that run a company. The auditor (commissaire aux comptes) and a
# supervisory board member are on the record too, and are not who decides.
_DIRECTOR_ROLE = re.compile(r"(?i)\b(?:pr[ée]sident|directeur g[ée]n[ée]ral|directrice g[ée]n[ée]rale|"
                            r"g[ée]rant|pdg|dirigeant)")
_NOT_DIRECTOR = re.compile(r"(?i)commissaire|surveillance|suppl[ée]ant|liquidateur")
# "Président de SAS" is a president; the legal form is the registry's detail.
_LEGAL_FORM_TAIL = re.compile(r"(?i)\s+(?:de|d['’])\s*(?:la\s+)?(?:sas|sasu|sarl|eurl|sa|sca|snc|soci[ée]t[ée]\b.*)$")


@dataclass
class Director:
    first_name: str
    last_name: str
    role: str

    @property
    def label(self) -> str:
        return f"{self.first_name} {self.last_name} — {self.role}"


@dataclass
class RegistryCompany:
    siren: str
    name: str = ""
    created: str = ""            # YYYY-MM-DD
    headcount_code: str = ""
    headcount_year: str = ""
    naf: str = ""
    closed: bool = False
    public: bool = False
    directors: list[Director] = field(default_factory=list)

    @property
    def headcount(self) -> tuple[str, int] | None:
        return HEADCOUNT_BANDS.get(self.headcount_code)

    def age_years(self, today: date | None = None) -> float | None:
        try:
            created = date.fromisoformat(self.created)
        except ValueError:
            return None
        return round(max(0, ((today or date.today()) - created).days) / 365.25, 1)


def annuaire_url(siren: str) -> str:
    """The company's public page on the State's company directory."""
    return f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}"


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _page_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    return unescape(re.sub(r"(?s)<[^>]+>", " ", text))


def find_siren(html: str) -> str:
    """The SIREN a page publishes about its company, or "". Only a number
    announced as one (SIREN, SIRET, RCS, VAT) that passes its check digit."""
    text = _page_text(html)
    for m in [*_ANNOUNCED.finditer(text), *_VAT.finditer(text)]:
        digits = re.sub(r"\D", "", m.group(1))
        siren = digits[:9]
        if len(digits) in (9, 14) and siren != "000000000" and luhn_ok(siren):
            return siren
    return ""


def legal_links(html: str, site: str) -> list[str]:
    """The site's own links to its legal notice, as absolute URLs."""
    host = (urlparse(site).hostname or "").removeprefix("www.")
    out = []
    for m in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html or ""):
        href, anchor = m.group(1).strip(), unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(2)))
        if not (_LEGAL_LINK.search(anchor) or _LEGAL_LINK.search(href)):
            continue
        url = urljoin(site + "/", href)
        link_host = (urlparse(url).hostname or "").removeprefix("www.")
        if urlparse(url).scheme in ("http", "https") and link_host == host and url not in out:
            out.append(url)
    return out[:2]


def looks_french(html: str, domain: str) -> bool:
    """Worth looking for a legal notice: a .fr site, or one in French."""
    return (domain.endswith(".fr")
            or bool(re.search(r"""(?i)<html\b[^>]*\blang=["']?fr""", html or ""))
            or bool(_LEGAL_LINK.search(html or "") and re.search(r"(?i)mentions", html or "")))


def _tidy_name(text: str) -> str:
    """Registry names come in capitals ("DUVAL", "MARTIN-ROUX")."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if text.isupper():
        text = " ".join("-".join(p.capitalize() for p in w.split("-")) for w in text.lower().split())
    return text


def parse_company(result: dict, siren: str) -> RegistryCompany | None:
    """One search result, read into what Harvey keeps; None unless it is
    the company with exactly this SIREN."""
    if not isinstance(result, dict) or str(result.get("siren") or "") != siren:
        return None
    company = RegistryCompany(
        siren=siren,
        name=str(result.get("nom_complet") or result.get("nom_raison_sociale") or ""),
        created=str(result.get("date_creation") or "")[:10],
        headcount_code=str(result.get("tranche_effectif_salarie") or ""),
        headcount_year=str(result.get("annee_tranche_effectif_salarie") or ""),
        naf=str(result.get("activite_principale") or ""),
        closed=result.get("etat_administratif") == "C",
        public=result.get("statut_diffusion") == "O",
    )
    if not company.public:
        return company
    for d in result.get("dirigeants") or []:
        if not isinstance(d, dict) or d.get("type_dirigeant") != "personne physique":
            continue
        role = str(d.get("qualite") or "").strip()
        if not _DIRECTOR_ROLE.search(role) or _NOT_DIRECTOR.search(role):
            continue
        first = re.split(r"[\s,]+", str(d.get("prenoms") or "").strip())[0]
        last = str(d.get("nom") or "").strip()
        if not first or not last:
            continue
        company.directors.append(Director(_tidy_name(first), _tidy_name(last),
                                          _LEGAL_FORM_TAIL.sub("", role).strip() or role))
    return company


async def lookup(client: httpx.AsyncClient, siren: str) -> RegistryCompany | None:
    """Ask the registry about one SIREN: one request, one retry when rate
    limited. None when it doesn't know the number or doesn't answer."""
    for attempt in range(2):
        try:
            resp = await client.get(API_URL, params={"q": siren, "per_page": 1},
                                    headers={"Accept": "application/json"})
        except (httpx.HTTPError, ValueError) as e:
            logger.debug(f"registry: lookup failed for {siren}: {e}")
            return None
        if resp.status_code == 429 and attempt == 0:
            await asyncio.sleep(1.5)
            continue
        if resp.status_code != 200:
            logger.debug(f"registry: {siren} → {resp.status_code}")
            return None
        try:
            results = resp.json().get("results") or []
        except (ValueError, AttributeError):
            return None
        for result in results:
            company = parse_company(result, siren)
            if company:
                return company
        return None
    return None

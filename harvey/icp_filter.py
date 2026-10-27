"""What the ICP rules out: competitors, and businesses that never buy.

``icp.exclude_keywords`` are words that disqualify a company when its own
name, page title or description uses them ("ESN", "cabinet de recrutement",
"venture capital"). ``icp.exclude_naf`` are French activity-code prefixes
("78." for staffing, "64.30" for investment funds), read from the company
registry. Both lists are the user's own choice, so applying them needs no
confirmed signal: setting them is the confirmation.

Matching is on whole words, ignoring case and accents: "agence web" matches
"Agence Web" and "AGENCE WEB", never "agence webinaire".
"""

import re
import unicodedata


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", plain.lower()).strip()


def _words(icp, name: str) -> list[str]:
    return [w.strip() for w in (getattr(icp, name, None) or [])
            if isinstance(w, str) and w.strip()]


def has_exclusions(icp) -> bool:
    return bool(_words(icp, "exclude_keywords") or _words(icp, "exclude_naf"))


def excluded_by(icp, *texts: str) -> str:
    """The first exclude_keywords entry any of the texts uses, or ""."""
    blob = " | ".join(_fold(t) for t in texts if t)
    if not blob:
        return ""
    for keyword in _words(icp, "exclude_keywords"):
        folded = _fold(keyword)
        if folded and re.search(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])", blob):
            return keyword
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

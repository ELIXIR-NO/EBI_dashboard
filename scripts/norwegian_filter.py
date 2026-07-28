#!/usr/bin/env python3
"""
norwegian_filter.py
===================
Norwegian-entry detection shared by the fetch and join stages.

A single copy of the geo/institution/email filter lives here so the two
consumers (fetch_ebi_data.py and join_ena.py) cannot drift apart.

Detection signals
-----------------
  a) Geographic indicators  (Norway, Oslo, Bergen, Tromsø, \\bNO\\b …)
  b) Institution regexes    (NTNU, Folkehelseinstituttet, …)
  c) Norwegian TLD email    (@*.no)

All three are sourced from data/institution_map.json.
"""

import json
import logging
import re

from paths import INSTITUTION_MAP

log = logging.getLogger("norwegian_filter")

_EMAIL_NO_RE = re.compile(r"@[\w.\-]+\.no\b", re.IGNORECASE)

# Non-geographic uses of "Norway" — species vernacular names (Norway spruce =
# Picea abies, Norway rat = Rattus norvegicus, etc.).  These appear in the
# title/abstract of studies submitted from anywhere in the world and must NOT,
# on their own, flag an entry as a Norwegian submission.  They are stripped from
# the text before Norwegian-signal matching, so any *other* signal (a real place
# name, a .no email, an institution name) still qualifies the entry.
FALSE_POSITIVE_RE = re.compile(
    r"\bNorway\s+(?:spruce|rat|rats|maple|lobster|pout|lemming|lemmings|haddock)\b",
    re.IGNORECASE,
)

# Module-level cache: built on first call, reused thereafter
_GEO_TOKENS_CACHE: list[str] | None = None
_INST_REGEXES_CACHE: list[re.Pattern] | None = None
_GEO_RE_CACHE: re.Pattern | None = None
_COMBINED_FILTER_CACHE: re.Pattern | None = None
_WEB_DOMAINS_CACHE: set[str] | None = None


def strip_false_positives(text: str) -> str:
    """Blank out species vernaculars like 'Norway spruce' so they can't, by
    themselves, mark an entry as Norwegian.  See FALSE_POSITIVE_RE."""
    return FALSE_POSITIVE_RE.sub(" ", text)


def load_web_domains(path=INSTITUTION_MAP) -> set[str]:
    """
    Return the set of lowercase Norwegian institution web-domains declared in the
    institution map (e.g. {"uib.no", "uio.no", …}).  Used by
    email_domain_is_norwegian() to recognise affiliations from a contact's email
    address even when no free-text affiliation string is present.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        log.warning("Institution map not found: %s – no web domains loaded", path)
        return set()
    result: set[str] = set()
    for inst in data.get("institutions", []):
        d = inst.get("web_domain")
        if d and isinstance(d, str) and d.strip():
            result.add(d.strip().lower())
    return result


def email_domain_is_norwegian(email: str, web_domains: set[str]) -> bool:
    """
    True if an email address carries a Norwegian signal, judged **only on its
    domain part** (everything after the last '@').

    Two ways to qualify:
      a) the domain ends in the Norwegian ccTLD '.no'         (e.g. *@uib.no)
      b) the domain equals, or is a sub-domain of, a known institution
         web_domain from the institution map                 (e.g. *@ous-research.no)

    Matching the domain rather than the whole address avoids false positives
    from name-like local parts (e.g. "nina.gasparoni@uni-saarland.de" must NOT
    match the NINA institution pattern).
    """
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if not domain:
        return False
    if domain == "no" or domain.endswith(".no"):
        return True
    return any(domain == wd or domain.endswith("." + wd) for wd in web_domains)


def load_geo_tokens(path=INSTITUTION_MAP) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        log.warning("Institution map not found: %s – using fallback geo tokens", path)
        return ["Norway", "Norge", "Norwegian", "Norsk", "Oslo", "Bergen",
                "Trondheim", "Tromsø", "Stavanger"]
    # Indicators are treated as regex patterns (matching the R side's NORWAY_RE),
    # so entries like "Troms[øo]" are kept rather than dropped.  We only skip
    # blanks and patterns that fail to compile, keeping Python and R detection in
    # sync instead of silently weaker on the fetch side.
    result: list[str] = []
    for t in data.get("norway_indicators", []):
        t = t.strip()
        if not t:
            continue
        try:
            re.compile(t)
        except re.error as exc:
            log.debug("Skipping invalid norway_indicator %r: %s", t, exc)
            continue
        result.append(t)
    return sorted(set(result))


def _institution_name_patterns(inst: dict) -> list[str]:
    """
    Escaped, word-bounded regexes for an institution's identifying names so an
    entry mentioning any of them counts as Norwegian, even when the curated
    `patterns` list doesn't spell that variant out:

      canonical      English name      "University of Bergen"
      canonical_no   Norwegian name    "Universitetet i Bergen"
      abbrev         abbreviation      "UiB"
      ror            ROR id            "03zga2b32" (matched, not the full URL)

    Escaping + \\b boundaries keep these literal (no accidental regex meaning,
    and "UiB" won't match inside another word).
    """
    out: list[str] = []
    for key in ("canonical", "canonical_no", "abbrev"):
        v = inst.get(key)
        if isinstance(v, str) and v.strip():
            out.append(r"\b" + re.escape(v.strip()) + r"\b")
    ror = inst.get("ror")
    if isinstance(ror, str) and ror.strip():
        ror_id = ror.strip().rstrip("/").rsplit("/", 1)[-1]   # id after last '/'
        if ror_id:
            out.append(r"\b" + re.escape(ror_id) + r"\b")
    return out


def load_institution_regexes(path=INSTITUTION_MAP) -> list[re.Pattern]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        log.warning("Institution map not found: %s – institution filter disabled", path)
        return []
    compiled: list[re.Pattern] = []
    for inst in data.get("institutions", []):
        # Curated regex patterns first, then literal name/abbrev/ROR fallbacks.
        for p in list(inst.get("patterns", [])) + _institution_name_patterns(inst):
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error as exc:
                log.debug("Skipping invalid pattern %r: %s", p, exc)
    log.info("Loaded %d institution regex patterns", len(compiled))
    return compiled


def build_geo_regex(geo_tokens: list[str]) -> re.Pattern:
    # geo_tokens are regex patterns (see load_geo_tokens), joined as a case-
    # insensitive alternation — same as R's NORWAY_RE.
    parts = list(geo_tokens)
    return re.compile("|".join(parts), re.IGNORECASE)


def make_combined_filter(geo_re: re.Pattern,
                         inst_regexes: list[re.Pattern]) -> re.Pattern:
    """A single regex OR-ing geo, institution, and Norwegian-email patterns."""
    all_patterns = [geo_re.pattern] + [p.pattern for p in inst_regexes] + \
                   [_EMAIL_NO_RE.pattern]
    return re.compile("|".join(f"(?:{p})" for p in all_patterns), re.IGNORECASE)


def get_cached_combined_filter() -> re.Pattern:
    """
    Lazily build and cache the combined Norwegian filter (geo + institutions + email).
    Subsequent calls return the cached version; the filter is built only once.
    """
    global _GEO_TOKENS_CACHE, _INST_REGEXES_CACHE, _GEO_RE_CACHE, _COMBINED_FILTER_CACHE
    if _COMBINED_FILTER_CACHE is not None:
        return _COMBINED_FILTER_CACHE
    _GEO_TOKENS_CACHE = load_geo_tokens()
    _INST_REGEXES_CACHE = load_institution_regexes()
    _GEO_RE_CACHE = build_geo_regex(_GEO_TOKENS_CACHE)
    _COMBINED_FILTER_CACHE = make_combined_filter(_GEO_RE_CACHE, _INST_REGEXES_CACHE)
    log.info("Filter cached: %d geo tokens, %d institution patterns",
             len(_GEO_TOKENS_CACHE), len(_INST_REGEXES_CACHE))
    return _COMBINED_FILTER_CACHE


def get_cached_web_domains() -> set[str]:
    """
    Lazily load and cache the set of Norwegian institution web domains.
    Subsequent calls return the cached version; the map is loaded only once.
    """
    global _WEB_DOMAINS_CACHE
    if _WEB_DOMAINS_CACHE is not None:
        return _WEB_DOMAINS_CACHE
    _WEB_DOMAINS_CACHE = load_web_domains()
    return _WEB_DOMAINS_CACHE


def is_norwegian_entry(entry: dict, combined_filter: re.Pattern) -> bool:
    """True if any field value in an EBI Search entry carries a Norwegian signal."""
    fields = entry.get("fields", {})
    parts: list[str] = []
    for vals in fields.values():
        if isinstance(vals, list):
            parts.extend(str(v) for v in vals if v is not None and str(v).strip())
        elif vals is not None and str(vals).strip():
            parts.append(str(vals))
    combined = strip_false_positives(" ".join(parts))
    if not combined.strip():
        return False
    return bool(combined_filter.search(combined))

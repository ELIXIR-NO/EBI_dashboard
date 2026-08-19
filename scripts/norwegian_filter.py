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

# Fields that carry specimen/strain/taxonomic codes and other free descriptive
# text, rather than the identity of a submitting institution or person.  Bare
# institution abbreviations (e.g. "OUS", "HUS", "INN", "USN") collide with
# unrelated codes in these fields far more often than in fields that
# structurally name an institution or submitter (center_name, broker_name,
# submitter, author, affiliation, ...).  Confirmed collisions: a Japanese
# shrew specimen "Suncus murinus Ous:KAT-227c" (OUS = Oslo University
# Hospital), a Finnish sample described "HUS-41-79" (HUS = Haukeland
# University Hospital), fungal isolates "USN-sp 20" (USN = Univ. of
# South-Eastern Norway) — none have any connection to Norway.
# See is_norwegian_entry(), which matches bare abbreviations only outside
# these fields.
SPECIMEN_LIKE_FIELDS = frozenset({
    "description", "abstract", "name", "title", "alias", "tag",
    "scientific_name", "strain", "sub_species", "isolate", "classification",
    "host", "study_keywords", "study_type", "legend", "image_name",
    "figure_sub", "figure_type", "method", "collection",
})

# Module-level cache: built on first call, reused thereafter
_GEO_TOKENS_CACHE: list[str] | None = None
_INST_REGEXES_CACHE: list[re.Pattern] | None = None
_ABBREV_REGEXES_CACHE: list[re.Pattern] | None = None
_GEO_RE_CACHE: re.Pattern | None = None
_FILTER_TIERS_CACHE: tuple[re.Pattern, re.Pattern] | None = None
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

    `abbrev` is deliberately excluded here: short institution codes (e.g. "IMR",
    "Nord") collide with unrelated words/acronyms (the IMR-90 cell line, the
    English word "Nord" in other org names) far more often than full names do.
    Curators add a bare \\babbrev\\b pattern to `patterns` explicitly when it's
    judged safe, and add a `(?=.*Norway|.*Norsk)` context guard when it isn't
    (see e.g. HI, VI, NR, NCR below) — auto-generating an unguarded fallback
    here would silently bypass that judgement call.
    """
    out: list[str] = []
    for key in ("canonical", "canonical_no"):
        v = inst.get(key)
        if isinstance(v, str) and v.strip():
            out.append(r"\b" + re.escape(v.strip()) + r"\b")
    ror = inst.get("ror")
    if isinstance(ror, str) and ror.strip():
        ror_id = ror.strip().rstrip("/").rsplit("/", 1)[-1]   # id after last '/'
        if ror_id:
            out.append(r"\b" + re.escape(ror_id) + r"\b")
    return out


def _is_bare_abbrev_pattern(pattern: str, abbrev: str | None) -> bool:
    """
    True if `pattern` is exactly \\b<abbrev>\\b with no lookahead context guard
    — the collision-prone shape curators use for a short institution code that
    hasn't been given a `(?=.*Norway|.*Norsk)` guard.  Guarded abbreviation
    patterns (e.g. "\\bVI\\b(?=.*Norway)") are NOT bare, and are treated as
    safe like any other curated pattern.
    """
    if not abbrev:
        return False
    return pattern.strip().lower() == rf"\b{re.escape(abbrev.strip())}\b".lower()


def load_institution_regexes(path=INSTITUTION_MAP) -> tuple[list[re.Pattern], list[re.Pattern]]:
    """
    Returns (safe_regexes, abbrev_regexes):

      safe_regexes   Full institution names, ROR ids, and any curated pattern
                     that already carries a context guard.  Safe to match
                     against any field, including specimen/description text.
      abbrev_regexes Bare, unguarded abbreviation patterns (e.g. "\\bOUS\\b").
                     Only safe to match against identity-bearing fields — see
                     SPECIMEN_LIKE_FIELDS and is_norwegian_entry().
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        log.warning("Institution map not found: %s – institution filter disabled", path)
        return [], []
    safe: list[re.Pattern] = []
    abbrev: list[re.Pattern] = []
    for inst in data.get("institutions", []):
        ab = inst.get("abbrev")
        # Curated regex patterns first, then literal name/abbrev/ROR fallbacks.
        for p in list(inst.get("patterns", [])) + _institution_name_patterns(inst):
            try:
                compiled = re.compile(p, re.IGNORECASE)
            except re.error as exc:
                log.debug("Skipping invalid pattern %r: %s", p, exc)
                continue
            if _is_bare_abbrev_pattern(p, ab):
                abbrev.append(compiled)
            else:
                safe.append(compiled)
    log.info("Loaded %d safe + %d bare-abbreviation institution patterns",
             len(safe), len(abbrev))
    return safe, abbrev


def build_geo_regex(geo_tokens: list[str]) -> re.Pattern:
    # geo_tokens are regex patterns (see load_geo_tokens), joined as a case-
    # insensitive alternation — same as R's NORWAY_RE.  Each token is wrapped in
    # \b...\b so a plain place name like "Bergen" only matches as a whole word
    # and not as a substring of an unrelated word (e.g. the plant genus
    # "Bergenia", or "Bodoe" inside a longer identifier).
    parts = [rf"\b{t}\b" for t in geo_tokens]
    return re.compile("|".join(parts), re.IGNORECASE)


def make_combined_filter(geo_re: re.Pattern,
                         inst_regexes: list[re.Pattern]) -> re.Pattern:
    """A single regex OR-ing geo, institution, and Norwegian-email patterns."""
    all_patterns = [geo_re.pattern] + [p.pattern for p in inst_regexes] + \
                   [_EMAIL_NO_RE.pattern]
    return re.compile("|".join(f"(?:{p})" for p in all_patterns), re.IGNORECASE)


def _ensure_filter_caches_built() -> None:
    global _GEO_TOKENS_CACHE, _INST_REGEXES_CACHE, _ABBREV_REGEXES_CACHE, _GEO_RE_CACHE
    if _GEO_RE_CACHE is not None:
        return
    _GEO_TOKENS_CACHE = load_geo_tokens()
    _INST_REGEXES_CACHE, _ABBREV_REGEXES_CACHE = load_institution_regexes()
    _GEO_RE_CACHE = build_geo_regex(_GEO_TOKENS_CACHE)
    log.info("Filter cached: %d geo tokens, %d safe + %d bare-abbreviation institution patterns",
             len(_GEO_TOKENS_CACHE), len(_INST_REGEXES_CACHE), len(_ABBREV_REGEXES_CACHE))


def get_cached_filter_tiers() -> tuple[re.Pattern, re.Pattern]:
    """
    Lazily build and cache (safe_filter, abbrev_filter) for field-tiered
    Norwegian detection on raw per-field entries — see is_norwegian_entry().

    safe_filter    Geo names + full institution names + guarded abbreviations
                   + .no email.  Trustworthy against any field's text.
    abbrev_filter  The bare, unguarded abbreviation patterns alone.  Matches
                   a real institution's short code, but collides often enough
                   with unrelated specimen/strain codes that it's only safe
                   against identity-bearing fields (SPECIMEN_LIKE_FIELDS lists
                   the fields it's excluded from).
    """
    global _FILTER_TIERS_CACHE
    if _FILTER_TIERS_CACHE is not None:
        return _FILTER_TIERS_CACHE
    _ensure_filter_caches_built()
    safe_filter = make_combined_filter(_GEO_RE_CACHE, _INST_REGEXES_CACHE)
    abbrev_filter = (
        re.compile("|".join(f"(?:{p.pattern})" for p in _ABBREV_REGEXES_CACHE), re.IGNORECASE)
        if _ABBREV_REGEXES_CACHE else re.compile(r"(?!)")   # never matches
    )
    _FILTER_TIERS_CACHE = (safe_filter, abbrev_filter)
    return _FILTER_TIERS_CACHE


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


def is_norwegian_entry(entry: dict, safe_filter: re.Pattern,
                       abbrev_filter: re.Pattern) -> bool:
    """
    True if any field value in an EBI Search entry carries a Norwegian signal.

    Bare institution abbreviations (abbrev_filter) are only trusted in
    identity-bearing fields — center_name, broker_name, submitter, author,
    affiliation, etc. — never in SPECIMEN_LIKE_FIELDS (description, alias,
    scientific_name, strain, ...), where they collide with unrelated specimen
    and strain codes (see SPECIMEN_LIKE_FIELDS for confirmed examples).
    safe_filter (geo names, full institution names, guarded abbreviations,
    .no email) is checked against every field regardless.
    """
    fields = entry.get("fields", {})
    all_parts: list[str] = []
    identity_parts: list[str] = []
    for key, vals in fields.items():
        vlist = vals if isinstance(vals, list) else [vals]
        for v in vlist:
            if v is None or not str(v).strip():
                continue
            s = str(v)
            all_parts.append(s)
            if key not in SPECIMEN_LIKE_FIELDS:
                identity_parts.append(s)

    all_text = strip_false_positives(" ".join(all_parts))
    if not all_text.strip():
        return False
    if safe_filter.search(all_text):
        return True
    identity_text = strip_false_positives(" ".join(identity_parts))
    return bool(identity_text.strip()) and bool(abbrev_filter.search(identity_text))

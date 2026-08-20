#!/usr/bin/env python3
"""
fetch_ebi_data.py
=================
Domain configuration and fetch/cache logic for the EBI Norway Dashboard.

Responsibilities
----------------
* DOMAINS         – the authoritative domain config (single source of truth;
                    imported by the Snakefile and fetch_one_domain.py).
* fetch_domain    – paginate a domain, routing large partitionable domains to
                    the incremental year/quarter/month fetch.
* incremental cache – per-window partition files + manifest with sha256 checks.
* save_domain     – write data/raw/<domain>/latest.json (+ dated copy).
* fetch_and_save_domain – per-domain orchestration used by both the CLI
                    (fetch_one_domain.py) and the local in-process run below.

The low-level HTTP client lives in ebi_api.py and the Norwegian filter in
norwegian_filter.py, so this module is free of those concerns.

Strategy
--------
1. DISCOVER  – ebi_api.get_retrievable_fields() lists retrievable field IDs.
2. FETCH     – *:* with all fields.  Domains with a partition_date_field that
               exceed MAX_PAGEABLE are split into year/quarter/month/day
               windows; any single window still over the cap (and sra-study,
               which has no searchable date field) is paged with a keyset
               cursor on `acc` — see _fetch_window().
3. CACHE     – Only the last REFETCH_YEARS calendar years are re-fetched; older
               windows are served from sha256-verified partition files on disk.
4. FILTER    – sra-study is saved unfiltered (filter deferred to join_ena.py).
               sra-experiment and sra-sample are pre-filtered at page level
               (39–53 M rows unfiltered would exceed RAM).  Non-SRA domains are
               filtered at page level too.
5. SAVE      – data/raw/<domain>/latest.json (+ dated copy).

Output
------
  data/domains.json                       – domain config snapshot
  data/raw/<domain>/latest.json           – entries for this run
  data/raw/<domain>/manifest.json         – incremental-cache manifest
  data/raw/<domain>/partitions/<key>.json – per-window checkpoint files
"""

import hashlib
import json
import os
import sys
import logging
from datetime import date
from pathlib import Path

from paths import RAW_DIR, DOMAINS_JSON
from ebi_api import (
    BASE_URL, PAGE_SIZE, RATE_SLEEP, CATCH_ALL_QUERY,
    get_json, get_retrievable_fields, get_hit_count,
)
from norwegian_filter import (
    get_cached_filter_tiers, is_norwegian_entry,
)
import time
import re

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_ebi")

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

TODAY = date.today().isoformat()

# Hard deep-paging cap of the EBI Search API.  `start` at or beyond this value
# returns an empty `entries` list — silently, with HTTP 200 and the true
# (UNcapped) hitCount still reported.  A plain start-offset loop therefore
# stops here and looks exactly like a clean finish, so any window above the cap
# must either be split into smaller windows or continued with the keyset cursor
# in _fetch_window().  Verified against the live API: sra-study reports
# hitCount=753568 but returns nothing at start=100000.
MAX_PAGEABLE         = 100_000

# Field used as the keyset cursor when a window exceeds MAX_PAGEABLE.  It must
# be unique per entry and sortable; `acc` is both in every SRA domain (there it
# is also the entry id).  Override per domain with a "cursor_field" key.
DEFAULT_CURSOR_FIELD = "acc"

PARTITION_START_YEAR = 2007

# Number of trailing calendar years that are always re-fetched on every run.
# current_year and current_year-1 are refetched; everything older is immutable.
REFETCH_YEARS = 2

# sra-study: saved unfiltered so join_ena.py can find studies whose Norwegian
# signal is only visible after joining experiments or samples.  The domain
# has ~730 K entries total — well within the memory budget for a single pass.
#
# sra-experiment and sra-sample are NOT listed here: they have 39–53 M entries
# and are pre-filtered for Norwegian entries at page level inside _fetch_window.
# This trades a rare edge-case (Norwegian sample linked to a non-Norwegian
# experiment) for avoiding OOM on the fetch server.
#
# FILTER_VERSION must be bumped whenever the filtering OR pagination strategy
# changes, so that fetch_domain_partitioned() discards partition files whose
# contents the current code would no longer produce.
#
# 3: MAX_PAGEABLE was 1_000_000, ten times the API's real deep-paging cap, so
#    every window holding 100 K–1 M entries was silently cut off at 100 K and
#    then checkpointed to disk as if complete.  Immutable years would otherwise
#    keep serving those truncated files forever.
SRA_DOMAINS    = frozenset({"sra-study"})
FILTER_VERSION = 3


# ──────────────────────────────────────────────────────────────────────────────
# Domain configuration
# ──────────────────────────────────────────────────────────────────────────────

DOMAINS: dict[str, dict] = {
    "bioimages": {
        "required_fields": [
            "acc", "attach_to", "author", "collection", "creation_date",
            "domain_source", "figure_sub", "figure_type", "id", "image_name",
            "journal_name", "legend", "license", "method", "modified_date",
            "name", "omics_type", "release_date", "repository", "species",
        ],
        # identifiers.org prefix(es) for this domain's accessions; consumed by
        # the R render step to build https://identifiers.org/<prefix>:<acc> links
        # (the accession is validated against the registry pattern first).
        "identifiers_prefix": "biostudies",
    },
    "biostudies-other": {
        "required_fields": [
            "abstract", "acc", "agency", "author", "collection",
            "creation_date", "data_source", "domain_source", "experiment_type",
            "grant_id", "id", "id_noversion", "journal", "method",
            "modified_date", "name", "omics_type", "organisation",
            "pagination", "pmcid", "project", "pub_date", "release_date",
            "repository", "species", "volume",
        ],
        "identifiers_prefix": "biostudies",
    },
    "metabolights": {
        "required_fields": [
            "description", "domain_source", "full_dataset_link",
            "ftp_download_link", "id", "instrument_platform", "name",
            "omics_type", "organism", "organism_group", "organism_part",
            "publication", "publication_date", "repository", "study",
            "study_design", "study_factor", "study_status", "submission_date",
            "submitter_affiliation", "submitter_email", "submitter_name",
            "technology_type",
        ],
        "identifiers_prefix": "metabolights",
    },
    "pride": {
        "required_fields": [
            "curator_keywords", "data_protocol", "description", "disease",
            "doi", "domain_source", "full_dataset_link", "id",
            "instrument_platform", "labhead", "labhead_affiliation",
            "labhead_mail", "modification", "name", "omics_type",
            "publication", "publication_date", "quantification_method",
            "repository", "sample_protocol", "software", "species",
            "submission_date", "submission_type", "submitter",
            "submitter_affiliation", "submitter_country", "submitter_keywords",
            "submitter_mail", "technology_type", "tissue",
        ],
        "identifiers_prefix": "pride.project",
    },
    "biomodels": {
        "required_fields": [
            "all_xrefs", "curationstatus", "description", "disease",
            "domain_source", "first_author", "full_dataset_link", "id",
            "isprivate", "last_modification_date", "levelversion", "modelflag",
            "modelformat", "modellingapproach", "name", "non_derived_xrefs",
            "omics_type", "publication", "publication_authors",
            "publication_date", "publication_doi", "publication_pubmed",
            "publication_title", "publication_url", "publication_year",
            "publicationid", "repository", "submission_date", "submissionid",
            "submitter", "submitter_affiliation", "submitter_keywords",
            "submitter_mail", "tokenised_name",
        ],
        "identifiers_prefix": "biomodels.db",
    },
    # EGA (European Genome-phenome Archive): NOT a DOMAINS entry because the EBI
    # Search index returns only id/description/name for it — no dates and no
    # affiliation fields — making Norwegian detection impossible here.  EGA is
    # instead fetched from the EGA Public Metadata API by scripts/fetch_ega.py
    # (DACs → datasets → studies) and written to data/raw/ega/latest.json.
    "sra-study": {
        "required_fields": [
            "abstract", "acc", "alias", "center_project_name", "description",
            "domain_source", "first_public_date", "id", "insdc-project",
            "study_keywords", "study_type", "tag",
        ],
        # No partition_date_field: the sra-study index exposes first_public_date
        # as retrievable but NOT searchable (nor any other date), so there is no
        # date axis to window on.  Its ~753 K entries are fetched in one pass,
        # with _fetch_window's keyset cursor carrying it past the 100 K cap.
        "join_key": "study_accession",
        # ENA/SRA study accessions are SRP/ERP/DRP (insdc.sra) or PRJ* (bioproject);
        # the render tries each prefix and links the one whose pattern matches.
        "identifiers_prefix": ["insdc.sra", "bioproject"],
    },
    "sra-sample": {
        "required_fields": [
            "acc", "alias", "broker_name", "center_name", "classification",
            "collection_date", "country", "description", "domain_source",
            "first_public_date", "host", "id", "isolate", "last_updated_date",
            "region", "sample_capture_status", "scientific_name", "strain",
            "submission_tool", "tag",
        ],
        "partition_date_field": "first_public_date",
        "join_key": "sample_accession",
        "identifiers_prefix": "biosample",
    },
    "sra-experiment": {
        "required_fields": [
            "abstract", "acc", "alias", "center_name", "classification",
            "collection_date", "country", "description", "domain_source",
            "first_public_date", "host", "id", "instrument_model",
            "instrument_platform", "last_updated_date", "library_layout",
            "library_name", "library_selection", "library_source",
            "library_strategy", "region", "scientific_name", "strain",
            "sub_species", "tag",
        ],
        "partition_date_field": "first_public_date",
        "join_key": "study_accession",
        "identifiers_prefix": ["insdc.sra", "bioproject"],
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Manifest (incremental cache bookkeeping)
# ──────────────────────────────────────────────────────────────────────────────

def _manifest_path(domain: str) -> Path:
    return RAW_DIR / domain / "manifest.json"


def _load_manifest(domain: str) -> dict:
    path = _manifest_path(domain)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"domain": domain, "partitions": {}}


def _save_manifest(domain: str, manifest: dict) -> None:
    path = _manifest_path(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["last_fetch_date"] = TODAY
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# Partition helpers
# ──────────────────────────────────────────────────────────────────────────────

def _partition_dir(domain: str) -> Path:
    return RAW_DIR / domain / "partitions"


def _partition_path(domain: str, key: str) -> Path:
    return _partition_dir(domain) / f"{key}.json"


def _partition_ok(domain: str, key: str, manifest: dict) -> bool:
    """
    Return True if the partition file exists, is valid JSON, and its sha256
    matches the manifest record (if one exists).  On any mismatch returns False
    so the window is re-fetched.
    """
    path = _partition_path(domain, key)
    if not path.exists():
        return False
    try:
        json.loads(path.read_bytes())           # validity check
    except Exception:
        log.warning("  Corrupt partition %s – will re-fetch", path)
        return False
    recorded = manifest.get("partitions", {}).get(key, {}).get("sha256")
    if recorded:
        actual = _sha256_file(path)
        if actual != recorded:
            log.warning("  sha256 mismatch for partition %s – will re-fetch", key)
            return False
    return True


def _invalidate_partition(domain: str, key: str, manifest: dict) -> None:
    """Delete a partition file and remove it from the manifest."""
    path = _partition_path(domain, key)
    if path.exists():
        path.unlink()
    manifest.get("partitions", {}).pop(key, None)
    # Also delete sub-window files (Q and M keys) under the same year
    year = key.split("_")[0]
    part_dir = _partition_dir(domain)
    for child in list(part_dir.glob(f"{year}_*.json")):
        child.unlink()
        child_key = child.stem
        manifest.get("partitions", {}).pop(child_key, None)
    log.info("  Invalidated partition %s and sub-windows for year %s", key, year)


def _load_partition(domain: str, key: str) -> list[dict] | None:
    path = _partition_path(domain, key)
    if not path.exists():
        return None
    try:
        entries = json.loads(path.read_text())
        log.info("    ↩ loaded %s partition %s from disk (%d entries)",
                 domain, key, len(entries))
        return entries
    except Exception as exc:
        log.warning("    Corrupt partition %s – will re-fetch (%s)", path, exc)
        return None


def _save_partition(domain: str, key: str, entries: list[dict],
                    manifest: dict) -> None:
    """Write partition atomically and record its sha256 in the manifest."""
    path = _partition_path(domain, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(entries, ensure_ascii=False)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw)
    os.replace(tmp, path)
    manifest.setdefault("partitions", {})[key] = {
        "entries":    len(entries),
        "sha256":     _sha256_file(path),
        "fetch_date": TODAY,
    }


def _cursor_value(entry: dict, cursor_field: str) -> str:
    """Read an entry's keyset cursor value (top-level first, then `fields`)."""
    val = entry.get(cursor_field)
    if val is None:
        vals = entry.get("fields", {}).get(cursor_field) or []
        val = vals[0] if vals else entry.get("id", "")
    return str(val)


def _fetch_window(domain: str, fields: list[str], query: str,
                  safe_filter: re.Pattern, abbrev_filter: re.Pattern,
                  hit_count: int | None = None) -> tuple[list[dict], int]:
    """
    Paginate through a single query window.

    Returns (kept_entries, entries_seen) — `entries_seen` counts everything the
    API returned, before the Norwegian filter, so callers can report the keep
    ratio and detect short reads.

    Windows at or below MAX_PAGEABLE use a plain start-offset loop.  Above it
    the offset loop would stop dead at the cap (see MAX_PAGEABLE), so the window
    is continued with a keyset cursor instead: the query is re-issued sorted by
    `cursor_field` and restricted to `cursor_field:[<last value seen> TO *]`, so
    every chunk starts from offset 0 and the cap is never reached.  The
    inclusive lower bound re-serves the entries tied on the cursor value, which
    are tracked in `boundary_ids` and skipped once.
    """
    url          = f"{BASE_URL}/{domain}"
    cursor_field = DOMAINS.get(domain, {}).get("cursor_field", DEFAULT_CURSOR_FIELD)
    is_sra       = domain in SRA_DOMAINS

    if hit_count is None:
        hit_count = get_hit_count(domain, fields, query)
    use_cursor = hit_count > MAX_PAGEABLE
    if use_cursor:
        log.info("    window has %d entries > cap %d – paging via %s cursor",
                 hit_count, MAX_PAGEABLE, cursor_field)

    entries:      list[dict] = []
    total_seen:   int        = 0
    cursor:       str | None = None
    boundary_ids: set[str]   = set()
    failed:       bool       = False

    while True:
        chunk_query = (query if cursor is None else
                       f"({query}) AND {cursor_field}:[{cursor} TO *]")
        params = {
            "query":  chunk_query,
            "fields": ",".join(fields),
            "format": "json",
            "size":   PAGE_SIZE,
            "start":  0,
        }
        if use_cursor:
            params["sort"] = cursor_field

        # Carry the boundary set forward so entries re-served by the inclusive
        # lower bound stay skipped even if the cursor cannot advance this chunk.
        last_val:   str | None = cursor
        last_ids:   set[str]   = set(boundary_ids)
        chunk_seen: int        = 0
        chunk_hits: int | None = None
        exhausted:  bool       = False

        while params["start"] < MAX_PAGEABLE:
            try:
                data = get_json(url, params)
            except Exception as exc:
                # Fall through rather than return: the completeness check below
                # is what stops a short window being cached as if it were whole.
                log.error("    Window fetch failed at start=%d: %s",
                          params["start"], exc)
                failed = True
                break

            # Bound the offset loop by this chunk's own hitCount: the API
            # answers 400, not an empty page, once start >= hitCount, and a
            # 400 here would abandon the rest of the window.
            if chunk_hits is None:
                chunk_hits = int(data.get("hitCount", 0))

            batch = data.get("entries", [])
            if not batch:
                exhausted = True
                break

            for e in batch:
                eid = e.get("id", "")
                val = _cursor_value(e, cursor_field)
                if cursor is not None and val == cursor and eid in boundary_ids:
                    continue
                if val != last_val:
                    last_val, last_ids = val, {eid}
                else:
                    last_ids.add(eid)
                total_seen += 1
                chunk_seen += 1
                if is_sra or is_norwegian_entry(e, safe_filter, abbrev_filter):
                    entries.append(e)

            params["start"] += PAGE_SIZE
            if params["start"] >= chunk_hits:
                exhausted = True
                break
            time.sleep(RATE_SLEEP)

        if failed or exhausted or not use_cursor:
            break
        if chunk_seen == 0 or last_val is None:
            log.error("    %s: keyset cursor stalled at %s=%r after %d entries",
                      domain, cursor_field, cursor, total_seen)
            break
        cursor, boundary_ids = last_val, last_ids
        time.sleep(RATE_SLEEP)

    if hit_count and total_seen < hit_count:
        log.error("    %s: INCOMPLETE window – got %d of %d reported entries "
                  "for query %s", domain, total_seen, hit_count, query)

    return entries, total_seen



# ──────────────────────────────────────────────────────────────────────────────
# Incremental partitioned fetch
# ──────────────────────────────────────────────────────────────────────────────

def fetch_domain_partitioned(domain: str, cfg: dict, fields: list[str],
                              safe_filter: re.Pattern, abbrev_filter: re.Pattern) -> list[dict]:
    """
    Fetch a partitioned domain with incremental caching.

    Incremental rule
    ----------------
    immutable : year <= current_year - REFETCH_YEARS
        Loaded from disk if the partition file passes its sha256 check.
        Never re-fetched unless the file is absent or corrupt.

    refetch   : year >= current_year - REFETCH_YEARS + 1
        Deleted and re-fetched on every run (these windows grow as new
        records are added to EBI).

    Window splitting (recursive, year → quarter → month)
    --------------------------------------------------------
    If a window exceeds MAX_PAGEABLE it is split into sub-windows.
    Sub-windows follow the same immutable/refetch rule based on their year.
    """
    import calendar

    date_field   = cfg.get("partition_date_field", "first_public_date")
    current_year = date.today().year
    immutable_max = current_year - REFETCH_YEARS   # years ≤ this are immutable

    manifest = _load_manifest(domain)

    # If the filter strategy changed (e.g. domain moved from unfiltered to
    # pre-filtered), old partition files contain the wrong content.  Wipe them
    # so they are re-fetched with the current strategy.
    if manifest.get("filter_version", 1) != FILTER_VERSION:
        log.info("  %s: filter_version changed (%s→%d) – clearing cached partitions",
                 domain, manifest.get("filter_version", 1), FILTER_VERSION)
        part_dir = _partition_dir(domain)
        if part_dir.exists():
            for pf in sorted(part_dir.glob("*.json")):
                pf.unlink()
        manifest = {"domain": domain, "partitions": {}}
    manifest["filter_version"] = FILTER_VERSION

    all_entries: list[dict] = []
    seen_ids:    set[str]   = set()

    def _add(batch: list[dict]) -> None:
        for e in batch:
            # Entries with an id de-dup by id; id-less entries (rare) de-dup by
            # their content so the same record appearing in overlapping or
            # retried windows is not counted twice.
            eid = e.get("id", "")
            key = eid or json.dumps(e.get("fields", {}), sort_keys=True,
                                    ensure_ascii=False)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            all_entries.append(e)

    def _date_range(year: int, month: int | None, quarter: int | None,
                    day: int | None = None) -> tuple[str, str]:
        if day is not None and month is not None:
            return (f"{year}-{month:02d}-{day:02d}",
                    f"{year}-{month:02d}-{day:02d}")
        if month is not None:
            last_day = calendar.monthrange(year, month)[1]
            return (f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}")
        if quarter is not None:
            m_start = (quarter - 1) * 3 + 1
            m_end   = quarter * 3
            last_day = calendar.monthrange(year, m_end)[1]
            return (f"{year}-{m_start:02d}-01", f"{year}-{m_end:02d}-{last_day:02d}")
        return (f"{year}-01-01", f"{year}-12-31")

    def _fetch_or_load(key: str, year: int, quarter: int | None,
                       month: int | None, day: int | None = None,
                       indent: str = "  ") -> list[dict]:
        """
        Return entries for one date window, splitting recursively if needed
        (year → quarter → month → day).  Immutable windows are served from disk;
        refetch windows are re-downloaded.
        """
        # Immutable window: serve from disk if file is OK
        if year <= immutable_max:
            if _partition_ok(domain, key, manifest):
                return _load_partition(domain, key) or []
            # File missing or corrupt → fall through to re-fetch

        # Refetch window: always invalidate first
        else:
            _invalidate_partition(domain, key, manifest)

        d_start, d_end = _date_range(year, month, quarter, day)
        window_query   = f"{date_field}:[{d_start} TO {d_end}]"
        count = get_hit_count(domain, fields, window_query)
        log.info("%s%s  %s–%s  %d entries", indent, domain, d_start, d_end, count)

        if count == 0:
            _save_partition(domain, key, [], manifest)
            return []

        # Windows within the cap are fetched directly.  Larger ones are still
        # split rather than left to the cursor: small partitions checkpoint to
        # disk, so an interrupted run resumes instead of restarting the year.
        if count <= MAX_PAGEABLE:
            entries, _ = _fetch_window(domain, fields, window_query,
                                       safe_filter, abbrev_filter, hit_count=count)
            _save_partition(domain, key, entries, manifest)
            log.info("%s→ fetched %d entries", indent + "  ", len(entries))
            time.sleep(RATE_SLEEP)
            return entries

        # Window too large: split to the next finer granularity.
        if day is not None:
            # A single day is the finest date window the API offers, but days
            # over the cap are common in the SRA domains (2024-12-10 alone holds
            # ~195 K samples).  _fetch_window pages past the cap with its keyset
            # cursor, so the day is still retrieved in full.
            log.info("%s%s %s has %d entries > cap %d – keyset cursor",
                     indent, domain, d_start, count, MAX_PAGEABLE)
            entries, _ = _fetch_window(domain, fields, window_query,
                                       safe_filter, abbrev_filter, hit_count=count)
            _save_partition(domain, key, entries, manifest)
            time.sleep(RATE_SLEEP)
            return entries

        if month is not None:
            sub: list[dict] = []
            last_day = calendar.monthrange(year, month)[1]
            for d in range(1, last_day + 1):
                sub.extend(_fetch_or_load(
                    f"{year}_M{month:02d}_D{d:02d}", year, None, month, d,
                    indent + "  "))
            _save_partition(domain, key, sub, manifest)
            return sub

        if quarter is not None:
            sub = []
            m0 = (quarter - 1) * 3 + 1
            for m in range(m0, m0 + 3):
                sub.extend(_fetch_or_load(
                    f"{year}_M{m:02d}", year, None, m, None, indent + "  "))
            _save_partition(domain, key, sub, manifest)
            return sub

        sub = []
        for q in range(1, 5):
            sub.extend(_fetch_or_load(
                f"{year}_Q{q}", year, q, None, None, indent + "  "))
        _save_partition(domain, key, sub, manifest)
        return sub

    log.info("  %s: incremental partitioned fetch  date_field=%s"
             "  years=%d–%d  immutable_up_to=%d",
             domain, date_field, PARTITION_START_YEAR,
             current_year, immutable_max)

    for year in range(PARTITION_START_YEAR, current_year + 1):
        year_entries = _fetch_or_load(str(year), year, None, None)
        _add(year_entries)

    _save_manifest(domain, manifest)
    log.info("  %s: done – %d unique entries (%d immutable years cached)",
             domain, len(all_entries), max(0, immutable_max - PARTITION_START_YEAR + 1))
    return all_entries


# ──────────────────────────────────────────────────────────────────────────────
# Core fetch dispatcher
# ──────────────────────────────────────────────────────────────────────────────

def fetch_domain(domain: str, cfg: dict, fields: list[str],
                 safe_filter: re.Pattern, abbrev_filter: re.Pattern) -> list[dict]:
    """
    Fetch all entries for a domain.

    Domains with partition_date_field are routed to fetch_domain_partitioned()
    which implements both the year/quarter/month window splitting and the
    incremental cache.  Small domains use standard single-pass pagination.
    """
    hit_count = get_hit_count(domain, fields, CATCH_ALL_QUERY)
    log.info("  %s → %d total entries in domain", domain, hit_count)

    # hitCount is reported accurately even above MAX_PAGEABLE, so > is exact
    # here: only domains that genuinely exceed the cap need partitioning.
    if cfg.get("partition_date_field") and hit_count > MAX_PAGEABLE:
        log.info("  %s above MAX_PAGEABLE – using incremental partitioned fetch",
                 domain)
        return fetch_domain_partitioned(domain, cfg, fields, safe_filter, abbrev_filter)

    # Single-pass fetch for every other domain.  _fetch_window transparently
    # switches to its keyset cursor above MAX_PAGEABLE, so this path is safe for
    # sra-study (~753 K entries, no searchable date field to partition on).
    entries, total_seen = _fetch_window(domain, fields, CATCH_ALL_QUERY,
                                        safe_filter, abbrev_filter,
                                        hit_count=hit_count)

    if domain in SRA_DOMAINS:
        log.info("  %s → saved all %d entries unfiltered (filter deferred to join_ena.py)",
                 domain, len(entries))
    else:
        log.info("  %s → kept %d / %d after Norwegian filter",
                 domain, len(entries), total_seen)
    return entries


# ──────────────────────────────────────────────────────────────────────────────
# Save
# ──────────────────────────────────────────────────────────────────────────────

def save_domain(domain: str, entries: list[dict], fields: list[str]) -> Path:
    """Write entries to data/raw/<domain>/latest.json (and a dated copy)."""
    raw_dir = RAW_DIR / domain
    raw_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "domain":      domain,
        "fetch_date":  TODAY,
        "query":       CATCH_ALL_QUERY,
        "fields_used": fields,
        "entry_count": len(entries),
        "entries":     entries,
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)

    dated_path = raw_dir / f"{TODAY}.json"
    dated_path.write_text(body)
    log.info("  Saved %d entries → %s", len(entries), dated_path)

    latest_path = raw_dir / "latest.json"
    latest_path.write_text(body)
    return dated_path


def save_domains_json(path: Path = DOMAINS_JSON) -> None:
    """Write DOMAINS config to data/domains.json for external consumers."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            "Auto-generated by fetch_ebi_data.py – do not edit manually. "
            "Edit DOMAINS in scripts/fetch_ebi_data.py instead."
        ),
        "generated":    TODAY,
        "domain_count": len(DOMAINS),
        "domains":      DOMAINS,
    }
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, out_path)
    log.info("Wrote %s (%d domains)", path, len(DOMAINS))


# ──────────────────────────────────────────────────────────────────────────────
# Per-domain orchestration  (single source of truth for one domain's fetch)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_and_save_domain(domain: str) -> int:
    """
    Fetch + filter + save a single domain.  Used by fetch_one_domain.py (one
    domain per Snakemake job) and by main()'s local in-process loop.

    The Norwegian filter is lazily built on first call and cached for reuse
    across all domains in the process.
    Returns the number of entries saved.
    """
    cfg = DOMAINS[domain]
    safe_filter, abbrev_filter = get_cached_filter_tiers()

    log.info("=== fetch_and_save_domain: %s ===", domain)
    fields  = get_retrievable_fields(domain, cfg)
    entries = fetch_domain(domain, cfg, fields, safe_filter, abbrev_filter)
    save_domain(domain, entries, fields)

    # Partition checkpoint summary (files are retained so a re-run resumes)
    part_dir = _partition_dir(domain)
    if part_dir.exists():
        part_files = sorted(part_dir.glob("*.json"))
        log.info("  Partitions: %d checkpoint files retained in %s",
                 len(part_files), part_dir)

    log.info("=== Done: %s (%d entries) ===", domain, len(entries))
    return len(entries)


# ──────────────────────────────────────────────────────────────────────────────
# Main  –  sequential local orchestrator (Snakemake is the production driver)
# ──────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--list-domains":
        save_domains_json()
        print(json.dumps(list(DOMAINS.keys())))
        return

    log.info("Starting EBI fetch (sequential / local)  date=%s", TODAY)
    save_domains_json()

    # Cache the identifiers.org namespace registry for the link feature.  Failure
    # is non-fatal (the render step simply skips links it can't build).
    log.info("─── Caching identifiers.org namespaces ───")
    try:
        import fetch_identifiers
        fetch_identifiers.main()
    except Exception as exc:
        log.error("fetch_identifiers failed: %s", exc)

    # The Norwegian filter is lazily built on the first domain fetch and cached
    # for reuse across all domains.
    failed: list[str] = []
    for domain in DOMAINS:
        log.info("─── Domain: %s ───", domain)
        try:
            fetch_and_save_domain(domain)
        except Exception as exc:
            log.error("fetch_and_save_domain failed for %s: %s", domain, exc)
            failed.append(domain)

    if failed:
        log.error("Failed domains: %s", ", ".join(failed))
    else:
        log.info("All domains fetched ✓")

    # Run the SRA join in-process (no subprocess indirection).
    log.info("─── Running join_ena ───")
    try:
        import join_ena
        join_ena.main()
        log.info("join_ena ✓")
    except Exception as exc:
        log.error("join_ena failed: %s", exc)

    # Fetch EGA studies via the EGA Public Metadata API (separate service; not a
    # DOMAINS entry — see scripts/fetch_ega.py for why).
    log.info("─── Running fetch_ega ───")
    try:
        import fetch_ega
        fetch_ega.main()
        log.info("fetch_ega ✓")
    except Exception as exc:
        log.error("fetch_ega failed: %s", exc)


if __name__ == "__main__":
    main()

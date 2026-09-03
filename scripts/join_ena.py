#!/usr/bin/env python3
"""
join_ena.py
===========
Joins the three SRA/ENA sub-tables fetched by fetch_ebi_data.py into a single
flat table `data/processed/ena_joined.json`, filtered to Norwegian entries.

Sub-tables used
---------------
sra-study      (primary key: study_accession)
sra-experiment (study_accession → study)
sra-sample     (sample_accession, via experiment)

Dropped vs. earlier version
----------------------------
sra-submission  No searchable date field; 31M entries not partitionable.
sra-analysis    Date field mismatch (last_updated_date ≠ first_public_date).
sra-run         No searchable date field; 42M entries; only used for run counts.

Filtering strategy
------------------
sra-study      Saved unfiltered by the fetch step (~730 K entries); the
               Norwegian filter is applied here post-join so that studies
               detectable only via joined experiment/sample signals are kept.

sra-experiment Pre-filtered for Norwegian entries at page level during the
sra-sample     fetch step (memory constraint: 39–53 M rows each).  The
               partition cache is versioned (FILTER_VERSION=2) so stale
               unfiltered partition files from older runs are discarded.

Recovery for uncovered samples
------------------------------
After the initial join, any Norwegian sample whose linked experiment was
filtered out (non-Norwegian center_name/country) is detected and a targeted
API query fetches the missing experiment→study links from sra-experiment.
This uses the SAMPLE XREF field (SAMEA/SAMN → ERS fallback) in batches of
50.  Network access is required for this step; failures are logged and the
join continues with the links already available.

EBI API join-key note: study_accession and sample_accession fields in
sra-experiment are always empty.  The actual links are in the XREF fields
SRA-STUDY (ERP/SRP accession) and SAMPLE (SAMEA/SAMN BioSample accession).

Output
------
  data/processed/ena_joined.json
  data/processed/ena_joined_<date>.json
"""

import json
import logging
import re
import time
import sys
from pathlib import Path
from datetime import date
from typing import Iterator

import pandas as pd

# Ensure scripts/ is importable regardless of how this file is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from norwegian_filter import (
    get_cached_filter_tiers, FALSE_POSITIVE_RE,
)
from paths import RAW_DIR, PROC_DIR

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("join_ena")

TODAY = date.today().isoformat()
PROC_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Projected loading helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fv(fields: dict, key: str) -> str:
    v = fields.get(key, [])
    if isinstance(v, list):
        return v[0] if v else ""
    return str(v) if v else ""


def _fvlist(fields: dict, key: str) -> list[str]:
    v = fields.get(key, [])
    items = v if isinstance(v, list) else [v]
    return [str(x) for x in items if x]


def iter_entries(domain: str) -> Iterator[dict]:
    path = RAW_DIR / domain / "latest.json"
    if not path.exists():
        log.warning("No latest.json for %s – skipping", domain)
        return
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in data.get("entries", []):
        yield entry


# Column schemas for the frames built by rows_to_df() below.  Each is the
# single source of truth for its table: rows_to_df() asserts the row dicts
# match it, and it doubles as the fallback schema when zero rows are loaded
# (pd.DataFrame([]) would otherwise produce a 0-column frame, breaking the
# join-key merges/groupbys downstream).
STUDY_COLUMNS = ["study_acc", "title", "center_name", "description", "study_text"]
EXP_COLUMNS = [
    "exp_acc", "study_acc", "sample_acc", "first_public_date",
    "exp_country", "exp_center", "exp_text",
]
SAMPLE_COLUMNS = [
    "sample_acc", "sample_country", "sample_center", "sample_broker",
    "sample_region", "sample_text",
]


def rows_to_df(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    """Build a schema-stable DataFrame from a list of same-shaped dicts."""
    if not rows:
        return pd.DataFrame(columns=columns, dtype="string")
    df = pd.DataFrame(rows, dtype="string")
    assert set(df.columns) == set(columns), (
        f"row dict keys {sorted(df.columns)} != declared columns {sorted(columns)}"
    )
    return df


def load_studies() -> pd.DataFrame:
    """
    Load all sra-study entries (unfiltered).  Norwegian filter is applied post-join
    so that studies detectable only via their joined experiment signals are kept.
    """
    rows = []
    for e in iter_entries("sra-study"):
        f = e.get("fields", {})
        rows.append({
            "study_acc":   e.get("id", "") or _fv(f, "acc"),
            "title":       _fv(f, "abstract") or _fv(f, "description"),
            "center_name": _fv(f, "center_project_name"),
            "description": _fv(f, "description"),
            "study_text":  " ".join(filter(None, [
                _fv(f, "abstract"), _fv(f, "description"),
                _fv(f, "center_project_name"), _fv(f, "alias"),
                _fv(f, "study_keywords"), _fv(f, "study_type"),
            ])),
        })
    df = rows_to_df(rows, STUDY_COLUMNS)
    log.info("  sra-study:      %d rows loaded", len(df))
    return df


def load_experiments() -> pd.DataFrame:
    """Load pre-filtered Norwegian sra-experiment entries; project to join keys + signal columns."""
    rows = []
    for e in iter_entries("sra-experiment"):
        f = e.get("fields", {})
        # SRA-STUDY and SAMPLE are the actual join-key XREF fields in the EBI Search API.
        # study_accession / sample_accession are never populated.
        rows.append({
            "exp_acc":           e.get("id", "") or _fv(f, "acc"),
            "study_acc":         _fv(f, "SRA-STUDY"),
            "sample_acc":        _fv(f, "SAMPLE") or _fv(f, "SRA-SAMPLE"),
            "first_public_date": _fv(f, "first_public_date"),
            "exp_country":       _fv(f, "country"),
            "exp_center":        _fv(f, "center_name"),
            "exp_text": " ".join(filter(None, [
                _fv(f, "abstract"),
                _fv(f, "alias"),
                _fv(f, "country"),
                _fv(f, "center_name"),
                _fv(f, "description"),
                _fv(f, "region"),
            ])),
        })
    df = rows_to_df(rows, EXP_COLUMNS)
    log.info("  sra-experiment: %d rows loaded", len(df))
    return df


def load_samples() -> pd.DataFrame:
    """Load pre-filtered Norwegian sra-sample entries; project to acc + signal columns."""
    rows = []
    for e in iter_entries("sra-sample"):
        f = e.get("fields", {})
        rows.append({
            "sample_acc":     e.get("id", "") or _fv(f, "acc"),
            "sample_country": _fv(f, "country"),
            "sample_center":  _fv(f, "center_name"),
            "sample_broker":  _fv(f, "broker_name"),
            "sample_region":  _fv(f, "region"),
            # broker_name, alias, description are fetched but were previously
            # dropped before the Norwegian filter; include them now.
            "sample_text":    " ".join(filter(None, [
                _fv(f, "broker_name"),
                _fv(f, "alias"),
                _fv(f, "description"),
            ])),
        })
    df = rows_to_df(rows, SAMPLE_COLUMNS)

    # EBI Search's sra-sample domain indexes broker_name (searchable, facetable)
    # but does NOT mark it retrievable, so the fetch above returns "" for every
    # sample even when ENA does record a broker (e.g. "ELIXIR Norway" brokering
    # a Norwegian institution's samples) — this is what silently dropped broker
    # attribution in the dashboard.  Backfill from ENA's own Portal API, which
    # exposes broker_name cleanly and separately from center_name, for exactly
    # the samples the regular fetch left blank.
    if len(df):
        missing = df.loc[df["sample_broker"].fillna("") == "", "sample_acc"] \
            .dropna().tolist()
        missing = [a for a in missing if a]
        if missing:
            log.info("  sra-sample: backfilling broker_name for %d samples via "
                      "ENA Portal API …", len(missing))
            broker_map = _fetch_broker_names(missing)
            if broker_map:
                fill = df["sample_acc"].map(broker_map).fillna("")
                blank = df["sample_broker"].fillna("") == ""
                df.loc[blank, "sample_broker"] = fill[blank]
                log.info("  sra-sample: recovered %d broker names", len(broker_map))

    log.info("  sra-sample:     %d rows loaded", len(df))
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Broker-name backfill (EBI Search does not retrieve it)
# ──────────────────────────────────────────────────────────────────────────────

_PORTAL_URL   = "https://www.ebi.ac.uk/ena/portal/api/search"
_BROKER_BATCH = 50   # Portal API query-string length is the practical limit


def _fetch_broker_names(sample_accs: list[str]) -> dict[str, str]:
    """
    Look up broker_name for the given sample accessions via ENA's Portal API.

    Why this is needed: EBI Search's sra-sample domain field config marks
    broker_name searchable/facetable but NOT retrievable, so every fetch via
    scripts/fetch_ebi_data.py gets back "" for it regardless of what ENA has
    on record — silently dropping broker attribution (e.g. "ELIXIR Norway"
    brokering samples whose center_name is the depositing institution) even
    though the study/sample genuinely has one.  ENA's own Portal API exposes
    broker_name as a clean field, separate from center_name, so it is used
    here as a best-effort backfill for exactly the accessions the regular
    fetch left blank.

    Best-effort: network failures are logged and skipped so the join
    continues with whatever the pipeline's own fetch collected instead.
    Returns {sample_accession: broker_name}, omitting accessions with no
    broker on record.
    """
    if not _REQUESTS_AVAILABLE:
        log.warning("requests not installed – cannot backfill ENA broker_name")
        return {}

    result: dict[str, str] = {}
    for i in range(0, len(sample_accs), _BROKER_BATCH):
        batch = sample_accs[i : i + _BROKER_BATCH]
        query = " OR ".join(f'sample_accession="{acc}"' for acc in batch)
        try:
            resp = _requests.get(
                _PORTAL_URL,
                params={
                    "result": "sample",
                    "query":  query,
                    "fields": "sample_accession,broker_name",
                    "format": "json",
                    "limit":  len(batch),
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json() or []
        except Exception as exc:
            log.warning("    Broker-name backfill batch %d failed: %s",
                        i // _BROKER_BATCH, exc)
            continue

        for entry in data:
            acc    = (entry.get("sample_accession") or "").strip()
            broker = (entry.get("broker_name") or "").strip()
            if acc and broker:
                result[acc] = broker

        if i + _BROKER_BATCH < len(sample_accs):
            time.sleep(0.4)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Targeted experiment-link recovery for uncovered Norwegian samples
# ──────────────────────────────────────────────────────────────────────────────

_EBI_URL = "https://www.ebi.ac.uk/ebisearch/ws/rest"
_LINK_BATCH = 50   # Lucene OR-clause limit for XREF queries


def _fetch_experiment_links(sample_accs: list[str]) -> list[dict]:
    """
    For Norwegian samples not covered by any experiment in df_exps (i.e. their
    experiment was filtered out because it has no Norwegian center/country),
    fetch the (exp_acc, study_acc, sample_acc) link rows plus their
    country/center_name signal directly from the sra-experiment API using
    SAMPLE XREF queries.

    country/center_name are fetched (not left blank) so a study recovered
    only through this path still carries an experiment-level Norwegian signal
    and isn't silently dropped by the filter a few steps later.

    Returns a list of link rows suitable for appending to df_exps before the
    join aggregation.
    """
    if not _REQUESTS_AVAILABLE:
        log.warning("requests not installed – cannot recover uncovered sample links")
        return []

    rows: list[dict] = []
    for i in range(0, len(sample_accs), _LINK_BATCH):
        batch = sample_accs[i : i + _LINK_BATCH]
        query = " OR ".join(f"SAMPLE:{acc}" for acc in batch)
        start = 0
        while True:
            try:
                resp = _requests.get(
                    f"{_EBI_URL}/sra-experiment",
                    params={
                        "query":  query,
                        "fields": "acc,SRA-STUDY,SAMPLE,SRA-SAMPLE,country,center_name",
                        "format": "json",
                        "size":   500,
                        "start":  start,
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning("    Link-fetch batch %d failed: %s", i // _LINK_BATCH, exc)
                break

            for entry in data.get("entries", []):
                f = entry.get("fields", {})
                exp_country = _fv(f, "country")
                exp_center  = _fv(f, "center_name")
                rows.append({
                    "exp_acc":           entry.get("id", "") or _fv(f, "acc"),
                    "study_acc":         _fv(f, "SRA-STUDY"),
                    "sample_acc":        _fv(f, "SAMPLE") or _fv(f, "SRA-SAMPLE"),
                    "first_public_date": "",
                    "exp_country":       exp_country,
                    "exp_center":        exp_center,
                    "exp_text":          " ".join(filter(None, [exp_country, exp_center])),
                })

            hit_count = data.get("hitCount", 0)
            start += 500
            if start >= hit_count or not data.get("entries"):
                break
            time.sleep(0.4)

    return rows


def _fetch_study_dates(study_accs: list[str]) -> dict[str, str]:
    """
    Return the earliest first_public_date per study, queried directly from
    sra-experiment via the SRA-STUDY XREF.

    sra-study carries no first_public_date of its own (the EBI Search index
    leaves the field empty), so a study's date lives only on its experiments.
    The experiment table is pre-filtered to Norwegian entries, so a study kept
    on a study/sample text signal whose experiments are non-Norwegian arrives
    with no date and is silently dropped by the R render (filter(!is.na(date))).
    This backfill re-queries sra-experiment unfiltered for exactly those studies
    and recovers the date.  Batched OR-queries, 50 studies each.

    When a study's experiments carry different dates, the latest is kept
    (chronological max, matching the exp_agg rule) so both paths agree.

    Returns {study_acc: "YYYYMMDD"} for studies where a date was found.
    """
    if not _REQUESTS_AVAILABLE:
        log.warning("requests not installed – cannot backfill study dates")
        return {}

    dates: dict[str, str] = {}
    for i in range(0, len(study_accs), _LINK_BATCH):
        batch = study_accs[i : i + _LINK_BATCH]
        query = " OR ".join(f"SRA-STUDY:{acc}" for acc in batch)
        start = 0
        while True:
            try:
                resp = _requests.get(
                    f"{_EBI_URL}/sra-experiment",
                    params={
                        "query":  query,
                        "fields": "SRA-STUDY,first_public_date",
                        "format": "json",
                        "size":   500,
                        "start":  start,
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning("    Date-backfill batch %d failed: %s", i // _LINK_BATCH, exc)
                break

            for entry in data.get("entries", []):
                f = entry.get("fields", {})
                sacc = _fv(f, "SRA-STUDY")
                fpd  = _fv(f, "first_public_date")
                if sacc and fpd:
                    prev = dates.get(sacc)
                    if prev is None or fpd > prev:   # keep latest (most recent)
                        dates[sacc] = fpd

            hit_count = data.get("hitCount", 0)
            start += 500
            if start >= hit_count or not data.get("entries"):
                break
            time.sleep(0.4)

    return dates


# ──────────────────────────────────────────────────────────────────────────────
# Main join
# ──────────────────────────────────────────────────────────────────────────────

def main():
    safe_filter, abbrev_filter = get_cached_filter_tiers()
    log.info("Filter ready (cached)")

    log.info("Loading SRA sub-tables …")
    df_studies  = load_studies()
    df_exps     = load_experiments()
    df_samples  = load_samples()

    # All three tables are cumulative (not just "new since last run"), so a
    # healthy fetch should never come back with zero rows.  0 rows almost
    # always means an upstream EBI Search outage or fetch bug rather than a
    # genuine "no data" state.  Abort without writing output rather than
    # silently overwriting the last known-good ena_joined.json (the file the
    # dashboard renders from) with degraded or empty results.
    for name, table in (
        ("sra-study", df_studies), ("sra-experiment", df_exps), ("sra-sample", df_samples),
    ):
        if table.empty:
            log.error(
                "  %s: 0 rows loaded – aborting without writing output "
                "(likely an upstream fetch failure; leaving existing %s in place)",
                name, PROC_DIR / "ena_joined.json",
            )
            sys.exit(1)

    # ── Recover Norwegian samples whose experiment was filtered out ────────────
    # sra-experiment is pre-filtered for Norwegian entries, so samples linked
    # via non-Norwegian experiments have no path to their study.  Find those
    # samples and fetch the missing experiment→study link rows from the API.
    covered_sample_accs = set(df_exps["sample_acc"].dropna())
    covered_sample_accs.discard("")
    uncovered = [
        acc for acc in df_samples["sample_acc"].dropna()
        if acc and acc not in covered_sample_accs
    ]
    if uncovered:
        log.info("  %d Norwegian samples lack a Norwegian experiment – fetching links …",
                 len(uncovered))
        link_rows = _fetch_experiment_links(uncovered)
        if link_rows:
            df_links = rows_to_df(link_rows, EXP_COLUMNS)
            df_exps = pd.concat([df_exps, df_links], ignore_index=True).drop_duplicates(
                subset=["exp_acc"]
            )
            log.info("  df_exps after link recovery: %d rows", len(df_exps))
    else:
        log.info("  All Norwegian samples covered by a Norwegian experiment ✓")

    # ── Join experiments → samples ────────────────────────────────────────────
    df_exp_sample = df_exps.merge(df_samples, on="sample_acc", how="left")

    # ── Aggregate experiment+sample signals per study ─────────────────────────
    def join_unique(series: pd.Series) -> str:
        vals = series.dropna()
        vals = vals[vals != ""]
        return " | ".join(sorted(set(vals)))

    exp_agg = df_exp_sample.groupby("study_acc", as_index=False).agg(
        n_experiments     = ("exp_acc",             "nunique"),
        # When a study's experiments carry different first_public_dates, use the
        # latest as the study's date (most recent public activity).  Dates are
        # YYYYMMDD compact, so lexicographic max == chronological max.
        first_public_date = ("first_public_date",   lambda s: max((v for v in s if v), default="")),
        exp_countries     = ("exp_country",         join_unique),
        exp_centers       = ("exp_center",          join_unique),
        sample_countries  = ("sample_country",      join_unique),
        sample_centers    = ("sample_center",       join_unique),
        sample_brokers    = ("sample_broker",       join_unique),
        sample_regions    = ("sample_region",       join_unique),
        exp_text_blob     = ("exp_text",            lambda s: " ".join(s.dropna())),
        sample_text_blob  = ("sample_text",         lambda s: " ".join(s.dropna())),
    )

    # ── Assemble master join ──────────────────────────────────────────────────
    df = df_studies.merge(exp_agg, on="study_acc", how="left")

    for col in ("n_experiments",):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    log.info("Master join: %d studies × %d columns", len(df), len(df.columns))

    # ── Norwegian filter ──────────────────────────────────────────────────────
    # Split signal columns the same way is_norwegian_entry() splits raw fields:
    # identity columns (exp_countries/exp_centers/sample_*) reliably name an
    # institution or its country, so bare-abbreviation patterns (abbrev_filter)
    # are trusted there.  The free-text blobs (study_text, exp_text_blob,
    # sample_text_blob) are built from abstract/description/alias/study_keywords
    # etc. — specimen/strain-code-prone fields (see SPECIMEN_LIKE_FIELDS) where a
    # bare abbreviation match is unreliable — so only safe_filter (geo names,
    # full institution names, guarded abbreviations, .no email) is checked there.
    identity_cols = [
        "center_name",
        "exp_countries", "exp_centers",
        "sample_countries", "sample_centers", "sample_brokers", "sample_regions",
    ]
    text_cols = ["study_text", "exp_text_blob", "sample_text_blob"]
    identity_cols = [c for c in identity_cols if c in df.columns]
    text_cols = [c for c in text_cols if c in df.columns]

    # Column-wise concatenation rather than a row-wise .agg(" ".join, axis=1):
    # the latter degrades to returning a DataFrame instead of a Series when df
    # has 0 rows (e.g. sra-study fetch came back empty), breaking the
    # .str.replace() below.  Plain object dtype (not "string") avoids a
    # per-iteration StringDtype coercion that roughly doubles the cost of
    # this loop at the ~730K-row scale sra-study can reach.
    def _concat_cols(cols: list[str]) -> pd.Series:
        blob = pd.Series("", index=df.index, dtype=object)
        for i, col in enumerate(cols):
            sep = " " if i else ""
            blob = blob + sep + df[col].fillna("").astype(str)
        return blob

    # Drop species vernaculars ("Norway spruce", "Norway rat", …) so a study
    # is not flagged Norwegian solely because its title names such a species.
    all_blob = _concat_cols(identity_cols + text_cols).str.replace(
        FALSE_POSITIVE_RE, " ", regex=True
    )
    identity_blob = _concat_cols(identity_cols).str.replace(
        FALSE_POSITIVE_RE, " ", regex=True
    )
    mask = (
        all_blob.str.contains(safe_filter.pattern, regex=True, flags=re.IGNORECASE, na=False)
        | identity_blob.str.contains(abbrev_filter.pattern, regex=True, flags=re.IGNORECASE, na=False)
    )
    df_nor = df[mask].copy()

    log.info("Norwegian filter: kept %d / %d studies", len(df_nor), len(df))

    # ── Backfill missing study dates ──────────────────────────────────────────
    # A study kept on a study/sample text signal whose experiments are all
    # non-Norwegian (and thus filtered out of df_exps) has no first_public_date
    # after the join; the R render drops those rows.  Re-query sra-experiment
    # directly for exactly those studies to recover the date.
    fpd = df_nor["first_public_date"] if "first_public_date" in df_nor.columns \
        else pd.Series("", index=df_nor.index, dtype="string")
    undated_mask = fpd.isna() | (fpd.astype("string").fillna("") == "")
    undated = [a for a in df_nor.loc[undated_mask, "study_acc"].dropna().tolist() if a]
    if undated:
        log.info("  %d Norwegian studies lack a date – backfilling from sra-experiment …",
                 len(undated))
        date_map = _fetch_study_dates(undated)
        if date_map:
            backfilled = df_nor.loc[undated_mask, "study_acc"].map(date_map)
            df_nor.loc[undated_mask, "first_public_date"] = backfilled
            df_nor["first_public_date"] = df_nor["first_public_date"].fillna("")
            log.info("  Backfilled dates for %d / %d studies", len(date_map), len(undated))
        else:
            log.info("  No dates recovered (offline or no matching experiments)")

    # ── Serialise ─────────────────────────────────────────────────────────────
    output_cols = {
        "study_acc":          "accession",
        "title":              "title",
        "description":        "description",
        "center_name":        "center_name",
        "first_public_date":  "first_public_date",
        "sample_countries":   "sample_countries_str",
        "sample_centers":     "sample_centers_str",
        "sample_brokers":     "sample_brokers_str",
        "n_experiments":      "n_experiments",
    }
    present = {k: v for k, v in output_cols.items() if k in df_nor.columns}
    df_out = df_nor[list(present.keys())].rename(columns=present)

    def pipe_to_list(s) -> list:
        if pd.isna(s) or s == "":
            return []
        return [x.strip() for x in str(s).split("|") if x.strip()]

    entries: list[dict] = []
    for row in df_out.to_dict(orient="records"):
        row["source"] = "ENA"
        row["domain"] = "sra-study"
        row["sample_countries"] = pipe_to_list(row.pop("sample_countries_str", ""))
        row["sample_centers"]   = pipe_to_list(row.pop("sample_centers_str",   ""))
        row["sample_brokers"]   = pipe_to_list(row.pop("sample_brokers_str",   ""))
        if "n_experiments" in row:
            row["n_experiments"] = int(row["n_experiments"]) if pd.notna(row["n_experiments"]) else 0
        entries.append(row)

    out = {
        "join_date":   TODAY,
        "study_count": len(entries),
        "entries":     entries,
    }

    payload = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    (PROC_DIR / f"ena_joined_{TODAY}.json").write_text(payload)
    latest = PROC_DIR / "ena_joined.json"
    latest.write_text(payload)
    log.info("Wrote %s (%d Norwegian studies) ✓", latest, len(entries))


if __name__ == "__main__":
    main()

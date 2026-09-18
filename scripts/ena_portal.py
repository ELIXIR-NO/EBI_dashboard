#!/usr/bin/env python3
"""
ena_portal.py
=============
Low-level client for ENA's Portal API (https://www.ebi.ac.uk/ena/portal/api).

This is a DIFFERENT service from the EBI Search REST API wrapped by
ebi_api.py, and it exists here for one reason: broker attribution.

EBI Search's sra-sample domain marks ``broker_name`` searchable and facetable
but NOT retrievable, so every entry fetched through fetch_ebi_data.py comes
back with the field absent, no matter what ENA actually holds.  The dashboard
therefore fell back to ``center_name`` for every sample and showed the
depositing institution (e.g. "Norwegian Institute of Public Health (NIPH)")
where the broker ("ELIXIR-Norway") should have been.  The Portal API exposes
broker_name as a clean field, separate from center_name, so it is used here to
backfill what EBI Search withholds.

Like ebi_api.py this module knows only about HTTP and imports nothing from the
other scripts, keeping the import graph acyclic.
"""

import logging
import time

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

log = logging.getLogger("ena_portal")

SEARCH_URL = "https://www.ebi.ac.uk/ena/portal/api/search"

# Accessions per request.  The batch is sent as a POST body rather than a query
# string, so the practical GET URL-length ceiling (~50 accessions) does not
# apply; 1000 keeps each response small and well inside the API's limits while
# cutting a 174 K-sample backfill from ~3500 requests to ~175.
BROKER_BATCH = 1000

RATE_SLEEP = 0.2


def _fetch_fields_by(result_type: str, key_field: str, fields: list[str],
                     accs: list[str], batch_size: int) -> dict[str, dict]:
    """
    POST `accs` to the Portal in batches, asking `result_type` for `fields`
    keyed on `key_field`.  Returns {accession: {field: value}} for every
    accession that resolved, with values stripped.

    Best-effort: a batch that errors is logged and skipped, so a network blip
    costs that batch's attribution rather than the whole lookup.
    """
    result: dict[str, dict] = {}
    n_batches = (len(accs) + batch_size - 1) // batch_size
    field_spec = ",".join([key_field] + fields)

    for i in range(0, len(accs), batch_size):
        batch = accs[i: i + batch_size]
        query = " OR ".join(f'{key_field}="{acc}"' for acc in batch)
        try:
            resp = requests.post(
                SEARCH_URL,
                data={
                    "result": result_type,
                    "query":  query,
                    "fields": field_spec,
                    "format": "json",
                    "limit":  str(len(batch)),
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json() or []
        except Exception as exc:
            log.warning("    %s lookup batch %d/%d failed: %s",
                        result_type, i // batch_size + 1, n_batches, exc)
            continue

        for entry in data:
            acc = (entry.get(key_field) or "").strip()
            if acc:
                result[acc] = {f: (entry.get(f) or "").strip() for f in fields}

        if i + batch_size < len(accs):
            time.sleep(RATE_SLEEP)

    return result


def fetch_broker_names(sample_accs: list[str],
                       batch_size: int = BROKER_BATCH) -> dict[str, str]:
    """
    Look up broker_name for the given sample accessions via ENA's Portal API.

    `sample_accs` are BioSample accessions (SAMEA…/SAMN…), which is what both
    EBI Search's sra-sample entry ids and the Portal's `sample_accession`
    field use.

    Best-effort: network failures are logged and that batch is skipped so the
    caller continues with whatever it already had.  Returns
    {sample_accession: broker_name}, omitting accessions with no broker on
    record, so a plain ``.get()``/``.map()`` leaves those untouched.
    """
    if not _REQUESTS_AVAILABLE:
        log.warning("requests not installed – cannot backfill ENA broker_name")
        return {}

    accs = [a for a in dict.fromkeys(sample_accs) if a]
    if not accs:
        return {}

    rows = _fetch_fields_by("sample", "sample_accession", ["broker_name"],
                            accs, batch_size)
    return {acc: r["broker_name"] for acc, r in rows.items() if r["broker_name"]}


def fetch_study_attribution(study_accs: list[str],
                            batch_size: int = BROKER_BATCH) -> dict[str, dict]:
    """
    Look up a study's OWN broker_name and center_name via ENA's Portal API.

    Why this exists: EBI Search's sra-study domain exposes neither field — it
    has no broker_name at all, and its `center_project_name` (what the join
    step reads as center_name) is empty for every study in practice.  The join
    used to paper over both gaps by inheriting a broker from the study's
    samples, which was wrong twice over: it missed the studies ENA really does
    record a broker for (the 'ELIXIR-Norway' ones the dashboard is meant to
    surface), and for everything else it promoted the sample's INSDC mirroring
    provenance ('NCBI', 'DDBJ' — the archive the record was copied from) into a
    submission broker it never was.  Dropping that inheritance without filling
    center_name would leave the studies with no attribution at all, so both
    fields come from the Portal's `study` result, which carries them directly.

    Accessions may be secondary (ERP/SRP/DRP) or primary (PRJ…): the Portal
    keys those on different columns, so the secondary column is tried first
    and whatever it leaves unresolved is retried against the primary one.
    `first_public` (ISO YYYY-MM-DD) comes back on the same call: a study with
    no experiments left in the join has no date either, and the render drops
    undated rows — which silently hid several ELIXIR-Norway studies.

    Returns {accession_as_passed_in: {"broker_name": …, "center_name": …,
    "first_public": …}}, with any value possibly "" when ENA holds none.
    """
    if not _REQUESTS_AVAILABLE:
        log.warning("requests not installed – cannot look up ENA study attribution")
        return {}

    accs = [a for a in dict.fromkeys(study_accs) if a]
    if not accs:
        return {}

    fields = ["broker_name", "center_name", "first_public"]
    result = _fetch_fields_by("study", "secondary_study_accession", fields,
                              accs, batch_size)
    remaining = [a for a in accs if a not in result]
    if remaining:
        result.update(_fetch_fields_by("study", "study_accession", fields,
                                       remaining, batch_size))
    return result

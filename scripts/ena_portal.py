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

    result: dict[str, str] = {}
    n_batches = (len(accs) + batch_size - 1) // batch_size

    for i in range(0, len(accs), batch_size):
        batch = accs[i: i + batch_size]
        query = " OR ".join(f'sample_accession="{acc}"' for acc in batch)
        try:
            resp = requests.post(
                SEARCH_URL,
                data={
                    "result": "sample",
                    "query":  query,
                    "fields": "sample_accession,broker_name",
                    "format": "json",
                    "limit":  str(len(batch)),
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json() or []
        except Exception as exc:
            log.warning("    Broker-name backfill batch %d/%d failed: %s",
                        i // batch_size + 1, n_batches, exc)
            continue

        for entry in data:
            acc    = (entry.get("sample_accession") or "").strip()
            broker = (entry.get("broker_name") or "").strip()
            if acc and broker:
                result[acc] = broker

        if i + batch_size < len(accs):
            time.sleep(RATE_SLEEP)

    return result

"""
Regression tests for the sra-sample broker_name backfill.

Background
----------
EBI Search's sra-sample domain marks `broker_name` searchable and facetable
but `retrievable: false`, so it is absent from every entry fetch_ebi_data.py
saves even when ENA holds a broker on record.  The dashboard reads that field
per sample and falls back to `center_name` when it is empty, so every sample
rendered its depositing institution ("Norwegian Institute of Public Health
(NIPH)") where the broker ("ELIXIR-Norway") belongs.

join_ena.py already backfilled the field for the ~1 K joined study rows; these
tests cover the far larger sra-sample table, which the dashboard renders
directly and which is where the wrong names were actually showing.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


fetch_ebi_data = _load_module("fetch_ebi_data", "scripts/fetch_ebi_data.py")


def _entry(acc, center, broker=None):
    fields = {"acc": [acc], "center_name": [center], "country": ["Norway"]}
    if broker is not None:
        fields["broker_name"] = [broker]
    return {"id": acc, "fields": fields}


def test_backfill_fills_broker_name_the_index_withholds(monkeypatch):
    entries = [
        _entry("SAMEA1", "Norwegian Institute of Public Health (NIPH)"),
        _entry("SAMEA2", "University of Bergen"),
    ]
    monkeypatch.setattr(
        fetch_ebi_data, "fetch_broker_names",
        lambda accs: {"SAMEA1": "ELIXIR-Norway", "SAMEA2": "ELIXIR-Norway"},
    )

    filled = fetch_ebi_data.backfill_broker_names("sra-sample", entries)

    assert filled == 2
    # Written in EBI Search's own list shape so the R reader needs no special case.
    assert entries[0]["fields"]["broker_name"] == ["ELIXIR-Norway"]
    assert entries[1]["fields"]["broker_name"] == ["ELIXIR-Norway"]
    # center_name is left intact — it stays the fallback, not the display value.
    assert entries[0]["fields"]["center_name"] == \
        ["Norwegian Institute of Public Health (NIPH)"]


def test_backfill_never_overwrites_a_broker_already_present(monkeypatch):
    entries = [_entry("SAMEA1", "University of Oslo", broker="UiO")]

    def fake_fetch(accs):
        # Entries that already carry a broker must not even be looked up.
        assert accs == []
        return {}

    monkeypatch.setattr(fetch_ebi_data, "fetch_broker_names", fake_fetch)

    assert fetch_ebi_data.backfill_broker_names("sra-sample", entries) == 0
    assert entries[0]["fields"]["broker_name"] == ["UiO"]


def test_backfill_leaves_samples_with_no_broker_on_record_alone(monkeypatch):
    # A sample ENA has no broker for keeps an empty broker_name, so the R side
    # falls back to center_name for it — the intended fallback, not a bug.
    entries = [_entry("SAMEA1", "University of Bergen")]
    monkeypatch.setattr(fetch_ebi_data, "fetch_broker_names", lambda accs: {})

    assert fetch_ebi_data.backfill_broker_names("sra-sample", entries) == 0
    assert not entries[0]["fields"].get("broker_name")


def test_backfill_is_limited_to_domains_that_carry_biosample_ids(monkeypatch):
    # sra-experiment entry ids are ERX…, not SAMEA…, so querying the Portal's
    # sample_accession with them would be meaningless.
    called = []
    monkeypatch.setattr(fetch_ebi_data, "fetch_broker_names",
                        lambda accs: called.append(accs) or {})

    entries = [_entry("ERX1", "University of Oslo")]
    assert fetch_ebi_data.backfill_broker_names("sra-experiment", entries) == 0
    assert called == []


def test_backfill_dedupes_repeated_accessions_and_fills_every_copy(monkeypatch):
    # Overlapping partition windows surface the same sample more than once;
    # each copy must end up with the broker, from a single lookup.
    entries = [
        _entry("SAMEA1", "Norwegian Institute of Public Health (NIPH)"),
        _entry("SAMEA1", "Norwegian Institute of Public Health (NIPH)"),
    ]
    seen = []

    def fake_fetch(accs):
        seen.append(list(accs))
        return {"SAMEA1": "ELIXIR-Norway"}

    monkeypatch.setattr(fetch_ebi_data, "fetch_broker_names", fake_fetch)

    assert fetch_ebi_data.backfill_broker_names("sra-sample", entries) == 2
    assert seen == [["SAMEA1"]]
    assert all(e["fields"]["broker_name"] == ["ELIXIR-Norway"] for e in entries)


def test_backfill_runs_before_the_entries_are_saved(monkeypatch, tmp_path):
    # The dashboard only ever reads the saved JSON, so a backfill that happened
    # after save_domain() would be invisible to it.
    monkeypatch.setattr(fetch_ebi_data, "RAW_DIR", tmp_path)
    monkeypatch.setattr(fetch_ebi_data, "get_cached_filter_tiers",
                        lambda: (None, None))
    monkeypatch.setattr(fetch_ebi_data, "get_retrievable_fields",
                        lambda domain, cfg: ["acc", "center_name"])
    monkeypatch.setattr(
        fetch_ebi_data, "fetch_domain",
        lambda domain, cfg, fields, safe, abbrev: [
            _entry("SAMEA1", "Norwegian Institute of Public Health (NIPH)")
        ],
    )
    monkeypatch.setattr(fetch_ebi_data, "fetch_broker_names",
                        lambda accs: {"SAMEA1": "ELIXIR-Norway"})

    saved = {}
    monkeypatch.setattr(fetch_ebi_data, "save_domain",
                        lambda domain, entries, fields: saved.update(
                            entries=[dict(e) for e in entries]))

    fetch_ebi_data.fetch_and_save_domain("sra-sample")

    assert saved["entries"][0]["fields"]["broker_name"] == ["ELIXIR-Norway"]

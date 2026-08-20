"""Regression tests for the EGA DAC Norwegian filter (scripts/fetch_ega.py).

fetch_ega.py consumes load_institution_regexes() directly rather than through
get_cached_filter_tiers(), so it does not share the wiring the other fetchers
are covered by.  When that loader grew a second return value (safe patterns +
bare abbreviations), fetch_ega.py kept treating the result as one flat list and
died with "'list' object has no attribute 'search'" — but only after ~7 minutes
of live API paging, deep inside a scheduled run.  These tests pin the contract
and the filter behaviour so the same drift fails in CI instead.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_ega
from norwegian_filter import (
    build_geo_regex, load_geo_tokens, load_institution_regexes, load_web_domains,
)


def _filter_args():
    """Build the filter arguments exactly as fetch_ega.main() does."""
    geo_re = build_geo_regex(load_geo_tokens())
    inst_safe, inst_abbrev = load_institution_regexes()
    return geo_re, inst_safe + inst_abbrev, load_web_domains()


def _dac(accession="EGAC00001000000", contacts=None):
    return {"accession_id": accession, "contacts": contacts or []}


def test_load_institution_regexes_returns_two_pattern_tiers():
    """The contract fetch_ega.main() unpacks — two lists of compiled patterns."""
    result = load_institution_regexes()
    assert isinstance(result, tuple) and len(result) == 2
    safe, abbrev = result
    assert safe and abbrev, "both tiers should be non-empty for the shipped map"
    for pattern in (*safe, *abbrev):
        assert isinstance(pattern, re.Pattern)


def test_dac_signal_matches_full_institution_name():
    geo_re, inst_regexes, web_domains = _filter_args()
    dac = _dac(contacts=[{"institution_name": "Oslo University Hospital",
                          "email": "someone@example.org"}])
    insts, emails = fetch_ega.dac_norwegian_signal(dac, geo_re, inst_regexes, web_domains)
    assert insts == ["Oslo University Hospital"]
    assert emails == []


def test_dac_signal_matches_bare_abbreviation_institution_name():
    """institution_name is identity-bearing, so the abbreviation tier applies."""
    geo_re, inst_regexes, web_domains = _filter_args()
    dac = _dac(contacts=[{"institution_name": "NTNU", "email": ""}])
    insts, _ = fetch_ega.dac_norwegian_signal(dac, geo_re, inst_regexes, web_domains)
    assert insts == ["NTNU"]


def test_dac_signal_matches_norwegian_email_domain():
    geo_re, inst_regexes, web_domains = _filter_args()
    dac = _dac(contacts=[{"institution_name": "Department of Clinical Science",
                          "email": "kristian.lovas@uib.no"}])
    insts, emails = fetch_ega.dac_norwegian_signal(dac, geo_re, inst_regexes, web_domains)
    assert emails == ["kristian.lovas@uib.no"]
    assert insts == []


def test_dac_signal_ignores_name_like_email_local_part():
    """'nina.…@uni-saarland.de' must not match NINA — the domain decides."""
    geo_re, inst_regexes, web_domains = _filter_args()
    dac = _dac(contacts=[{"institution_name": "Saarland University",
                          "email": "nina.gasparoni@uni-saarland.de"}])
    assert fetch_ega.dac_norwegian_signal(dac, geo_re, inst_regexes, web_domains) == ([], [])


def test_dac_signal_deduplicates_repeated_contacts():
    geo_re, inst_regexes, web_domains = _filter_args()
    dac = _dac(contacts=[
        {"institution_name": "University of Oslo", "email": "a@medisin.uio.no"},
        {"institution_name": "University of Oslo", "email": "a@medisin.uio.no"},
    ])
    insts, emails = fetch_ega.dac_norwegian_signal(dac, geo_re, inst_regexes, web_domains)
    assert insts == ["University of Oslo"]
    assert emails == ["a@medisin.uio.no"]


def test_dac_signal_handles_null_contacts():
    """The live /dacs endpoint returns rows with contacts: null."""
    geo_re, inst_regexes, web_domains = _filter_args()
    assert fetch_ega.dac_norwegian_signal(
        {"accession_id": "", "contacts": None}, geo_re, inst_regexes, web_domains
    ) == ([], [])


def test_main_passes_flat_compiled_patterns_to_the_collector(monkeypatch):
    """Guards main()'s wiring itself — the line that actually broke.

    main() must hand collect_norwegian_records a flat list of compiled patterns.
    Passing load_institution_regexes()' raw 2-tuple through instead yields a list
    of *lists*, which only explodes once a DAC is inspected mid-run.
    """
    captured = {}

    def fake_collect(geo_re, inst_regexes, web_domains):
        captured["inst_regexes"] = inst_regexes
        captured["geo_re"] = geo_re
        return {}, {}

    monkeypatch.setattr(fetch_ega, "collect_norwegian_records", fake_collect)
    monkeypatch.setattr(fetch_ega, "save_domain", lambda *a, **k: None)

    assert fetch_ega.main() == 0
    assert isinstance(captured["geo_re"], re.Pattern)
    patterns = captured["inst_regexes"]
    assert patterns, "institution patterns should not be empty"
    assert all(isinstance(p, re.Pattern) for p in patterns), (
        f"got non-pattern elements: {[type(p).__name__ for p in patterns if not isinstance(p, re.Pattern)]}"
    )


# ── Failure guards ────────────────────────────────────────────────────────────

import json

import pytest

import ega_api


def _write_snapshot(tmp_path, domain, entries):
    d = tmp_path / domain
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(
        json.dumps({"domain": domain, "fetch_date": "2026-08-01",
                    "entry_count": len(entries), "entries": entries}),
        encoding="utf-8",
    )


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    """Redirect the guards' snapshot lookups at a throwaway data/raw."""
    monkeypatch.setattr(fetch_ega, "RAW_DIR", tmp_path)
    return tmp_path


def test_screening_survives_one_malformed_dac(raw_dir):
    """A bad row is skipped; the rest of the expensive page-walk still counts."""
    geo_re, inst_regexes, web_domains = _filter_args()
    dacs = [
        {"accession_id": "EGAC00000000001",
         "contacts": [{"institution_name": "University of Bergen", "email": ""}]},
        {"accession_id": "EGAC00000000002", "contacts": "not-a-list"},
        {"accession_id": "EGAC00000000003",
         "contacts": [{"institution_name": "NTNU", "email": ""}]},
    ]
    accs = []
    for dac in dacs:
        try:
            insts, emails = fetch_ega.dac_norwegian_signal(
                dac, geo_re, inst_regexes, web_domains)
        except Exception:
            continue
        if insts or emails:
            accs.append(dac["accession_id"])
    assert accs == ["EGAC00000000001", "EGAC00000000003"]


def test_systematic_screening_failure_raises(raw_dir, monkeypatch):
    """Every DAC failing means our filter is broken — never 'no Norwegian DACs'."""
    monkeypatch.setattr(fetch_ega, "get_dacs", lambda: [
        {"accession_id": f"EGAC0000000000{i}",
         "contacts": [{"institution_name": "University of Oslo", "email": ""}]}
        for i in range(3)
    ])

    def boom(*a, **k):
        raise AttributeError("'list' object has no attribute 'search'")

    monkeypatch.setattr(fetch_ega, "dac_norwegian_signal", boom)
    with pytest.raises(RuntimeError, match="filter is broken"):
        fetch_ega.collect_norwegian_records(*_filter_args())


def test_api_outage_keeps_previous_snapshot(raw_dir, monkeypatch):
    """A transient outage must not sink the pipeline, nor rewrite good data."""
    _write_snapshot(raw_dir, fetch_ega.DOMAIN, [{"id": "EGAS1", "fields": {}}])
    _write_snapshot(raw_dir, fetch_ega.DOMAIN_SAMPLE, [{"id": "EGAN1", "fields": {}}])
    before = (raw_dir / fetch_ega.DOMAIN / "latest.json").read_text()

    def outage(*a, **k):
        raise ega_api.requests.ConnectionError("metadata.ega-archive.org unreachable")

    monkeypatch.setattr(fetch_ega, "collect_norwegian_records", outage)
    monkeypatch.setattr(fetch_ega, "save_domain", lambda *a, **k:
                        pytest.fail("save_domain must not run during an outage"))

    assert fetch_ega.main() == 0
    assert (raw_dir / fetch_ega.DOMAIN / "latest.json").read_text() == before


def test_api_outage_without_previous_snapshot_fails(raw_dir, monkeypatch):
    """Nothing to fall back on — exiting 0 would fake a successful fetch."""
    def outage(*a, **k):
        raise ega_api.requests.ConnectionError("metadata.ega-archive.org unreachable")

    monkeypatch.setattr(fetch_ega, "collect_norwegian_records", outage)
    monkeypatch.setattr(fetch_ega, "save_domain", lambda *a, **k: None)
    assert fetch_ega.main() == 1


def test_programming_errors_are_not_masked_as_an_outage(raw_dir, monkeypatch):
    """A bug must surface, not be laundered into 'keeping stale EGA data'."""
    _write_snapshot(raw_dir, fetch_ega.DOMAIN, [{"id": "EGAS1", "fields": {}}])
    _write_snapshot(raw_dir, fetch_ega.DOMAIN_SAMPLE, [{"id": "EGAN1", "fields": {}}])

    monkeypatch.setattr(fetch_ega, "save_domain", lambda *a, **k: None)

    # ValueError is included deliberately: json.JSONDecodeError subclasses it,
    # so a too-broad TRANSIENT_ERRORS would report our own bug as an outage.
    for exc_type in (AttributeError, TypeError, KeyError, ValueError):
        def bug(*a, _e=exc_type, **k):
            raise _e("boom")

        monkeypatch.setattr(fetch_ega, "collect_norwegian_records", bug)
        with pytest.raises(exc_type):
            fetch_ega.main()


def test_empty_result_never_overwrites_a_populated_snapshot(raw_dir, monkeypatch):
    """Accessions are only ever added, so a drop to zero is a fault."""
    _write_snapshot(raw_dir, fetch_ega.DOMAIN, [{"id": "EGAS1", "fields": {}}])
    _write_snapshot(raw_dir, fetch_ega.DOMAIN_SAMPLE, [{"id": "EGAN1", "fields": {}}])

    monkeypatch.setattr(fetch_ega, "collect_norwegian_records",
                        lambda *a, **k: ({}, {}))
    monkeypatch.setattr(fetch_ega, "save_domain", lambda *a, **k:
                        pytest.fail("save_domain must not clobber good data"))
    assert fetch_ega.main() == 1


def test_empty_result_is_saved_on_a_first_run(raw_dir, monkeypatch):
    """With no prior snapshot there is nothing to protect — write the result."""
    saved = []
    monkeypatch.setattr(fetch_ega, "collect_norwegian_records",
                        lambda *a, **k: ({}, {}))
    monkeypatch.setattr(fetch_ega, "save_domain",
                        lambda domain, entries, fields: saved.append(domain))
    assert fetch_ega.main() == 0
    assert saved == [fetch_ega.DOMAIN, fetch_ega.DOMAIN_SAMPLE]

"""
Regression tests for _fetch_window's pagination.

Background
----------
The EBI Search API caps deep paging: `start` at or beyond MAX_PAGEABLE returns
an empty entries list (HTTP 200, true hitCount still reported), and `start` at
or beyond a query's own hitCount answers HTTP 400.  A naive start-offset loop
therefore stops at the cap and looks like a clean finish — which silently
truncated sra-study at 100 000 of its 753 568 entries.

These tests drive _fetch_window against a fake API that reproduces both
behaviours, so the truncation cannot come back unnoticed.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

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

MATCH_ALL = re.compile(r"")
MATCH_NONE = re.compile(r"(?!x)x")

# Range clause the keyset cursor appends, e.g. "acc:[ERP000123 TO *]".
_CURSOR_RE = re.compile(r"acc:\[(?P<lo>[^\s\]]+) TO \*\]")


class FakeHTTP400(Exception):
    """What the real API raises once `start` runs past a query's hitCount."""


class FakeEbiSearch:
    """
    Minimal stand-in for the EBI Search API over a sorted corpus of accessions.

    Reproduces the two quirks that matter: a hard deep-paging cap, and HTTP 400
    (raised as an exception) once `start` runs past a query's own hitCount.
    """

    def __init__(self, accessions, cap):
        self.corpus = sorted(accessions)
        self.cap = cap
        self.requests = 0
        # Entries the index claims to hold but never serves, to model a window
        # that comes back short of its own reported hitCount.
        self.phantom_hits = 0

    def hit_count(self, query):
        return len(self._matching(query)) + self.phantom_hits

    def _matching(self, query):
        m = _CURSOR_RE.search(query)
        if not m:
            return self.corpus
        lo = m.group("lo")
        return [a for a in self.corpus if a >= lo]   # inclusive, as the API is

    def get_json(self, url, params):
        self.requests += 1
        hits = self._matching(params["query"])
        start, size = params["start"], params["size"]
        if start >= len(hits) and start > 0:
            raise FakeHTTP400(
                f"request past hitCount (start={start}, hitCount={len(hits)})"
            )
        window = [] if start >= self.cap else hits[start:start + size]
        return {
            "hitCount": self.hit_count(params["query"]),
            "entries": [{"id": a, "acc": a, "source": "sra-study"} for a in window],
        }


@pytest.fixture
def fake_api(monkeypatch):
    """Install a FakeEbiSearch and shrink the cap so the cursor path is exercised."""

    def _install(n_entries, cap, page_size=50, prefix="ERP"):
        accs = [f"{prefix}{i:06d}" for i in range(n_entries)]
        api = FakeEbiSearch(accs, cap)
        monkeypatch.setattr(fetch_ebi_data, "get_json", api.get_json)
        # Must be stubbed too, or _fetch_window probes the live API for the
        # window size and the whole test stops being hermetic.
        monkeypatch.setattr(fetch_ebi_data, "get_hit_count",
                            lambda domain, fields, query: api.hit_count(query))
        monkeypatch.setattr(fetch_ebi_data, "MAX_PAGEABLE", cap)
        monkeypatch.setattr(fetch_ebi_data, "PAGE_SIZE", page_size)
        monkeypatch.setattr(fetch_ebi_data, "RATE_SLEEP", 0)
        return api

    return _install


def _fetch(query="*:*", domain="sra-study"):
    return fetch_ebi_data._fetch_window(
        domain, ["acc", "id"], query, MATCH_ALL, MATCH_ALL
    )


def test_window_far_above_cap_is_fetched_completely(fake_api):
    """The reported regression: 7x the cap must not stop at the cap."""
    fake_api(n_entries=3500, cap=500, page_size=50)
    entries, seen = _fetch()
    assert seen == 3500
    assert len({e["id"] for e in entries}) == 3500


def test_cursor_boundary_entry_is_not_duplicated(fake_api):
    """The inclusive `[cursor TO *]` bound re-serves one entry per chunk."""
    fake_api(n_entries=1000, cap=200, page_size=100)
    entries, seen = _fetch()
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids)) == 1000
    assert seen == 1000


def test_below_cap_matches_cursor_path_exactly(fake_api):
    """Turning the cursor on must not change which entries come back."""
    fake_api(n_entries=800, cap=10_000, page_size=50)
    plain, plain_seen = _fetch()
    fake_api(n_entries=800, cap=200, page_size=50)
    cursored, cursor_seen = _fetch()
    assert plain_seen == cursor_seen == 800
    assert [e["id"] for e in plain] == sorted(e["id"] for e in cursored)


def test_never_requests_past_hit_count(fake_api, caplog):
    """
    Guards the HTTP 400 the API raises once start >= hitCount.  _fetch_window
    catches request errors, so an overrun shows up as a short read plus a
    logged failure rather than a raised exception.
    """
    # n_entries a clean multiple of page_size is the case that used to overrun.
    fake_api(n_entries=600, cap=200, page_size=100)
    with caplog.at_level("ERROR"):
        _, seen = _fetch()
    assert seen == 600
    assert not [r for r in caplog.records if "Window fetch failed" in r.getMessage()]


def test_exact_cap_multiple_is_complete(fake_api):
    fake_api(n_entries=1000, cap=500, page_size=100)
    _, seen = _fetch()
    assert seen == 1000


def test_empty_result_set(fake_api):
    fake_api(n_entries=0, cap=500, page_size=100)
    entries, seen = _fetch()
    assert entries == [] and seen == 0


def test_cap_equal_to_page_size_still_completes(fake_api):
    """
    One page per chunk is enough to advance: the chunk re-reads only the single
    boundary entry, so the other page_size-1 entries are progress.
    """
    fake_api(n_entries=500, cap=50, page_size=50)
    _, seen = _fetch()
    assert seen == 500


def test_stalled_cursor_is_reported_not_silent(fake_api, caplog):
    """
    A one-entry page is entirely consumed by the boundary re-read, so the
    cursor can never advance.  That must fail loudly — silent truncation is the
    bug this module exists to prevent.
    """
    fake_api(n_entries=500, cap=1, page_size=1)
    with caplog.at_level("ERROR"):
        _, seen = _fetch()
    assert seen < 500
    messages = [r.getMessage() for r in caplog.records]
    assert any("stalled" in m for m in messages)
    assert any("INCOMPLETE" in m for m in messages)


def test_short_read_from_server_is_reported(fake_api, caplog):
    """A window that comes back short of its own hitCount must not pass quietly."""
    api = fake_api(n_entries=250, cap=10_000, page_size=100)
    api.phantom_hits = 250                 # index claims 500, only serves 250
    with caplog.at_level("ERROR"):
        _, seen = _fetch()
    assert seen < 500
    assert any("INCOMPLETE" in r.getMessage() for r in caplog.records)


def test_filtered_domain_still_sees_every_entry(fake_api):
    """
    sra-sample is filtered at page level; the filter must narrow what is kept
    without narrowing what is paged through.
    """
    fake_api(n_entries=1200, cap=300, page_size=100)
    entries, seen = fetch_ebi_data._fetch_window(
        "sra-sample", ["acc", "id"], "*:*", MATCH_NONE, MATCH_NONE
    )
    assert seen == 1200
    assert entries == []


def test_max_pageable_matches_the_real_api_cap():
    """
    Verified against the live API: sra-study reports hitCount=753568 but
    returns an empty entries list at start=100000.
    """
    assert fetch_ebi_data.MAX_PAGEABLE == 100_000
    assert fetch_ebi_data.MAX_PAGEABLE >= 2 * fetch_ebi_data.PAGE_SIZE

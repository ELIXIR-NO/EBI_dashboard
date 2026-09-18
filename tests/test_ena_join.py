import importlib.util
import json
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


join_ena = _load_module("join_ena", "scripts/join_ena.py")

import ena_portal  # noqa: E402  (needs SCRIPTS_DIR on sys.path)


def test_join_ena_main_writes_summary_for_valid_norwegian_study(tmp_path, monkeypatch):
    raw_dir = tmp_path / "data" / "raw"
    proc_dir = tmp_path / "data" / "processed"
    monkeypatch.setattr(join_ena, "RAW_DIR", raw_dir)
    monkeypatch.setattr(join_ena, "PROC_DIR", proc_dir)
    proc_dir.mkdir(parents=True, exist_ok=True)

    for domain, entries in {
        "sra-study": [{
            "id": "ERP000001",
            "fields": {
                "acc": ["ERP000001"],
                "abstract": ["Study on marine biodiversity"],
                "description": ["A Norwegian project"],
                "center_project_name": ["University of Oslo"],
                "alias": ["ERP000001"],
                "study_keywords": ["marine"],
                "study_type": ["Genome sequencing"],
            },
        }],
        "sra-experiment": [{
            "id": "ERX000001",
            "fields": {
                "acc": ["ERX000001"],
                "SRA-STUDY": ["ERP000001"],
                "SAMPLE": ["SAMEA000001"],
                "first_public_date": ["20240115"],
                "country": ["Norway"],
                "center_name": ["University of Oslo"],
                "abstract": ["Experiment abstract"],
                "alias": ["ERX000001"],
                "description": ["Sampling"],
                "region": ["Oslo"],
            },
        }],
        "sra-sample": [{
            "id": "SAMEA000001",
            "fields": {
                "acc": ["SAMEA000001"],
                "country": ["Norway"],
                "center_name": ["University of Oslo"],
                "broker_name": ["UiO"],
                "region": ["Oslo"],
                "description": ["Marine sample"],
                "alias": ["SAMEA000001"],
            },
        }],
    }.items():
        domain_dir = raw_dir / domain
        domain_dir.mkdir(parents=True, exist_ok=True)
        (domain_dir / "latest.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")

    join_ena.main()

    output_path = proc_dir / "ena_joined.json"
    assert output_path.exists(), "join_ena main should write output JSON"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["study_count"] == 1
    assert payload["entries"][0]["accession"] == "ERP000001"
    assert payload["entries"][0]["domain"] == "sra-study"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_broker_names_backfills_from_ena_portal_api(monkeypatch):
    # EBI Search's sra-sample domain marks broker_name searchable/facetable but
    # not retrievable, so the regular fetch always sees "" for it even when
    # ENA has a broker on record (e.g. ELIXIR-Norway brokering samples for a
    # Norwegian institution).  fetch_broker_names() backfills it from ENA's
    # own Portal API, which exposes broker_name as a clean, separate field.
    calls = []

    def fake_post(url, data, timeout):
        calls.append((url, data))
        assert url == ena_portal.SEARCH_URL
        assert data["result"] == "sample"
        return _FakeResponse([
            {"sample_accession": "SAMEA11477150", "broker_name": "ELIXIR-Norway"},
            {"sample_accession": "SAMEA2", "broker_name": ""},
        ])

    monkeypatch.setattr(ena_portal, "_REQUESTS_AVAILABLE", True)
    monkeypatch.setattr(ena_portal, "requests",
                        type("R", (), {"post": staticmethod(fake_post)}))

    result = ena_portal.fetch_broker_names(["SAMEA11477150", "SAMEA2"])

    assert result == {"SAMEA11477150": "ELIXIR-Norway"}
    assert len(calls) == 1


def test_fetch_broker_names_batches_and_survives_a_failed_batch(monkeypatch):
    # A batch that errors must not sink the whole backfill: the remaining
    # batches still contribute, so the join proceeds with partial attribution
    # rather than silently falling back to center_name everywhere.
    seen = []

    def fake_post(url, data, timeout):
        batch_idx = len(seen)
        seen.append(data["query"])
        if batch_idx == 0:
            raise RuntimeError("boom")
        return _FakeResponse([{"sample_accession": "S3", "broker_name": "ELIXIR-Norway"}])

    monkeypatch.setattr(ena_portal, "_REQUESTS_AVAILABLE", True)
    monkeypatch.setattr(ena_portal, "requests",
                        type("R", (), {"post": staticmethod(fake_post)}))
    monkeypatch.setattr(ena_portal.time, "sleep", lambda _s: None)

    result = ena_portal.fetch_broker_names(["S1", "S2", "S3", "S4"], batch_size=2)

    assert len(seen) == 2
    assert result == {"S3": "ELIXIR-Norway"}


def test_fetch_study_brokers_falls_back_to_the_primary_accession_column(monkeypatch):
    # Joined study accessions are mostly secondary (ERP/SRP/DRP) but some are
    # primary (PRJ…), and the Portal keys those on different columns.  What the
    # secondary column does not resolve must be retried against the primary one,
    # or those studies silently lose their broker.
    calls = []

    def fake_post(url, data, timeout):
        calls.append(data["query"])
        if data["fields"].startswith("secondary_study_accession"):
            return _FakeResponse([
                {"secondary_study_accession": "ERP0001",
                 "broker_name": "ELIXIR-Norway"},
            ])
        return _FakeResponse([
            {"study_accession": "PRJEB0002", "broker_name": "ELIXIR-Norway"},
        ])

    monkeypatch.setattr(ena_portal, "_REQUESTS_AVAILABLE", True)
    monkeypatch.setattr(ena_portal, "requests",
                        type("R", (), {"post": staticmethod(fake_post)}))

    result = ena_portal.fetch_study_brokers(["ERP0001", "PRJEB0002"])

    assert result == {"ERP0001": "ELIXIR-Norway", "PRJEB0002": "ELIXIR-Norway"}
    # The second call retries only what the first left unresolved.
    assert "ERP0001" in calls[0] and "PRJEB0002" in calls[0]
    assert "ERP0001" not in calls[1] and "PRJEB0002" in calls[1]


def test_fetch_study_brokers_omits_studies_with_no_broker(monkeypatch):
    # A study ENA records no broker for must be absent from the map, so the
    # caller leaves it empty and the render falls back to center_name.
    def fake_post(url, data, timeout):
        return _FakeResponse([
            {"secondary_study_accession": "ERP0001", "broker_name": ""},
            {"secondary_study_accession": "ERP0002", "broker_name": None},
        ])

    monkeypatch.setattr(ena_portal, "_REQUESTS_AVAILABLE", True)
    monkeypatch.setattr(ena_portal, "requests",
                        type("R", (), {"post": staticmethod(fake_post)}))

    assert ena_portal.fetch_study_brokers(["ERP0001", "ERP0002"]) == {}


def test_load_samples_backfills_broker_only_where_missing(tmp_path, monkeypatch):
    raw_dir = tmp_path / "data" / "raw"
    monkeypatch.setattr(join_ena, "RAW_DIR", raw_dir)

    sample_dir = raw_dir / "sra-sample"
    sample_dir.mkdir(parents=True)
    (sample_dir / "latest.json").write_text(json.dumps({"entries": [
        # No broker_name from EBI Search (the real-world case) — should be
        # backfilled from the (mocked) Portal API.
        {"id": "SAMEA11477150", "fields": {
            "acc": ["SAMEA11477150"], "country": ["Norway"],
            "center_name": ["Norwegian Institute of Public Health (NIPH)"],
        }},
        # Already has a broker_name — must NOT be overwritten by the backfill.
        {"id": "SAMEA2", "fields": {
            "acc": ["SAMEA2"], "country": ["Norway"],
            "center_name": ["University of Oslo"], "broker_name": ["UiO"],
        }},
    ]}), encoding="utf-8")

    def fake_fetch_broker_names(accs):
        assert accs == ["SAMEA11477150"]  # SAMEA2 already has a broker; excluded
        return {"SAMEA11477150": "ELIXIR-Norway"}

    monkeypatch.setattr(join_ena, "fetch_broker_names", fake_fetch_broker_names)

    df = join_ena.load_samples()
    brokers = dict(zip(df["sample_acc"], df["sample_broker"]))
    assert brokers["SAMEA11477150"] == "ELIXIR-Norway"
    assert brokers["SAMEA2"] == "UiO"

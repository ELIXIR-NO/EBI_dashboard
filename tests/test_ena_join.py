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
    # ENA has a broker on record (e.g. ELIXIR Norway brokering samples for a
    # Norwegian institution).  _fetch_broker_names() backfills it from ENA's
    # own Portal API, which exposes broker_name as a clean, separate field.
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        assert url == join_ena._PORTAL_URL
        assert params["result"] == "sample"
        return _FakeResponse([
            {"sample_accession": "SAMEA11477150", "broker_name": "ELIXIR-Norway"},
            {"sample_accession": "SAMEA2", "broker_name": ""},
        ])

    monkeypatch.setattr(join_ena, "_REQUESTS_AVAILABLE", True)
    monkeypatch.setattr(join_ena, "_requests", type("R", (), {"get": staticmethod(fake_get)}))

    result = join_ena._fetch_broker_names(["SAMEA11477150", "SAMEA2"])

    assert result == {"SAMEA11477150": "ELIXIR-Norway"}
    assert len(calls) == 1


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

    monkeypatch.setattr(join_ena, "_fetch_broker_names", fake_fetch_broker_names)

    df = join_ena.load_samples()
    brokers = dict(zip(df["sample_acc"], df["sample_broker"]))
    assert brokers["SAMEA11477150"] == "ELIXIR-Norway"
    assert brokers["SAMEA2"] == "UiO"

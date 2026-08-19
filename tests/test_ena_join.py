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

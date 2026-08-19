import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_render_script_writes_expected_output_columns_when_data_present():
    raw_path = ROOT / "data" / "raw" / "pride" / "latest.json"
    raw_dir = raw_path.parent
    raw_dir.mkdir(parents=True, exist_ok=True)
    backup_path = raw_path.with_suffix(".bak.json")
    had_existing = raw_path.exists()
    if had_existing:
        backup_path.write_text(raw_path.read_text(encoding="utf-8"), encoding="utf-8")

    sample_json = {
        "entries": [
            {
                "id": "PXD000001",
                "fields": {
                    "submitter_affiliation": ["University of Oslo"],
                    "submitter_mail": ["uio@uio.no"],
                    "submission_date": ["2023-01-15"],
                    "name": ["Study 1"],
                    "title": ["Test project"],
                    "country": ["Norway"],
                }
            }
        ]
    }
    raw_path.write_text(json.dumps(sample_json), encoding="utf-8")

    try:
        expected_cols = [
            "domain",
            "domain_label",
            "accession",
            "title",
            "date",
            "year",
            "quarter",
            "month",
            "institution",
            "broker",
            "affiliation",
            "country",
            "email",
            "norwegian_submitter",
            "identifier_url",
        ]
        expected_r_vector = ", ".join(f'"{c}"' for c in sorted(expected_cols))

        script = f'''
          env <- new.env()
          source("{(ROOT / "R" / "plot_norwegian_data.R").as_posix()}", local = env)
          df <- env$load_all_data()
          expected_cols <- c({expected_r_vector})
          stopifnot(nrow(df) == 1)
          stopifnot(identical(sort(names(df)), sort(expected_cols)))
          cat("schema ok\n")
        '''

        result = subprocess.run(
            ["Rscript", "-e", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert "schema ok" in result.stdout
    finally:
        if had_existing:
            raw_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
            backup_path.unlink(missing_ok=True)
        else:
            raw_path.unlink(missing_ok=True)

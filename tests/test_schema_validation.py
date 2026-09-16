import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_render_script_writes_expected_output_columns_when_data_present(isolated_repo):
    # Runs in the isolated_repo tree, not the repo: sourcing the render script
    # runs it top to bottom, so with cwd=ROOT this would overwrite the
    # committed output/*.csv and output/*.png (see the fixture's docstring).
    # Isolation is also what makes the row count below meaningful — against the
    # real tree load_all_data() picks up data/processed/ena_joined.json and
    # every other domain on disk, so `df` holds ~1100 rows rather than the one
    # fixture entry.
    raw_path = isolated_repo / "data" / "raw" / "pride" / "latest.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)

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
      source("R/plot_norwegian_data.R", local = env)
      df <- env$load_all_data()
      expected_cols <- c({expected_r_vector})
      stopifnot(nrow(df) == 1)
      stopifnot(identical(sort(names(df)), sort(expected_cols)))
      cat("schema ok\n")
    '''

    result = subprocess.run(
        ["Rscript", "-e", script],
        cwd=str(isolated_repo),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "schema ok" in result.stdout
    # Guard against the isolation silently regressing: the render must have
    # written into the temporary tree, not into the repo.
    assert (isolated_repo / "output").is_dir(), result.stderr or result.stdout

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# SAMEA1 is fetched twice (overlapping partition windows) and only the second
# copy carries broker_name; ERP0001's broker lives on its samples while the
# study row itself only has a center_name.  Both are cases where a center used
# to win over a broker.
SRA_SAMPLES = {
    "entries": [
        {"id": "SAMEA1", "fields": {
            "center_name": ["University of Bergen"], "country": ["Norway"],
            "first_public_date": ["20240101"], "alias": ["copy without broker"]}},
        {"id": "SAMEA1", "fields": {
            "center_name": ["University of Bergen"], "broker_name": ["ELIXIR Norway"],
            "country": ["Norway"], "first_public_date": ["20240101"],
            "alias": ["copy with broker"]}},
        {"id": "SAMEA2", "fields": {
            "center_name": ["University of Oslo"], "country": ["Norway"],
            "first_public_date": ["20240201"], "alias": ["no broker at all"]}},
    ]
}

ENA_JOINED = {
    "join_date": "2026-01-01",
    "study_count": 2,
    "entries": [
        {"accession": "ERP0001", "title": "study with sample broker",
         "center_name": "University of Bergen", "first_public_date": "20240301",
         "sample_countries": ["Norway"], "sample_centers": ["University of Bergen"],
         "sample_brokers": ["ELIXIR Norway"], "n_experiments": 2},
        {"accession": "ERP0002", "title": "study without sample broker",
         "center_name": "University of Oslo", "first_public_date": "20240401",
         "sample_countries": ["Norway"], "sample_centers": ["University of Oslo"],
         "sample_brokers": [], "n_experiments": 1},
    ],
}


def test_center_never_overwrites_broker_for_ena_entries():
    fixtures = {
        ROOT / "data" / "raw" / "sra-sample" / "latest.json": SRA_SAMPLES,
        ROOT / "data" / "processed" / "ena_joined.json": ENA_JOINED,
    }
    saved = {p: (p.read_text(encoding="utf-8") if p.exists() else None) for p in fixtures}

    try:
        for path, payload in fixtures.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")

        script = f'''
          env <- new.env()
          source("{(ROOT / "R" / "plot_norwegian_data.R").as_posix()}", local = env)
          df <- env$load_all_data()
          brk <- setNames(df$broker, df$accession)
          # A broker anywhere among an accession's rows wins …
          stopifnot(identical(unname(brk["SAMEA1"]), "ELIXIR Norway"))
          stopifnot(identical(unname(brk["ERP0001"]), "ELIXIR Norway"))
          # … and the center is still the fallback when there is no broker.
          stopifnot(identical(unname(brk["SAMEA2"]), "University of Oslo"))
          stopifnot(identical(unname(brk["ERP0002"]), "University of Oslo"))
          # Helper columns must not leak into the output schema.
          stopifnot(!any(c("ena_broker", "ena_center") %in% names(df)))
          cat("broker precedence ok\\n")
        '''

        result = subprocess.run(
            ["Rscript", "-e", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert "broker precedence ok" in result.stdout
    finally:
        for path, content in saved.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(content, encoding="utf-8")

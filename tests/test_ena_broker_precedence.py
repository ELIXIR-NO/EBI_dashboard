import json
import subprocess

# SAMEA1 is fetched twice (overlapping partition windows) and only the second
# copy carries broker_name; ERP0001's broker lives on its samples while the
# study row itself only has a center_name.  Both are cases where a center used
# to win over a broker.  "ELIXIR-Norway" is spelled as ENA actually records it.
SRA_SAMPLES = {
    "entries": [
        {"id": "SAMEA1", "fields": {
            "center_name": ["University of Bergen"], "country": ["Norway"],
            "first_public_date": ["20240101"], "alias": ["copy without broker"]}},
        {"id": "SAMEA1", "fields": {
            "center_name": ["University of Bergen"], "broker_name": ["ELIXIR-Norway"],
            "country": ["Norway"], "first_public_date": ["20240101"],
            "alias": ["copy with broker"]}},
        {"id": "SAMEA2", "fields": {
            "center_name": ["University of Oslo"], "country": ["Norway"],
            "first_public_date": ["20240201"], "alias": ["no broker at all"]}},
    ]
}

# A study's broker is its own `study_broker` (ENA Portal), never one inherited
# from its samples.  ERP0003 is the case that drove the change: its samples
# carry "NCBI" — INSDC mirroring provenance, not a submission broker — which
# used to be promoted onto the study and shown as its broker.
ENA_JOINED = {
    "join_date": "2026-01-01",
    "study_count": 3,
    "entries": [
        {"accession": "ERP0001", "title": "study brokered by ELIXIR-Norway",
         "center_name": "University of Bergen", "study_broker": "ELIXIR-Norway",
         "first_public_date": "20240301",
         "sample_countries": ["Norway"], "sample_centers": ["University of Bergen"],
         "sample_brokers": [], "n_experiments": 2},
        {"accession": "ERP0002", "title": "study with no broker at all",
         "center_name": "University of Oslo", "study_broker": "",
         "first_public_date": "20240401",
         "sample_countries": ["Norway"], "sample_centers": ["University of Oslo"],
         "sample_brokers": [], "n_experiments": 1},
        {"accession": "ERP0003", "title": "study whose samples were mirrored from NCBI",
         "center_name": "Nord University", "study_broker": "",
         "first_public_date": "20240501",
         "sample_countries": ["Norway"], "sample_centers": ["Nord University"],
         "sample_brokers": ["NCBI"], "n_experiments": 3},
    ],
}


def test_center_never_overwrites_broker_for_ena_entries(isolated_repo):
    # Runs in the isolated_repo tree, not the repo.  Besides the output/*.png
    # and output/*.csv that sourcing the render script rewrites (see the
    # fixture's docstring), this test's ena_joined.json fixture would land on
    # top of the real committed data/processed/ena_joined.json — restored in a
    # finally block, but a killed or interrupted run would leave the repo's
    # data clobbered.
    fixtures = {
        isolated_repo / "data" / "raw" / "sra-sample" / "latest.json": SRA_SAMPLES,
        isolated_repo / "data" / "processed" / "ena_joined.json": ENA_JOINED,
    }
    for path, payload in fixtures.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    script = '''
      env <- new.env()
      source("R/plot_norwegian_data.R", local = env)
      df <- env$load_all_data()
      brk <- setNames(df$broker, df$accession)
      # A broker anywhere among an accession's rows wins …
      stopifnot(identical(unname(brk["SAMEA1"]), "ELIXIR-Norway"))
      stopifnot(identical(unname(brk["ERP0001"]), "ELIXIR-Norway"))
      # … and the center is still the fallback when there is no broker.
      stopifnot(identical(unname(brk["SAMEA2"]), "University of Oslo"))
      stopifnot(identical(unname(brk["ERP0002"]), "University of Oslo"))
      # A sample-level broker is never promoted onto the study: ERP0003 falls
      # back to its center rather than reporting the "NCBI" its samples carry.
      stopifnot(identical(unname(brk["ERP0003"]), "Nord University"))
      # Helper columns must not leak into the output schema.
      stopifnot(!any(c("ena_broker", "ena_center") %in% names(df)))
      cat("broker precedence ok\\n")
    '''

    result = subprocess.run(
        ["Rscript", "-e", script],
        cwd=str(isolated_repo),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "broker precedence ok" in result.stdout
    # Guard against the isolation silently regressing: the render must have
    # written into the temporary tree, not into the repo.
    assert (isolated_repo / "output").is_dir(), result.stderr or result.stdout

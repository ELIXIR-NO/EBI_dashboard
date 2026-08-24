import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_r_render_helpers_parse_dates_and_match_institutions(isolated_repo):
    r_code = r'''
        env <- new.env()
        source("R/plot_norwegian_data.R", local = env)
        stopifnot(identical(env$parse_ebi_date("2023-01-15"), as.Date("2023-01-15")))
        stopifnot(identical(env$parse_ebi_date("20230115"), as.Date("2023-01-15")))
        stopifnot(identical(env$normalise_institution(c("University of Oslo"), email_vec = c("abc@uio.no")), "University of Oslo"))
        stopifnot(identical(env$normalise_institution(c("UiO")), "University of Oslo"))
        stopifnot(identical(env$pick_affiliation(c("University of Bergen"), email_vec = c("person@uib.no")), "University of Bergen"))
        cat("R helpers ok\n")
    '''

    result = subprocess.run(
        ["Rscript", "-e", r_code],
        # source() runs the whole script, output-writing block included, so
        # this must not run in the repo root.  See the isolated_repo fixture.
        cwd=str(isolated_repo),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "R helpers ok" in result.stdout

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


def test_normalise_center_label_canonicalises_without_inventing_matches(isolated_repo):
    # The Broker column falls back to ENA's center_name, which spells one
    # institution several ways.  normalise_center_label() collapses those, but
    # must not reach for a fuzzy match: the centers reaching that column are
    # routinely foreign, and Jaro-Winkler happily reads "University of Oulu" as
    # "University of Oslo" — which would also flip norwegian_submitter.
    r_code = r'''
        env <- new.env()
        source("R/plot_norwegian_data.R", local = env)
        nc <- env$normalise_center_label

        # Case, punctuation and semicolon-packed department chains collapse to
        # one canonical name.
        stopifnot(identical(nc("NORWEGIAN INSTITUTE OF PUBLIC HEALTH"),
                            "Norwegian Institute of Public Health"))
        stopifnot(identical(nc("Norwegian Institute of Public Health;NIPH"),
                            "Norwegian Institute of Public Health"))
        stopifnot(identical(nc("University of Oslo;CEES"), "University of Oslo"))
        # Norwegian-language names resolve to the English canonical.
        stopifnot(identical(nc("Norsk Institutt for Vannforskning;NIVA-211"),
                            "Norwegian Institute for Water Research"))

        # Foreign centers keep their raw name: never "Other Norway", and never
        # a same-looking Norwegian institution.
        for (foreign in c("University of Oulu", "DOE Joint Genome Institute",
                          "Wellcome Sanger Institute",
                          "University Hospital Muenster;IFH_MS")) {
          stopifnot(identical(nc(foreign), foreign))
        }

        # Vectorised, and NA / "" pass through untouched.
        out <- nc(c("NORWEGIAN INSTITUTE OF PUBLIC HEALTH", NA, "",
                    "University of Oulu"))
        stopifnot(identical(out, c("Norwegian Institute of Public Health", NA, "",
                                   "University of Oulu")))
        cat("center label ok\n")
    '''

    result = subprocess.run(
        ["Rscript", "-e", r_code],
        cwd=str(isolated_repo),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "center label ok" in result.stdout

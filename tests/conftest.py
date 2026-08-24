"""
Shared pytest fixtures.

The R render script is executed by more than one test, and sourcing it runs
the whole file top to bottom — including the ggsave/write_csv block at the
end.  Every such test therefore has to run in an isolated tree; see the
isolated_repo fixture for why that matters beyond mere tidiness.
"""

import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent

# Small config files the render script reads from data/; institution_map.json
# is read unconditionally, the other two are optional but kept for realism.
CONFIG_JSON = (
    "institution_map.json",
    "domains.json",
    "identifiers_namespaces.json",
)


@pytest.fixture
def isolated_repo(tmp_path: Path) -> Path:
    """
    A throwaway repo-shaped tree for R/plot_norwegian_data.R to run in.

    The script resolves every path from here::here(), which anchors on the
    .here marker, so OUT_DIR lands inside tmp_path.  Running it with
    cwd=ROOT instead would overwrite the repo's real output/*.csv and
    output/*.png with whatever fixture data happens to be on disk — and
    those files are committed artefacts.  The pipeline breaks badly when
    they are wrong: launch.yml hard-resets the server tree to main,
    Snakemake then sees render's outputs already present, prunes the whole
    fetch -> join -> render chain, and writes the run-complete sentinel
    over stale data.
    """
    (tmp_path / ".here").touch()
    shutil.copytree(ROOT / "R", tmp_path / "R")
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "processed").mkdir(parents=True)
    for cfg in CONFIG_JSON:
        src = ROOT / "data" / cfg
        if src.exists():
            shutil.copy(src, tmp_path / "data" / cfg)
    return tmp_path

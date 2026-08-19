import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / relative_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_render_script_handles_empty_data():
    result = subprocess.run(
        ["Rscript", "-e", 'source("R/plot_norwegian_data.R")'],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_email_domain_is_norwegian_uses_domain_not_local_part():
    norwegian_filter = _load_module("norwegian_filter", "scripts/norwegian_filter.py")

    web_domains = {"uio.no", "helse-bergen.no"}

    assert norwegian_filter.email_domain_is_norwegian("someone@uio.no", web_domains) is True
    assert norwegian_filter.email_domain_is_norwegian("someone@research.uio.no", web_domains) is True
    assert norwegian_filter.email_domain_is_norwegian("nina.gasparoni@uni-saarland.de", web_domains) is False
    assert norwegian_filter.email_domain_is_norwegian("nina@helse-bergen.no", web_domains) is True

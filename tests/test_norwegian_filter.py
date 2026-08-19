import importlib.util
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


norwegian_filter = _load_module("norwegian_filter", "scripts/norwegian_filter.py")


def test_false_positive_species_name_does_not_count_as_norwegian():
    text = "Norway spruce sample from Finland"
    filtered = norwegian_filter.strip_false_positives(text)
    assert "Norway spruce" not in filtered
    assert filtered.strip() == "sample from Finland"


def test_email_domain_is_domain_based_not_local_part():
    web_domains = {"uio.no", "helse-bergen.no"}
    assert norwegian_filter.email_domain_is_norwegian("someone@uio.no", web_domains) is True
    assert norwegian_filter.email_domain_is_norwegian("someone@research.uio.no", web_domains) is True
    assert norwegian_filter.email_domain_is_norwegian("nina.gasparoni@uni-saarland.de", web_domains) is False
    assert norwegian_filter.email_domain_is_norwegian("nina@helse-bergen.no", web_domains) is True


def test_is_norwegian_entry_ignores_species_like_false_positives_in_description():
    entry = {
        "fields": {
            "description": ["Norway spruce specimen from a Nordic station"],
            "center_name": ["University of Oslo"],
        }
    }
    safe_filter, abbrev_filter = norwegian_filter.get_cached_filter_tiers()
    assert norwegian_filter.is_norwegian_entry(entry, safe_filter, abbrev_filter) is True


def test_is_norwegian_entry_rejects_non_norwegian_text_without_signal():
    entry = {
        "fields": {
            "description": ["Norway spruce specimen from Finland"],
            "title": ["A broad ecological study"],
        }
    }
    safe_filter, abbrev_filter = norwegian_filter.get_cached_filter_tiers()
    assert norwegian_filter.is_norwegian_entry(entry, safe_filter, abbrev_filter) is False

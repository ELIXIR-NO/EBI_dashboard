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


def test_load_geo_tokens_and_web_domains_are_usable():
    geo_tokens = norwegian_filter.load_geo_tokens()
    web_domains = norwegian_filter.load_web_domains()
    assert isinstance(geo_tokens, list)
    assert isinstance(web_domains, set)
    assert any("Oslo" in token for token in geo_tokens)
    assert "uio.no" in web_domains


def test_build_geo_regex_matches_norwegian_city_names():
    regex = norwegian_filter.build_geo_regex(["Oslo", "Bergen", "Tromsø"])
    assert regex.search("Oslo University Hospital") is not None
    assert regex.search("Tromsø research center") is not None
    assert regex.search("Copenhagen") is None

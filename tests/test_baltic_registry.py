"""The Nasdaq Baltic instrument registry: packaged entries, lookup, validation."""

import os

import pytest

from tradingagents.dataflows.baltic import (
    RegistryError,
    is_baltic,
    load_registry,
    lookup,
)
from tradingagents.dataflows.config import run_config

pytestmark = pytest.mark.unit


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "instruments.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestPackagedRegistry:
    def test_lists_apranga_and_artea(self):
        registry = load_registry()
        assert {"APG1L.VS", "ROE1L.VS"} <= set(registry)

    def test_artea_is_a_bank_searched_under_its_former_name(self):
        artea = lookup("ROE1L.VS")
        assert artea.issuer == "Artea Bankas"
        assert artea.sector == "bank"
        assert artea.market == "Vilnius"
        assert "Šiaulių bankas" in artea.search_names

    def test_entries_carry_a_lei(self):
        assert lookup("APG1L.VS").lei == "529900XX7EU9F4GJTY86"


class TestLookup:
    def test_bare_code_and_lower_case_find_the_entry(self):
        assert lookup("APG1L").ticker == "APG1L.VS"
        assert lookup("apg1l.vs").ticker == "APG1L.VS"

    def test_unknown_or_empty_ticker_is_none(self):
        assert lookup("AAPL") is None
        assert lookup("IGN1L.VS") is None  # Baltic, but not in the registry
        assert lookup("") is None
        assert lookup(None) is None

    def test_is_baltic_reads_the_exchange_not_the_registry(self):
        assert is_baltic("IGN1L.VS")
        assert is_baltic("LHV1T")
        assert not is_baltic("AAPL")
        assert not is_baltic("SAP.DE")

    def test_search_names_drop_case_insensitive_repeats(self, tmp_path):
        path = _write(tmp_path, """
[[instrument]]
ticker = "TST1L.VS"
name = "Test"
issuer = "TEST"
aliases = ["test", "Testas"]
""")
        assert lookup("TST1L", path).search_names == ("Test", "Testas")


class TestCustomRegistry:
    def test_configured_path_replaces_the_packaged_registry(self, tmp_path):
        path = _write(tmp_path, """
[[instrument]]
ticker = "LHV1T.TL"
name = "LHV Group"
issuer = "LHV Group"
""")
        with run_config({"baltic_registry_path": path}):
            assert set(load_registry()) == {"LHV1T.TL"}
            assert lookup("LHV1T").market == "Tallinn"
            assert lookup("APG1L") is None

    def test_an_edited_file_is_read_again(self, tmp_path):
        path = _write(tmp_path, '[[instrument]]\nticker="AAA1L.VS"\nname="A"\nissuer="A"\n')
        assert set(load_registry(path)) == {"AAA1L.VS"}
        _write(tmp_path, '[[instrument]]\nticker="BBB1L.VS"\nname="B"\nissuer="B"\n')
        os.utime(path, ns=(1, 1))  # a mtime surely unlike the first write's
        assert set(load_registry(path)) == {"BBB1L.VS"}

    def test_defaults_for_optional_keys(self, tmp_path):
        path = _write(tmp_path, '[[instrument]]\nticker="AAA1R"\nname="A"\nissuer="A"\n')
        entry = lookup("AAA1R.RG", path)
        assert (entry.ticker, entry.sector, entry.lei, entry.aliases) == ("AAA1R.RG", "general", None, ())


class TestValidation:
    @pytest.mark.parametrize(("body", "message"), [
        ('[[instrument]]\nticker="AAPL"\nname="A"\nissuer="A"\n', "Baltic suffix"),
        ('[[instrument]]\nticker="AAA1L.VS"\nname="A"\n', "missing or empty"),
        ('[[instrument]]\nticker="AAA1L.VS"\nname="A"\nissuer=""\n', "missing or empty"),
        ('[[instrument]]\nticker="AAA1L.VS"\nname="A"\nissuer="A"\nisin="X"\n', "unknown key"),
        ('[[instrument]]\nticker="AAA1L.VS"\nname="A"\nissuer="A"\nsector="retail"\n', "sector"),
        ('[[instrument]]\nticker="AAA1L.VS"\nname="A"\nissuer="A"\nlei="123"\n', "LEI"),
        ('[[instrument]]\nticker="AAA1L.VS"\nname="A"\nissuer="A"\naliases="A"\n', "aliases"),
        ('[[instrument]]\nticker="AAA1L"\nname="A"\nissuer="A"\n'
         '[[instrument]]\nticker="AAA1L.VS"\nname="B"\nissuer="B"\n', "listed twice"),
        ('instrument = "x"\n', "array of tables"),
        ("[[instrument]\n", "cannot read"),
    ])
    def test_invalid_registry_names_the_problem(self, tmp_path, body, message):
        path = _write(tmp_path, body)
        with pytest.raises(RegistryError, match=message):
            load_registry(path)

    def test_missing_file_is_a_registry_error(self, tmp_path):
        with pytest.raises(RegistryError, match="cannot read"):
            load_registry(str(tmp_path / "nope.toml"))

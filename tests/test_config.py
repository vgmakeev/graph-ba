"""Tests for config.py: loading, normalization, classification."""
import pytest
from pathlib import Path

from graph_ba.config import load_config, normalize_id, classify_id


class TestLoadConfig:
    def test_loads_successfully(self, project_config):
        assert project_config is not None

    def test_scan_dirs(self, project_config):
        assert project_config.scan_dirs == ["docs"]

    def test_all_types_loaded(self, project_config):
        assert set(project_config.types.keys()) == {"ST", "FEAT", "REQ", "BP", "BR"}

    def test_type_order_preserved(self, project_config):
        assert project_config.type_order == ["ST", "FEAT", "REQ", "BP", "BR"]

    def test_type_labels(self, project_config):
        assert project_config.types["FEAT"].label == "Features"
        assert project_config.types["REQ"].label == "Requirements"

    def test_type_ref_pattern_compiles(self, project_config):
        pat = project_config.types["REQ"].ref_pattern
        assert pat.search("see REQ-01 here")
        assert not pat.search("XREQ-01")

    def test_type_classify_pattern(self, project_config):
        pat = project_config.types["BR"].classify_pattern
        assert pat.fullmatch("BR.1")
        assert not pat.fullmatch("BR-1")

    def test_definitions_count(self, project_config):
        assert len(project_config.definitions) == 5

    def test_definition_modes(self, project_config):
        modes = {d.type_id: d.mode for d in project_config.definitions}
        assert modes["REQ"] == "table"
        assert modes["ST"] == "heading"

    def test_definition_glob(self, project_config):
        br_def = [d for d in project_config.definitions if d.type_id == "BR"][0]
        assert "*" in br_def.file

    def test_index_tables(self, project_config):
        assert len(project_config.index_tables) == 1
        assert "index.md" in project_config.index_tables[0].file

    def test_coverage_pairs(self, project_config):
        assert len(project_config.coverage_pairs) == 2
        labels = [cp.label for cp in project_config.coverage_pairs]
        assert any("FEAT" in l and "REQ" in l for l in labels)

    def test_clusters(self, project_config):
        assert "Order Management" in project_config.clusters
        assert "F-01" in project_config.clusters["Order Management"]

    def test_required_sections(self, project_config):
        assert project_config.required_sections["FEAT"] == ["Goal", "Scope"]

    def test_expected_bidir(self, project_config):
        assert project_config.expected_bidir["FEAT"] == ["REQ"]

    def test_expected_cross_layer(self, project_config):
        pairs = project_config.expected_cross_layer["FEAT"]
        assert ("REQ", "requirements") in pairs

    def test_missing_config_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path)


class TestNormalizeId:
    def test_char_map_underscore(self, project_config):
        assert normalize_id("REQ_01", project_config) == "REQ-01"

    def test_zero_pad(self, project_config):
        assert normalize_id("REQ-1", project_config) == "REQ-01"

    def test_combined(self, project_config):
        assert normalize_id("REQ_1", project_config) == "REQ-01"

    def test_already_normalized(self, project_config):
        assert normalize_id("REQ-01", project_config) == "REQ-01"

    def test_no_match(self, project_config):
        assert normalize_id("F-01", project_config) == "F-01"

    def test_large_number_no_pad(self, project_config):
        assert normalize_id("REQ-99", project_config) == "REQ-99"


class TestClassifyId:
    @pytest.mark.parametrize("raw,expected", [
        ("ST-01", "ST"),
        ("F-02", "FEAT"),
        ("REQ-01", "REQ"),
        ("BP-03", "BP"),
        ("BR.1", "BR"),
    ])
    def test_known_types(self, project_config, raw, expected):
        assert classify_id(raw, project_config) == expected

    def test_unknown(self, project_config):
        assert classify_id("UNKNOWN-99", project_config) is None

    def test_normalizes_before_classify(self, project_config):
        # REQ_1 → REQ-01 via normalize, then classified as REQ
        assert classify_id("REQ_1", project_config) == "REQ"

"""Tests for traceability.py: definition scanning, reference extraction."""
import pytest

from graph_ba.traceability import expand_ranges


class TestScanDefinitions:
    def test_total_count(self, scan_result):
        registry, _, _ = scan_result
        assert len(registry) == 11

    def test_heading_definitions(self, scan_result):
        registry, _, _ = scan_result
        for aid in ("ST-01", "ST-02", "F-01", "F-02", "BP-01", "BP-02"):
            assert aid in registry, f"{aid} not found"

    def test_table_definitions(self, scan_result):
        registry, _, _ = scan_result
        for aid in ("REQ-01", "REQ-02", "REQ-03"):
            assert aid in registry, f"{aid} not found"

    def test_glob_definitions(self, scan_result):
        registry, _, _ = scan_result
        assert "BR.1" in registry
        assert "BR.2" in registry

    def test_artifact_types(self, scan_result):
        registry, _, _ = scan_result
        assert registry["ST-01"].artifact_type == "ST"
        assert registry["F-01"].artifact_type == "FEAT"
        assert registry["REQ-01"].artifact_type == "REQ"
        assert registry["BP-01"].artifact_type == "BP"
        assert registry["BR.1"].artifact_type == "BR"

    def test_titles(self, scan_result):
        registry, _, _ = scan_result
        assert "Administrator" in registry["ST-01"].title
        assert "Order Management" in registry["F-01"].title
        assert "Must manage orders" in registry["REQ-01"].title

    def test_line_numbers(self, scan_result):
        registry, _, _ = scan_result
        assert registry["ST-01"].line_number == 3
        assert registry["F-01"].line_number == 3
        assert registry["REQ-01"].line_number == 5

    def test_source_files(self, scan_result):
        registry, _, _ = scan_result
        assert registry["ST-01"].source_file.name == "stakeholders.md"
        assert registry["REQ-01"].source_file.name == "requirements.md"
        assert registry["BR.1"].source_file.name == "BR-pricing.md"


class TestScanReferences:
    def test_finds_inline_refs(self, scan_result):
        _, refs, _ = scan_result
        targets = {r.target_id for r in refs}
        assert "REQ-01" in targets
        assert "REQ-02" in targets

    def test_code_fence_excluded(self, scan_result):
        _, refs, _ = scan_result
        targets = {r.target_id for r in refs}
        assert "REQ-50" not in targets

    def test_dangling_ref_found(self, scan_result):
        _, refs, _ = scan_result
        targets = {r.target_id for r in refs}
        assert "REQ-99" in targets

    def test_section_context_captured(self, scan_result):
        _, refs, _ = scan_result
        # Refs under "## F-01 — Order Management" heading should have context
        f01_refs = [r for r in refs
                    if r.source_file.name == "features.md"
                    and r.target_id == "REQ-01"]
        assert any(r.context for r in f01_refs)


class TestScanIndexCrossRefs:
    def test_count(self, scan_result):
        _, _, xrefs = scan_result
        assert len(xrefs) >= 4  # F-01→REQ-01, F-01→REQ-02, F-02→BR.1, F-02→BP-01

    def test_expected_pairs(self, scan_result):
        _, _, xrefs = scan_result
        pairs = {(src, tgt) for src, tgt, _, _ in xrefs}
        assert ("F-01", "REQ-01") in pairs
        assert ("F-01", "REQ-02") in pairs
        assert ("F-02", "BR.1") in pairs
        assert ("F-02", "BP-01") in pairs


class TestExpandRanges:
    def test_basic_range(self, project_config):
        result = expand_ranges("see BR.1.1\u2013BR.1.3 here", project_config)
        assert result == ["BR.1.1", "BR.1.2", "BR.1.3"]

    def test_hyphen_range(self, project_config):
        result = expand_ranges("BR.2.1-BR.2.2", project_config)
        assert result == ["BR.2.1", "BR.2.2"]

    def test_no_range(self, project_config):
        result = expand_ranges("just text", project_config)
        assert result == []

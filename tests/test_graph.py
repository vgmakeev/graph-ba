"""Tests for traceability.py: graph construction and verification."""
import pytest

from graph_ba.traceability import verify, _find_owner


class TestBuildGraph:
    def test_all_defined_nodes_present(self, built_graph):
        G, registry = built_graph
        for aid in registry:
            assert aid in G, f"Defined artifact {aid} missing from graph"

    def test_defined_flag_set(self, built_graph):
        G, registry = built_graph
        for aid in registry:
            assert G.nodes[aid].get("defined") is True

    def test_dangling_node_marked(self, built_graph):
        G, _ = built_graph
        # REQ-99 is referenced but not defined — unless resolved by variants
        if "REQ-99" in G:
            assert G.nodes["REQ-99"].get("defined") is False

    def test_file_node_created(self, built_graph):
        G, _ = built_graph
        file_nodes = [n for n in G.nodes() if n.startswith("FILE:")]
        assert len(file_nodes) >= 1  # FILE:index.md at minimum

    def test_node_attributes(self, built_graph):
        G, _ = built_graph
        data = G.nodes["F-01"]
        assert data["type"] == "FEAT"
        assert "Order Management" in data.get("title", "")
        assert data.get("source_file")

    def test_expected_edges(self, built_graph):
        G, _ = built_graph
        assert G.has_edge("F-01", "REQ-01")
        assert G.has_edge("F-01", "REQ-02")
        assert G.has_edge("F-01", "BP-01")
        assert G.has_edge("REQ-01", "F-01")
        assert G.has_edge("REQ-02", "F-01")
        assert G.has_edge("BP-01", "REQ-01")
        assert G.has_edge("BP-02", "REQ-02")
        assert G.has_edge("BR.1", "F-01")
        assert G.has_edge("BR.2", "F-02")
        assert G.has_edge("F-02", "BR.1")
        assert G.has_edge("F-02", "BP-01")

    def test_no_self_loops(self, built_graph):
        G, _ = built_graph
        for u, v in G.edges():
            assert u != v, f"Self-loop found: {u}"

    def test_edge_attributes(self, built_graph):
        G, _ = built_graph
        data = G.edges["F-01", "REQ-01"]
        assert "source_file" in data


class TestFindOwner:
    def test_single_artifact(self):
        assert _find_owner([(10, "A-01")], 15) == "A-01"

    def test_multiple_closest(self):
        arts = [(5, "A-01"), (20, "A-02"), (40, "A-03")]
        assert _find_owner(arts, 25) == "A-02"

    def test_before_first(self):
        arts = [(10, "A-01"), (20, "A-02")]
        assert _find_owner(arts, 5) is None

    def test_empty(self):
        assert _find_owner([], 10) is None


class TestVerify:
    def test_orphans(self, scan_result, project_config, built_graph):
        G, registry = built_graph
        _, references, _, _ = scan_result
        report = verify(G, registry, references, project_config)
        # ST-01, ST-02, REQ-03, BP-02, BR.2 have no incoming edges
        assert "ST-01" in report.orphans
        assert "ST-02" in report.orphans
        assert "REQ-03" in report.orphans

    def test_undefined_nodes_in_graph(self, built_graph):
        G, registry = built_graph
        # REQ-99 is referenced but not in registry — added to graph with defined=False
        undefined = [n for n in G.nodes()
                     if not G.nodes[n].get("defined", False)
                     and not n.startswith("FILE:")]
        assert "REQ-99" in undefined

    def test_coverage_feat_req(self, scan_result, project_config, built_graph):
        G, registry = built_graph
        _, references, _, _ = scan_result
        report = verify(G, registry, references, project_config)
        cov = report.coverage.get("FEAT \u2192 REQ", {})
        assert cov.get("total") == 2  # F-01, F-02
        assert cov.get("linked") == 1  # F-01 has REQ links, F-02 doesn't
        assert "F-02" in cov.get("missing", [])

    def test_coverage_req_bp(self, scan_result, project_config, built_graph):
        G, registry = built_graph
        _, references, _, _ = scan_result
        report = verify(G, registry, references, project_config)
        cov = report.coverage.get("REQ \u2192 BP", {})
        assert cov.get("total") == 3  # REQ-01, REQ-02, REQ-03
        # REQ-01 → F-01 (not BP), REQ-02 → F-01 (not BP), REQ-03 → nothing
        assert cov.get("linked") == 0

    def test_missing_expected_links(self, scan_result, project_config, built_graph):
        G, registry = built_graph
        _, references, _, _ = scan_result
        report = verify(G, registry, references, project_config)
        missing_ids = {aid for aid, _ in report.missing_expected}
        # F-02 has no REQ link (expected_cross_layer: FEAT needs REQ)
        assert "F-02" in missing_ids

    def test_registry_count(self, scan_result, project_config, built_graph):
        G, registry = built_graph
        _, references, _, _ = scan_result
        report = verify(G, registry, references, project_config)
        assert report.registry_count.get("FEAT") == 2
        assert report.registry_count.get("REQ") == 3
        assert report.registry_count.get("ST") == 2

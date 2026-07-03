"""Tests for [tests].files glob patterns (colocated tests)."""


def test_tests_files_config_loaded(project_config):
    assert project_config.tests.files == ["src/**/*.test.ts"]


def test_colocated_test_scanned_via_glob(test_refs):
    by_path = {r.rel_path for r in test_refs}
    assert "src/mappers.test.ts" in by_path
    glob_targets = {
        t for r in test_refs if r.rel_path == "src/mappers.test.ts"
        for t in r.target_ids
    }
    assert glob_targets == {"REQ-01"}


def test_non_test_src_files_not_scanned(test_refs):
    """order.ts mentions REQ-01 in @trace but is NOT a test — must not appear."""
    by_path = {r.rel_path for r in test_refs}
    assert "src/order.ts" not in by_path


def test_colocated_test_node_in_graph(built_graph):
    G, _ = built_graph
    node = "TEST:src/mappers.test.ts"
    assert node in G
    assert G.nodes[node]["type"] == "TEST"
    assert G.has_edge(node, "REQ-01")

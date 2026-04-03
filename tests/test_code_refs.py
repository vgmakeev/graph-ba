"""Tests for code-to-artifact traceability (@trace comments)."""
import json

import pytest

from graph_ba.graph_db import cli


class TestScanCodeReferences:
    def test_finds_ts_refs(self, scan_result):
        _, _, _, code_refs = scan_result
        ts_refs = [cr for cr in code_refs if cr.code_file.name == "order.ts"]
        assert len(ts_refs) == 2  # two @trace lines

    def test_finds_py_refs(self, scan_result):
        _, _, _, code_refs = scan_result
        py_refs = [cr for cr in code_refs if cr.code_file.name == "delivery.py"]
        assert len(py_refs) == 2

    def test_multiple_ids_per_line(self, scan_result):
        _, _, _, code_refs = scan_result
        multi = [cr for cr in code_refs if len(cr.target_ids) > 1]
        assert len(multi) >= 2  # order.ts line 2, delivery.py line 2

    def test_no_trace_file_ignored(self, scan_result):
        _, _, _, code_refs = scan_result
        go_refs = [cr for cr in code_refs if cr.code_file.suffix == ".go"]
        assert len(go_refs) == 0

    def test_ids_normalized(self, scan_result, project_config):
        _, _, _, code_refs = scan_result
        from graph_ba.config import classify_id
        all_ids = [tid for cr in code_refs for tid in cr.target_ids]
        assert all(classify_id(tid, project_config) is not None for tid in all_ids)

    def test_context_captured(self, scan_result):
        _, _, _, code_refs = scan_result
        assert all(cr.context for cr in code_refs)

    def test_rel_path_set(self, scan_result):
        _, _, _, code_refs = scan_result
        for cr in code_refs:
            assert cr.rel_path.startswith("src/")


class TestCodeNodesInGraph:
    def test_code_nodes_created(self, built_graph):
        G, _ = built_graph
        code_nodes = [n for n in G.nodes() if n.startswith("CODE:")]
        assert len(code_nodes) == 2  # order.ts and delivery.py

    def test_code_node_type(self, built_graph):
        G, _ = built_graph
        for n in G.nodes():
            if n.startswith("CODE:"):
                assert G.nodes[n]["type"] == "CODE"
                assert G.nodes[n]["defined"] is True

    def test_code_edges_exist(self, built_graph):
        G, _ = built_graph
        code_nodes = [n for n in G.nodes() if n.startswith("CODE:")]
        for cn in code_nodes:
            assert G.out_degree(cn) > 0

    def test_specific_edges(self, built_graph):
        G, _ = built_graph
        # order.ts references F-01, REQ-01, BR.1
        order_node = [n for n in G.nodes() if "order.ts" in n]
        assert len(order_node) == 1
        targets = set(G.successors(order_node[0]))
        assert "F-01" in targets
        assert "REQ-01" in targets
        assert "BR.1" in targets

    def test_delivery_edges(self, built_graph):
        G, _ = built_graph
        # delivery.py references F-02, BP-01, BR.2
        delivery_node = [n for n in G.nodes() if "delivery.py" in n]
        assert len(delivery_node) == 1
        targets = set(G.successors(delivery_node[0]))
        assert "F-02" in targets
        assert "BP-01" in targets
        assert "BR.2" in targets


class TestCodeRefsInDB:
    def test_code_artifacts_in_db(self, db_conn):
        rows = db_conn.execute(
            "SELECT * FROM artifacts WHERE type = 'CODE'"
        ).fetchall()
        assert len(rows) == 2

    def test_code_edges_in_db(self, db_conn):
        rows = db_conn.execute(
            "SELECT * FROM edges WHERE source_id LIKE 'CODE:%'"
        ).fetchall()
        # order.ts: F-01, REQ-01, BR.1 = 3; delivery.py: F-02, BP-01, BR.2 = 3
        assert len(rows) >= 6


class TestCodeRefsCLI:
    def test_code_refs_default(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "code-refs"
        ])
        assert result.exit_code == 0, result.output
        assert "order.ts" in result.output
        assert "F-01" in result.output

    def test_code_refs_by_artifact(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "code-refs", "--by-artifact"
        ])
        assert result.exit_code == 0, result.output
        assert "F-01" in result.output

    def test_code_refs_json(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "code-refs"
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "code_refs" in data
        assert len(data["code_refs"]) >= 6

    def test_code_refs_type_filter(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "code-refs", "--type", "FEAT"
        ])
        assert result.exit_code == 0, result.output
        assert "F-01" in result.output or "F-02" in result.output
        # Should not contain BR refs
        assert "BR.1" not in result.output


class TestCodeCoverage:
    def test_coverage_includes_code(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "coverage"
        ])
        assert result.exit_code == 0, result.output
        assert "CODE" in result.output

    def test_coverage_json_includes_code(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "coverage"
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "code_coverage" in data
        assert len(data["code_coverage"]) == 2  # FEAT and REQ

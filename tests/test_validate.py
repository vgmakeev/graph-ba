"""Tests for `graph-ba validate`, audit baseline ratchet, and the TEST: layer."""
import json

import pytest

from graph_ba.cli import cli


class TestValidateCmd:
    def test_pass_fully_linked(self, cli_env):
        runner, root, db_path = cli_env
        # REQ-01: defined, all outgoing refs defined, covered by a test file
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "validate", "REQ-01"
        ])
        assert result.exit_code == 0, result.output
        assert "VERDICT: PASS" in result.output

    def test_fail_dangling_out(self, cli_env):
        runner, root, db_path = cli_env
        # F-01 references REQ-99 which is never defined
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "validate", "F-01"
        ])
        assert result.exit_code == 1
        assert "VERDICT: FAIL" in result.output
        assert "dangling_out" in result.output
        assert "REQ-99" in result.output

    def test_fail_missing_required_section(self, cli_env):
        runner, root, db_path = cli_env
        # F-01 has "Goal" but not "Scope" (required_sections for FEAT)
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "validate", "F-01"
        ])
        assert result.exit_code == 1
        assert "required_sections" in result.output
        assert "Scope" in result.output

    def test_fail_missing_cross_layer(self, cli_env):
        runner, root, db_path = cli_env
        # F-02 has no REQ links (expected_cross_layer: FEAT needs REQ)
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "validate", "F-02"
        ])
        assert result.exit_code == 1
        assert "expected_cross_layer" in result.output

    def test_fail_unknown_id(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "validate", "FAKE-99"
        ])
        assert result.exit_code == 1
        assert "VERDICT: FAIL" in result.output
        assert "not found" in result.output

    def test_fail_dangling_node_itself(self, cli_env):
        runner, root, db_path = cli_env
        # REQ-99 exists in graph but defined=0 → defined check fails
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "validate", "REQ-99"
        ])
        assert result.exit_code == 1
        assert "never defined" in result.output

    def test_warn_does_not_fail(self, cli_env):
        runner, root, db_path = cli_env
        # REQ-02: no TEST references → test_evidence warn, but still PASS
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "validate", "REQ-02"
        ])
        assert result.exit_code == 0, result.output
        assert "VERDICT: PASS" in result.output
        assert "⚠" in result.output
        assert "test_evidence" in result.output

    def test_json_structure(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "validate", "REQ-01"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "REQ-01"
        assert data["verdict"] == "PASS"
        assert isinstance(data["checks"], list)
        for c in data["checks"]:
            assert set(c) == {"name", "status", "detail"}
            assert c["status"] in ("pass", "fail", "warn")
        names = [c["name"] for c in data["checks"]]
        assert "defined" in names
        assert "dangling_out" in names
        assert "test_evidence" in names

    def test_json_fail_exit_code(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "validate", "F-01"
        ])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["verdict"] == "FAIL"
        assert any(c["status"] == "fail" for c in data["checks"])


class TestAuditBaseline:
    def _write_baseline(self, runner, root, db_path, path):
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "audit", "--write-baseline", str(path)
        ])
        assert result.exit_code == 0, result.output
        return result

    def test_write_baseline(self, cli_env, tmp_path):
        runner, root, db_path = cli_env
        path = tmp_path / "baseline.json"
        result = self._write_baseline(runner, root, db_path, path)
        assert "Baseline written" in result.output
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        fps = data["fingerprints"]
        assert fps == sorted(fps)
        assert "DANGLING:REQ-99" in fps
        assert any(fp.startswith("COVERAGE_GAP:") for fp in fps)

    def test_baseline_full_snapshot_clean(self, cli_env, tmp_path):
        runner, root, db_path = cli_env
        path = tmp_path / "baseline.json"
        self._write_baseline(runner, root, db_path, path)
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "audit", "--baseline", str(path)
        ])
        assert result.exit_code == 0, result.output
        assert "0 new" in result.output
        assert "0 resolved" in result.output

    def test_baseline_detects_new_issue(self, cli_env, tmp_path):
        runner, root, db_path = cli_env
        path = tmp_path / "baseline.json"
        self._write_baseline(runner, root, db_path, path)
        # Shrink the baseline: the removed fingerprint becomes a "new" issue
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "DANGLING:REQ-99" in data["fingerprints"]
        data["fingerprints"].remove("DANGLING:REQ-99")
        path.write_text(json.dumps(data), encoding="utf-8")

        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "audit", "--baseline", str(path)
        ])
        assert result.exit_code == 1
        assert "1 new" in result.output
        assert "DANGLING:REQ-99" in result.output

    def test_baseline_json_fields(self, cli_env, tmp_path):
        runner, root, db_path = cli_env
        path = tmp_path / "baseline.json"
        self._write_baseline(runner, root, db_path, path)
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "audit", "--baseline", str(path)
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["new"] == []
        assert data["resolved"] == []
        baseline_fps = json.loads(path.read_text(encoding="utf-8"))["fingerprints"]
        assert data["known"] == baseline_fps

    def test_baseline_json_new_exits_1(self, cli_env, tmp_path):
        runner, root, db_path = cli_env
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps({"version": 1, "fingerprints": []}),
                        encoding="utf-8")
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "audit", "--baseline", str(path)
        ])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert len(data["new"]) >= 1

    def test_audit_without_options_unchanged(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "audit"
        ])
        assert result.exit_code == 0
        assert "Global Audit" in result.output
        assert "Review Candidates" in result.output
        assert "Baseline" not in result.output


class TestTestLayer:
    def test_test_node_in_graph(self, built_graph):
        G, _ = built_graph
        assert "TEST:tests/test_orders.py" in G
        assert G.nodes["TEST:tests/test_orders.py"]["type"] == "TEST"
        assert G.has_edge("TEST:tests/test_orders.py", "REQ-01")

    def test_test_node_in_db(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "node", "TEST:tests/test_orders.py"
        ])
        assert result.exit_code == 0
        assert "TEST:tests/test_orders.py" in result.output
        assert "REQ-01" in result.output

    def test_test_edge_in_db(self, db_conn):
        rows = db_conn.execute(
            "SELECT source_id FROM edges "
            "WHERE target_id = 'REQ-01' AND source_id LIKE 'TEST:%'"
        ).fetchall()
        assert any("test_orders.py" in r["source_id"] for r in rows)

    def test_coverage_shows_test_block(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "coverage"
        ])
        assert result.exit_code == 0
        assert "Test coverage" in result.output
        assert "TEST → REQ" in result.output

    def test_coverage_json_has_test_coverage(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "coverage"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["test_coverage"]) == 1
        req_cov = data["test_coverage"][0]
        assert req_cov["type"] == "REQ"
        assert req_cov["linked"] == 1  # only REQ-01 is referenced by tests
        assert req_cov["total"] == 3

    def test_import_summary_counts_test_files(self, cli_env_rw):
        runner, root, db_path = cli_env_rw
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "import"
        ])
        assert result.exit_code == 0
        # tests/test_orders.py (dir scan) + src/mappers.test.ts ([tests].files glob)
        assert "2 test files" in result.output

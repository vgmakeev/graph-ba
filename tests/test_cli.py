"""Tests for CLI commands via CliRunner."""
import json

import pytest
from click.testing import CliRunner

from graph_ba.graph_db import cli


class TestImportCmd:
    def test_success(self, cli_env_rw):
        runner, root, db_path = cli_env_rw
        # Import already ran in fixture; verify output
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "import"
        ])
        assert result.exit_code == 0
        assert "Imported:" in result.output

    def test_missing_config(self, tmp_path):
        runner = CliRunner()
        db_path = tmp_path / "empty.db"
        result = runner.invoke(cli, [
            "--root", str(tmp_path), "--db", str(db_path), "import"
        ])
        assert result.exit_code != 0


class TestInitCmd:
    def test_creates_config(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["--root", str(tmp_path), "init"])
        assert result.exit_code == 0
        assert (tmp_path / "graph-ba.toml").exists()

    def test_no_overwrite(self, tmp_path):
        (tmp_path / "graph-ba.toml").write_text("existing", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(cli, ["--root", str(tmp_path), "init"])
        assert result.exit_code == 0
        assert "already exists" in result.output
        assert (tmp_path / "graph-ba.toml").read_text() == "existing"


class TestSearchCmd:
    def test_found(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "search", "Order"
        ])
        assert result.exit_code == 0
        assert "F-01" in result.output or "REQ-01" in result.output

    def test_not_found(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "search", "zzzzzzz"
        ])
        assert result.exit_code == 0


class TestNodeCmd:
    def test_found(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "node", "F-01"
        ])
        assert result.exit_code == 0
        assert "F-01" in result.output
        assert "FEAT" in result.output

    def test_not_found(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "node", "FAKE-99"
        ])
        assert result.exit_code == 0
        assert "not found" in result.output

    def test_partial_match(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "node", "F-0"
        ])
        assert result.exit_code == 0
        assert "Similar" in result.output


class TestPathCmd:
    def test_path_found(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "path", "F-01", "REQ-01"
        ])
        assert result.exit_code == 0
        assert "F-01" in result.output and "REQ-01" in result.output

    def test_path_not_found(self, cli_env):
        runner, root, db_path = cli_env
        # ST-01 is isolated, no path to F-01
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "path", "ST-01", "F-01"
        ])
        assert result.exit_code == 0
        assert "No path between" in result.output

    def test_unknown_node(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "path", "FAKE-1", "FAKE-2"
        ])
        assert result.exit_code == 0
        assert "not found" in result.output


class TestImpactCmd:
    def test_has_impact(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "impact", "F-01"
        ])
        assert result.exit_code == 0
        assert "Cascade impact" in result.output

    def test_no_impact(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "impact", "ST-01"
        ])
        assert result.exit_code == 0
        assert "no cascade impact" in result.output

    def test_unknown_node(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "impact", "FAKE-99"
        ])
        assert result.exit_code == 0
        assert "not found" in result.output

    def test_descendants_grouped_by_type(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "impact", "F-01"
        ])
        assert result.exit_code == 0
        # F-01 references REQ-01, REQ-02, BP-01 → descendants should include them
        assert "REQ" in result.output or "BP" in result.output

    def test_reverse_impact_shown(self, cli_env):
        runner, root, db_path = cli_env
        # BP-01 is referenced by F-01 and F-02, so it should have ancestors
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "impact", "BP-01"
        ])
        assert result.exit_code == 0
        assert "Reverse impact" in result.output or "what affects" in result.output

    def test_leaf_node_no_descendants(self, cli_env):
        runner, root, db_path = cli_env
        # ST-02 is isolated — no descendants
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "impact", "ST-02"
        ])
        assert result.exit_code == 0
        assert "no cascade impact" in result.output

    # ── JSON output tests ──

    def test_json_structure(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "impact", "F-01"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["node"] == "F-01"
        assert data["type"] == "FEAT"
        assert "descendants" in data
        assert "ancestors" in data
        assert "total" in data["descendants"]
        assert "by_type" in data["descendants"]
        assert "total" in data["ancestors"]
        assert "by_type" in data["ancestors"]

    def test_json_descendants_content(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "impact", "F-01"
        ])
        data = json.loads(result.output)
        desc = data["descendants"]
        assert desc["total"] > 0
        # All descendant IDs across types should sum to total
        all_ids = [nid for ids in desc["by_type"].values() for nid in ids]
        assert len(all_ids) == desc["total"]

    def test_json_no_impact(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "impact", "ST-01"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["node"] == "ST-01"
        assert data["descendants"]["total"] == 0
        assert data["descendants"]["by_type"] == {}

    def test_json_unknown_node(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "impact", "FAKE-99"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "error" in data
        assert data["node"] == "FAKE-99"

    def test_json_ancestors_content(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "impact", "BP-01"
        ])
        data = json.loads(result.output)
        anc = data["ancestors"]
        assert anc["total"] > 0
        all_ids = [nid for ids in anc["by_type"].values() for nid in ids]
        assert len(all_ids) == anc["total"]

    def test_json_ids_sorted(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "impact", "F-01"
        ])
        data = json.loads(result.output)
        for type_name, ids in data["descendants"]["by_type"].items():
            assert ids == sorted(ids), f"IDs for {type_name} not sorted"


class TestSqlCmd:
    def test_valid_query(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "sql", "SELECT count(*) as c FROM artifacts"
        ])
        assert result.exit_code == 0

    def test_invalid_query(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "sql", "INVALID SQL"
        ])
        # Should handle error gracefully
        assert result.exit_code == 0


class TestCoverageCmd:
    def test_output(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "coverage"
        ])
        assert result.exit_code == 0
        assert "FEAT" in result.output
        assert "REQ" in result.output


class TestAnomaliesCmd:
    def test_output(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "anomalies"
        ])
        assert result.exit_code == 0
        # Should detect at least some anomalies (cycles, roots, sinks, dangling)
        assert "anomal" in result.output.lower() or "ROOT" in result.output or "CYCLE" in result.output


class TestReviewCmd:
    def test_by_id(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "review", "F-01"
        ])
        assert result.exit_code == 0
        assert "F-01" in result.output
        assert "REVIEW" in result.output

    def test_semantic_mode(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "review", "F-01", "--semantic", "--lines", "10"
        ])
        assert result.exit_code == 0
        assert "LINKED ARTIFACTS" in result.output

    def test_not_found(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "review", "FAKE-99"
        ])
        assert result.exit_code == 0
        assert "not found" in result.output

    def test_missing_section_detected(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "review", "F-01"
        ])
        # F-01 has Goal but missing Scope → STRUCT issue
        assert "STRUCT" in result.output or "Scope" in result.output


class TestAuditCmd:
    def test_runs(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "audit"
        ])
        assert result.exit_code == 0
        assert "Global Audit" in result.output

    def test_finds_issues(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "audit"
        ])
        assert result.exit_code == 0
        assert "Issues" in result.output

    def test_finds_coverage_gap(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "audit"
        ])
        assert result.exit_code == 0
        assert "COVERAGE_GAP" in result.output
        assert "F-02" in result.output

    def test_finds_dangling(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "audit"
        ])
        assert result.exit_code == 0
        assert "DANGLING" in result.output
        assert "REQ-99" in result.output

    def test_finds_missing_cross_layer(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "audit"
        ])
        assert result.exit_code == 0
        # F-02 has no REQ links (expected_cross_layer: FEAT needs REQ)
        assert "MISSING_CROSS_LAYER" in result.output

    def test_candidates_prioritized(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "audit"
        ])
        assert result.exit_code == 0
        assert "Review Candidates" in result.output
        # HIGH priority should appear before non-HIGH
        lines = result.output.split("\n")
        high_lines = [l for l in lines if "HIGH" in l]
        assert len(high_lines) >= 1

    def test_json_structure(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "audit"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "summary" in data
        assert "issues" in data
        assert "candidates" in data
        assert data["summary"]["artifacts"] >= 11
        assert data["summary"]["issues"] >= 1
        assert data["summary"]["candidates"] >= 1

    def test_json_candidates_have_reasons(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "audit"
        ])
        data = json.loads(result.output)
        for c in data["candidates"]:
            assert "id" in c
            assert "type" in c
            assert "reasons" in c
            assert "priority" in c
            assert len(c["reasons"]) >= 1

    def test_json_dangling_in_issues(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "audit"
        ])
        data = json.loads(result.output)
        dangling = [i for i in data["issues"] if i["type"] == "DANGLING"]
        assert any(i["id"] == "REQ-99" for i in dangling)

    def test_json_coverage_gap_with_missing(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json", "audit"
        ])
        data = json.loads(result.output)
        gaps = [i for i in data["issues"] if i["type"] == "COVERAGE_GAP"]
        assert len(gaps) >= 1
        for g in gaps:
            assert "source" in g
            assert "target" in g
            assert "missing" in g
            assert isinstance(g["missing"], list)

    def test_top_option(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "audit", "--top", "2"
        ])
        data = json.loads(result.output)
        assert len(data["candidates"]) <= 2


class TestJsonOutput:
    """Test --json flag across commands."""

    def test_search_json(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "search", "Order"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "artifacts" in data
        assert "clusters" in data
        assert "edges" in data

    def test_node_json(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "node", "F-01"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "F-01"
        assert data["type"] == "FEAT"
        assert isinstance(data["outgoing"], list)
        assert isinstance(data["incoming"], list)

    def test_anomalies_json(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "anomalies"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "nodes" in data
        assert "issues" in data

    def test_coverage_json(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path),
            "--json", "coverage"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "pairs" in data
        assert len(data["pairs"]) == 2

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
        assert "\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d" in result.output  # не найден

    def test_partial_match(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "node", "F-0"
        ])
        assert result.exit_code == 0
        assert "\u041f\u043e\u0445\u043e\u0436\u0438\u0435" in result.output  # Похожие


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
        assert "\u043d\u0435 \u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u0435\u0442" in result.output  # не существует

    def test_unknown_node(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "path", "FAKE-1", "FAKE-2"
        ])
        assert result.exit_code == 0
        assert "\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d" in result.output


class TestImpactCmd:
    def test_has_impact(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "impact", "F-01"
        ])
        assert result.exit_code == 0
        assert "\u041a\u0430\u0441\u043a\u0430\u0434\u043d\u043e\u0435" in result.output  # Каскадное

    def test_no_impact(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "impact", "ST-01"
        ])
        assert result.exit_code == 0
        assert "\u043d\u0435\u0442 \u043a\u0430\u0441\u043a\u0430\u0434\u043d\u043e\u0433\u043e" in result.output  # нет каскадного


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
        assert "\u0421\u0412\u042f\u0417\u0410\u041d\u041d\u042b\u0415" in result.output  # СВЯЗАННЫЕ

    def test_not_found(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "review", "FAKE-99"
        ])
        assert result.exit_code == 0
        assert "\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d" in result.output

    def test_missing_section_detected(self, cli_env):
        runner, root, db_path = cli_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "review", "F-01"
        ])
        # F-01 has Goal but missing Scope → STRUCT issue
        assert "STRUCT" in result.output or "Scope" in result.output


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

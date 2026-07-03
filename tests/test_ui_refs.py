"""Tests for the UI: traceability layer (trace sidecar files)."""
import json

from click.testing import CliRunner

from graph_ba.config import load_config
from graph_ba.graph_db import cli


UI_NODE = "UI:ui/orders/trace.json"


def test_ui_config_loaded(project_config):
    assert project_config.ui is not None
    assert project_config.ui.files == ["ui/*/trace.json"]
    assert project_config.ui.coverage_types == ["REQ"]


def test_scan_ui_references_finds_ac_ids(ui_refs):
    all_targets = {t for ref in ui_refs for t in ref.target_ids}
    assert all_targets == {"REQ-01", "REQ-02"}
    assert all(ref.rel_path == "ui/orders/trace.json" for ref in ui_refs)


def test_scan_ui_references_without_config(ba_project, tmp_path):
    """Project without [ui] section yields no UI refs."""
    from graph_ba.traceability import scan_ui_references

    config = load_config(ba_project)
    config.ui = None
    assert scan_ui_references(ba_project, config) == []


def test_ui_nodes_in_graph(built_graph):
    G, _ = built_graph
    assert UI_NODE in G
    assert G.nodes[UI_NODE]["type"] == "UI"
    assert G.has_edge(UI_NODE, "REQ-01")
    assert G.has_edge(UI_NODE, "REQ-02")


def test_import_reports_ui_files(ba_project, tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--root", str(ba_project), "--db", str(tmp_path / "ui.db"), "import"
    ])
    assert result.exit_code == 0, result.output
    assert "1 ui trace files" in result.output


def test_coverage_includes_ui_layer(cli_env):
    runner, root, db_path = cli_env
    result = runner.invoke(cli, [
        "--root", str(root), "--db", str(db_path), "--json", "coverage"
    ])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    ui_cov = data["ui_coverage"]
    assert len(ui_cov) == 1
    req_cov = ui_cov[0]
    assert req_cov["type"] == "REQ"
    # REQ-01 and REQ-02 have UI evidence, REQ-03 does not
    assert req_cov["linked"] == 2
    assert req_cov["total"] == 3


def test_ui_node_visible_in_node_command(cli_env):
    runner, root, db_path = cli_env
    result = runner.invoke(cli, [
        "--root", str(root), "--db", str(db_path), "node", "REQ-01"
    ])
    assert result.exit_code == 0, result.output
    assert UI_NODE in result.output

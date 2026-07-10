"""Optional CodeGraph symbol enrichment for @trace references."""

import sqlite3

from click.testing import CliRunner

from graph_ba.cli import cli
from graph_ba.config import load_config
from graph_ba.db import do_import, get_db
from graph_ba.graph_builder import build_graph
from graph_ba.scanning import scan_code_references


CONFIG = r"""
[types.REQ]
label = "Requirement"
ref = '(?<![A-Za-z])(REQ-\d{2})(?!\d)'
classify = 'REQ-\d{2}'

[code]
dirs = ["src"]
extensions = ["py"]

[providers.codegraph]
"""


def _project(tmp_path, *, with_index: bool = True):
    (tmp_path / "graph-ba.toml").write_text(CONFIG, encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "service.py").write_text(
        "# @trace: REQ-01\n"
        "def process_order(order):\n"
        "    # @trace: REQ-02\n"
        "    return order\n",
        encoding="utf-8",
    )
    if with_index:
        db_path = tmp_path / ".codegraph" / "codegraph.db"
        db_path.parent.mkdir()
        with sqlite3.connect(db_path) as db:
            db.execute(
                "CREATE TABLE nodes ("
                "id TEXT PRIMARY KEY, kind TEXT, qualified_name TEXT, "
                "file_path TEXT, start_line INTEGER, end_line INTEGER)"
            )
            db.execute(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "function:process-order",
                    "function",
                    "process_order",
                    "src/service.py",
                    2,
                    4,
                ),
            )
            db.execute(
                "CREATE TABLE files (path TEXT PRIMARY KEY, modified_at INTEGER)"
            )
            db.execute(
                "INSERT INTO files VALUES (?, ?)",
                (
                    "src/service.py",
                    int((src / "service.py").stat().st_mtime * 1000),
                ),
            )
    return load_config(tmp_path)


def test_provider_is_opt_in(ba_project):
    assert load_config(ba_project).codegraph is None


def test_resolves_trace_before_and_inside_symbol(tmp_path):
    config = _project(tmp_path)

    refs = scan_code_references(tmp_path, config)

    assert len(refs) == 2
    assert {ref.provider_id for ref in refs} == {"function:process-order"}
    assert {ref.provider_title for ref in refs} == {"process_order"}


def test_builds_symbol_node_with_existing_code_prefix(tmp_path):
    config = _project(tmp_path)
    refs = scan_code_references(tmp_path, config)

    graph = build_graph({}, [], config, code_refs=refs)

    node_id = "CODE:function:process-order"
    assert node_id in graph
    assert graph.nodes[node_id]["title"] == "process_order"
    assert graph.nodes[node_id]["source_file"] == "src/service.py"
    assert set(graph.successors(node_id)) == {"REQ-01", "REQ-02"}


def test_missing_index_falls_back_to_file_node(tmp_path, capsys):
    config = _project(tmp_path, with_index=False)

    refs = scan_code_references(tmp_path, config)
    graph = build_graph({}, [], config, code_refs=refs)

    assert "CODE:src/service.py" in graph
    assert all(not ref.provider_id for ref in refs)
    assert "using file-level code traces" in capsys.readouterr().err


def test_stale_index_falls_back_to_file_node(tmp_path, capsys):
    config = _project(tmp_path)
    with sqlite3.connect(tmp_path / ".codegraph" / "codegraph.db") as db:
        db.execute("UPDATE files SET modified_at = 0")

    refs = scan_code_references(tmp_path, config)
    graph = build_graph({}, [], config, code_refs=refs)

    assert "CODE:src/service.py" in graph
    assert "no current index for src/service.py" in capsys.readouterr().err


def test_import_persists_symbol_source(tmp_path):
    _project(tmp_path)
    db = get_db(tmp_path / "reports" / "graph.db")

    do_import(tmp_path, db, quiet=True)

    symbol = db.execute(
        "SELECT * FROM artifacts WHERE id = ?",
        ("CODE:function:process-order",),
    ).fetchone()
    assert symbol["title"] == "process_order"
    assert symbol["source_file"] == "src/service.py"
    db.close()


def test_code_refs_cli_shows_symbol_and_source(tmp_path):
    _project(tmp_path)
    db_path = tmp_path / "reports" / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "code-refs"],
    )

    assert result.exit_code == 0, result.output
    assert "process_order (src/service.py)" in result.output
    assert "function:process-order" not in result.output

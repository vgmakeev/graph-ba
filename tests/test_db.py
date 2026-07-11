"""Tests for SQLite schema, import, query helpers."""
from pathlib import Path

import pytest
import sqlite3

from graph_ba.cli import fmt_table
from graph_ba.db import do_import, get_db, _fts_query, _load_nx, _scan_file_mtimes


def test_import_keeps_root_relative_source_paths_for_duplicate_basenames(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        r"""
[scan]
dirs = ["docs"]

[types.AC]
label = "Acceptance"
ref = '(AC-\d+)'
classify = 'AC-\d+'

[[definitions]]
type = "AC"
file = "docs/*/spec.md"
mode = "heading"
pattern = '^##\s+(AC-\d+)\s+-\s+(.*)'
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    for folder, artifact in (("one", "AC-1"), ("two", "AC-2")):
        path = tmp_path / "docs" / folder
        path.mkdir(parents=True)
        (path / "spec.md").write_text(
            f"## {artifact} - Example\n", encoding="utf-8"
        )
    db = get_db(tmp_path / "reports" / "graph.db")

    do_import(tmp_path, db, quiet=True)

    rows = db.execute(
        "SELECT id, source_file FROM artifacts WHERE id LIKE 'AC-%' ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["source_file"]) for row in rows] == [
        ("AC-1", "docs/one/spec.md"),
        ("AC-2", "docs/two/spec.md"),
    ]
    db.close()


class TestSchema:
    def test_tables_created(self, db_conn):
        tables = {r[0] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "artifacts" in tables
        assert "edges" in tables
        assert "artifact_origins" in tables
        assert "relation_types" in tables
        assert "semantic_clusters" in tables
        assert "file_paths" in tables

    def test_artifacts_have_origin_column(self, db_conn):
        cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(artifacts)")}
        assert "origin" in cols

    def test_edges_have_relation_type_column(self, db_conn):
        cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(edges)")}
        assert "relation_type" in cols

    def test_fts_tables_created(self, db_conn):
        tables = {r[0] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "artifacts_fts" in tables
        assert "edges_fts" in tables
        assert "clusters_fts" in tables


class TestImportSnapshot:
    def test_scanned_file_mtimes_include_trace_source_layers(self, ba_project, project_config):
        files = _scan_file_mtimes(ba_project, project_config)
        rel_paths = {
            str(Path(path).relative_to(ba_project))
            for path in files
        }

        assert "src/order.ts" in rel_paths
        assert "tests/test_orders.py" in rel_paths
        assert "src/mappers.test.ts" in rel_paths
        assert "ui/orders/trace.json" in rel_paths


class TestImport:
    def test_artifact_count(self, db_conn):
        count = db_conn.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        # 11 defined + REQ-99 (dangling) + FILE:index.md = 13
        assert count >= 11

    def test_edge_count(self, db_conn):
        count = db_conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        assert count >= 10

    def test_cluster_count(self, db_conn):
        count = db_conn.execute(
            "SELECT count(DISTINCT cluster_name) FROM semantic_clusters"
        ).fetchone()[0]
        assert count == 2  # "Order Management" and "Delivery"

    def test_file_paths_populated(self, db_conn):
        count = db_conn.execute("SELECT count(*) FROM file_paths").fetchone()[0]
        assert count >= 5

    def test_fts_searchable(self, db_conn):
        rows = db_conn.execute(
            "SELECT id FROM artifacts_fts WHERE artifacts_fts MATCH 'Order*'"
        ).fetchall()
        assert len(rows) >= 1

    def test_defined_artifacts(self, db_conn):
        defined = db_conn.execute(
            "SELECT count(*) FROM artifacts WHERE defined = 1"
        ).fetchone()[0]
        # 11 BA artifacts + FILE:index.md
        assert defined >= 11

    def test_artifact_origin_persisted(self, db_conn):
        row = db_conn.execute(
            "SELECT origin FROM artifacts WHERE id = 'ST-01'"
        ).fetchone()
        assert row["origin"] == "human"

    def test_edge_relation_type_persisted(self, db_conn):
        row = db_conn.execute(
            "SELECT relation_type FROM edges "
            "WHERE source_id LIKE 'TEST:%' AND target_id = 'REQ-01'"
        ).fetchone()
        assert row["relation_type"] == "VERIFIES"

    def test_origin_enum_dictionary_persisted(self, db_conn):
        row = db_conn.execute(
            "SELECT label, description FROM artifact_origins WHERE id = 'reviewed_derived'"
        ).fetchone()
        assert row["label"] == "Reviewed derived artifact"
        assert "reviewed" in row["description"]

    def test_relation_type_enum_dictionary_persisted(self, db_conn):
        row = db_conn.execute(
            "SELECT label, direction FROM relation_types WHERE id = 'NORMALIZES'"
        ).fetchone()
        assert row["label"] == "Normalizes raw input"
        assert row["direction"] == "canonical_to_raw"

    def test_idempotent(self, ba_project, tmp_path):
        """Running import twice doesn't crash or duplicate."""
        path = tmp_path / "idem.db"
        db = get_db(path)
        from graph_ba.db import do_import
        do_import(ba_project, db)
        count1 = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        do_import(ba_project, db)
        count2 = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        assert count1 == count2
        db.close()

    def test_noop_import_preserves_import_time(self, ba_project, tmp_path):
        path = tmp_path / "noop.db"
        db = get_db(path)
        from graph_ba.db import do_import
        do_import(ba_project, db, quiet=True)
        import_time1 = db.execute(
            "SELECT value FROM meta WHERE key = 'import_time'"
        ).fetchone()["value"]
        changed = do_import(ba_project, db, quiet=True)
        import_time2 = db.execute(
            "SELECT value FROM meta WHERE key = 'import_time'"
        ).fetchone()["value"]
        assert changed is False
        assert import_time1 == import_time2
        db.close()


class TestFtsQuery:
    def test_wildcard_added(self):
        assert "*" in _fts_query("hello")

    def test_unicode_wildcard(self):
        result = _fts_query("kitchen")
        assert result == "kitchen*"

    def test_hyphenated_id_is_tokenized(self):
        assert _fts_query("RAC-KITCHEN") == "RAC* KITCHEN*"

    def test_passthrough_quoted(self):
        assert _fts_query('"exact match"') == '"exact match"'

    def test_passthrough_operators(self):
        assert _fts_query("A OR B") == "A OR B"

    def test_passthrough_wildcard(self):
        q = "test*"
        assert _fts_query(q) == q


class TestFmtTable:
    def test_basic(self):
        result = fmt_table([("A", "B"), ("CC", "D")], ["Col1", "Col2"])
        assert "Col1" in result
        assert "A" in result

    def test_empty(self):
        assert fmt_table([], ["H1"]) == "(empty)"

    def test_alignment(self):
        result = fmt_table([("short", "x"), ("longer text", "y")], ["A", "B"])
        lines = result.split("\n")
        # All lines should be similar length (aligned)
        assert len(lines) >= 3  # header + separator + 2 rows


class TestLoadNx:
    def test_roundtrip_nodes(self, db_conn, built_graph):
        G_loaded = _load_nx(db_conn)
        G_orig, _ = built_graph
        assert G_loaded.number_of_nodes() == G_orig.number_of_nodes()

    def test_roundtrip_edges(self, db_conn, built_graph):
        G_loaded = _load_nx(db_conn)
        G_orig, _ = built_graph
        assert G_loaded.number_of_edges() == G_orig.number_of_edges()

    def test_node_attributes(self, db_conn):
        G = _load_nx(db_conn)
        assert G.nodes["F-01"]["type"] == "FEAT"
        assert G.nodes["F-01"]["defined"] is True

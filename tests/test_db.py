"""Tests for graph_db.py: SQLite schema, import, query helpers."""
import pytest
import sqlite3

from graph_ba.graph_db import get_db, _fts_query, fmt_table, _load_nx


class TestSchema:
    def test_tables_created(self, db_conn):
        tables = {r[0] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "artifacts" in tables
        assert "edges" in tables
        assert "semantic_clusters" in tables
        assert "file_paths" in tables

    def test_fts_tables_created(self, db_conn):
        tables = {r[0] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "artifacts_fts" in tables
        assert "edges_fts" in tables
        assert "clusters_fts" in tables


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

    def test_idempotent(self, ba_project, tmp_path):
        """Running import twice doesn't crash or duplicate."""
        path = tmp_path / "idem.db"
        db = get_db(path)
        from graph_ba.graph_db import do_import
        do_import(ba_project, db)
        count1 = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        do_import(ba_project, db)
        count2 = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        assert count1 == count2
        db.close()


class TestFtsQuery:
    def test_wildcard_added(self):
        assert "*" in _fts_query("hello")

    def test_unicode_wildcard(self):
        result = _fts_query("kitchen")
        assert result == "kitchen*"

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

"""Tests for MCP tool wrappers."""
import pytest

pytest.importorskip("mcp")

from graph_ba.mcp_server import ba_schema, ba_sql
from graph_ba.db import SCHEMA_VERSION


def test_ba_sql_allows_select(ba_project, db_path):
    data = ba_sql("SELECT count(*) AS c FROM artifacts",
                  root=str(ba_project), db_path=str(db_path))
    assert "rows" in data
    assert data["rows"][0]["c"] > 0


def test_ba_sql_rejects_with_insert(ba_project, db_path):
    data = ba_sql(
        "WITH x AS (SELECT 'mcp_write_guard' AS key, '1' AS value) "
        "INSERT INTO meta SELECT key, value FROM x",
        root=str(ba_project),
        db_path=str(db_path),
    )
    assert "error" in data

    check = ba_sql("SELECT value FROM meta WHERE key = 'mcp_write_guard'",
                   root=str(ba_project), db_path=str(db_path))
    assert check["rows"] == []


def test_ba_sql_rejects_writable_pragma(ba_project, db_path):
    data = ba_sql("PRAGMA user_version = 5",
                  root=str(ba_project), db_path=str(db_path))
    assert "error" in data

    check = ba_sql("PRAGMA user_version",
                   root=str(ba_project), db_path=str(db_path))
    assert check["rows"][0]["user_version"] == SCHEMA_VERSION


def test_ba_schema_returns_agent_contract(ba_project, db_path):
    data = ba_schema(root=str(ba_project), db_path=str(db_path))
    assert any(t["id"] == "FEAT" and t["origin"] == "reviewed_derived"
               for t in data["types"])
    assert any(o["id"] == "reviewed_derived" for o in data["origins"])
    assert any(r["id"] == "NORMALIZES" and r["direction"] == "canonical_to_raw"
               for r in data["relation_types"])

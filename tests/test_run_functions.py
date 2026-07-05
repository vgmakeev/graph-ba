"""Direct tests for command logic functions without CliRunner."""
from graph_ba.audit import run_anomalies, run_audit, run_coverage
from graph_ba.config import load_config
from graph_ba.db import get_db
from graph_ba.lint import do_lint
from graph_ba.review import run_review, run_validate


def test_run_validate_direct(ba_project, db_path):
    db = get_db(db_path)
    data = run_validate(db, ba_project, load_config(ba_project), "REQ-01")
    db.close()
    assert data["id"] == "REQ-01"
    assert data["verdict"] == "PASS"
    assert data["checks"]


def test_run_review_direct(ba_project, db_path):
    db = get_db(db_path)
    data = run_review(db, ba_project, load_config(ba_project), "F-01",
                      semantic=True, lines=5)
    db.close()
    assert data["artifact"]["id"] == "F-01"
    assert "issues" in data
    assert "linked_artifacts" in data


def test_run_coverage_direct(ba_project, db_path):
    db = get_db(db_path)
    data = run_coverage(db, load_config(ba_project))
    db.close()
    assert data["pairs"]
    assert "test_coverage" in data


def test_run_anomalies_direct(db_path):
    db = get_db(db_path)
    data = run_anomalies(db)
    db.close()
    assert "nodes" in data
    assert "issues" in data


def test_run_audit_direct(ba_project, db_path):
    db = get_db(db_path)
    data = run_audit(db, ba_project, load_config(ba_project))
    db.close()
    assert data["summary"]["issues"] >= 1
    assert data["candidates"]


def test_run_lint_direct(ba_project, db_path):
    db = get_db(db_path)
    findings = do_lint(db, ba_project, load_config(ba_project), quick=True)
    db.close()
    assert isinstance(findings, list)

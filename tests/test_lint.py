"""Tests for the `graph-ba lint` command."""
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from graph_ba.graph_db import cli, get_db, do_import, do_lint


# ── Synthetic project with lint-relevant content ─────────────────

TOML_LINT = """\
[scan]
dirs = ["docs"]

[types.FEAT]
label = "Features"
ref = '(?<![A-Za-z])(F-\\d{2})(?!\\d)'
classify = 'F-\\d{2}'

[types.REQ]
label = "Requirements"
ref = '(?<![A-Za-z])(REQ-\\d{2})(?!\\d)'
classify = 'REQ-\\d{2}'

[types.BP]
label = "Business Processes"
ref = '(?<![A-Za-z])(BP-\\d{2})(?!\\d)'
classify = 'BP-\\d{2}'

[[definitions]]
type = "FEAT"
file = "docs/features.md"
mode = "heading"
pattern = '^##\\s+(F-\\d{2})\\s*[—–\\-]\\s*(.*)'

[[definitions]]
type = "REQ"
file = "docs/requirements.md"
mode = "heading"
pattern = '^##\\s+(REQ-\\d{2})\\s*[—–\\-]\\s*(.*)'

[[definitions]]
type = "BP"
file = "docs/processes.md"
mode = "heading"
pattern = '^##\\s+(BP-\\d{2})\\s*[—–\\-]\\s*(.*)'

[[coverage]]
source = "FEAT"
target = "REQ"
label = "FEAT → REQ"

[code]
dirs = ["src"]
extensions = ["ts"]
marker = "@trace"
coverage_types = ["FEAT"]

[lint]
glossary_file = "docs/glossary.md"
meetings_dir = "meetings_refined"
stale_threshold_days = 5
todo_patterns = ["TODO", "TBD", "FIXME", "???", "CLARIFY"]
"""

FEATURES_MD = """\
# Features

## F-01 — Order Management

### Goal
Manage orders. References: REQ-01.

TODO: clarify order cancellation flow.

### Scope

Orders can be cancelled.

## F-02 — Delivery Tracking

### Goal

### Exceptions
References: REQ-02.
TBD: need delivery zones.

## F-03 — Empty Feature

### Goal

### Scope

"""

REQUIREMENTS_MD = """\
# Requirements

## REQ-01 — Must manage orders
Basic order management.

## REQ-02 — Must track delivery
Delivery tracking. FIXME: add courier logic.
Also: ???
"""

PROCESSES_MD = """\
# Processes

## BP-01 — Main Order Process
Order flow. References: REQ-01, F-01.

## BP-02 — Reporting
Analytics.
CLARIFY: report format.
"""

GLOSSARY_MD = """\
# Glossary

| Термин | EN | Определение |
|--------|----|-------------|
| **Заказ** | Order | A customer order |
| **Доставка** | Delivery | Delivery to customer |
| **Курьер** | Courier | A person who delivers |
| **KDS** | KDS | Kitchen display system |
"""

ORDER_TS = """\
// @trace: F-01
export function processOrder() {}
"""


@pytest.fixture
def lint_project(tmp_path):
    """Create a synthetic project with lint-relevant artifacts."""
    root = tmp_path / "lint_proj"
    root.mkdir()
    (root / "graph-ba.toml").write_text(TOML_LINT, encoding="utf-8")

    docs = root / "docs"
    docs.mkdir()
    (docs / "features.md").write_text(FEATURES_MD, encoding="utf-8")
    (docs / "requirements.md").write_text(REQUIREMENTS_MD, encoding="utf-8")
    (docs / "processes.md").write_text(PROCESSES_MD, encoding="utf-8")
    (docs / "glossary.md").write_text(GLOSSARY_MD, encoding="utf-8")

    src = root / "src"
    src.mkdir()
    (src / "order.ts").write_text(ORDER_TS, encoding="utf-8")

    return root


@pytest.fixture
def lint_db(lint_project):
    """Populated DB for lint tests."""
    db_path = lint_project / "reports" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = get_db(db_path)
    do_import(lint_project, db)
    db.close()
    return db_path


@pytest.fixture
def lint_env(lint_project, lint_db):
    """CliRunner + paths for lint CLI tests."""
    return CliRunner(), lint_project, lint_db


# ── Check 1: TODO/TBD markers ───────────────────────────────────

class TestTodoMarkers:
    def test_finds_todo(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        todos = [f for f in findings if f["category"] == "TODO_TBD"]
        messages = " ".join(f["message"] for f in todos)
        assert any("TODO" in f["message"] for f in todos)
        assert any("TBD" in f["message"] for f in todos)
        assert any("FIXME" in f["message"] for f in todos)
        assert any("CLARIFY" in f["message"] for f in todos)
        assert any("???" in f["message"] for f in todos)

    def test_todo_severity_is_warn(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        todos = [f for f in findings if f["category"] == "TODO_TBD"]
        assert all(f["severity"] == "WARN" for f in todos)

    def test_todo_for_specific_artifact(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, node_id="F-01", quick=True)
        db.close()

        todos = [f for f in findings if f["category"] == "TODO_TBD"]
        assert len(todos) >= 1
        assert all(f["artifact_id"] == "F-01" for f in todos)
        assert any("TODO" in f["message"] for f in todos)

    def test_todo_has_line_number(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        todos = [f for f in findings if f["category"] == "TODO_TBD"]
        assert all(f["line"] > 0 for f in todos)


# ── Check 2: Empty sections ─────────────────────────────────────

class TestEmptySections:
    def test_finds_empty_sections(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        empty = [f for f in findings if f["category"] == "EMPTY_SECTION"]
        assert len(empty) >= 1
        msgs = [f["message"] for f in empty]
        # F-02 has empty "Goal" section, F-03 has empty "Goal" and "Scope"
        assert any("Goal" in m for m in msgs)

    def test_empty_section_severity_is_warn(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        empty = [f for f in findings if f["category"] == "EMPTY_SECTION"]
        assert all(f["severity"] == "WARN" for f in empty)

    def test_non_empty_section_not_flagged(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        empty = [f for f in findings if f["category"] == "EMPTY_SECTION"]
        # F-01's "Scope" section has content — should not be flagged
        f01_scope = [f for f in empty
                     if f["artifact_id"] == "F-01" and "Scope" in f["message"]]
        assert len(f01_scope) == 0


# ── Check 3: Terminology ────────────────────────────────────────

class TestTerminology:
    def test_finds_en_terms(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        terms = [f for f in findings if f["category"] == "TERMINOLOGY"]
        # "Order" and "Delivery" and "Courier" are used in artifact text
        en_found = {f["message"].split('"')[1].lower() for f in terms}
        assert "order" in en_found or "delivery" in en_found

    def test_terminology_severity_is_info(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        terms = [f for f in findings if f["category"] == "TERMINOLOGY"]
        assert all(f["severity"] == "INFO" for f in terms)

    def test_skips_short_abbreviations(self, lint_project, lint_db):
        """KDS is <=6 chars and all-caps — should be skipped."""
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        terms = [f for f in findings if f["category"] == "TERMINOLOGY"]
        en_found = {f["message"].split('"')[1] for f in terms}
        assert "KDS" not in en_found

    def test_glossary_not_linted(self, lint_project, lint_db):
        """The glossary file itself should not be scanned."""
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        terms = [f for f in findings if f["category"] == "TERMINOLOGY"]
        assert all("glossary" not in f["file"] for f in terms)

    def test_one_finding_per_term_per_artifact(self, lint_project, lint_db):
        """Same EN term in same artifact should produce only one finding."""
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        terms = [f for f in findings if f["category"] == "TERMINOLOGY"]
        seen = set()
        for f in terms:
            key = (f["artifact_id"], f["message"].split('"')[1].lower())
            assert key not in seen, f"Duplicate term finding: {key}"
            seen.add(key)


# ── Check 4: Stale artifacts ────────────────────────────────────

class TestStale:
    def test_skipped_with_quick_flag(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        stale = [f for f in findings if f["category"] == "STALE"]
        assert len(stale) == 0

    def test_stale_detection_with_git(self, lint_project, lint_db):
        """Set up a git repo with old commits and recent meeting files."""
        # Create meetings_refined dir with a "recent" meeting
        meetings = lint_project / "meetings_refined"
        meetings.mkdir(exist_ok=True)
        (meetings / "2026-04-01_Meeting.md").write_text("# Meeting\n", encoding="utf-8")

        # Init git repo and commit files with an old date
        subprocess.run(["git", "init"], cwd=lint_project, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=lint_project, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init", "--allow-empty"],
            cwd=lint_project, capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
                "GIT_AUTHOR_DATE": "2026-01-01T00:00:00",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00",
                "HOME": str(lint_project),
                "PATH": subprocess.run(
                    ["printenv", "PATH"], capture_output=True, text=True
                ).stdout.strip(),
            },
        )
        subprocess.run(
            ["git", "add", "."],
            cwd=lint_project, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "old docs"],
            cwd=lint_project, capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
                "GIT_AUTHOR_DATE": "2026-01-15T00:00:00",
                "GIT_COMMITTER_DATE": "2026-01-15T00:00:00",
                "HOME": str(lint_project),
                "PATH": subprocess.run(
                    ["printenv", "PATH"], capture_output=True, text=True
                ).stdout.strip(),
            },
        )

        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=False)
        db.close()

        stale = [f for f in findings if f["category"] == "STALE"]
        # Files committed on 2026-01-15, meeting on 2026-04-01, threshold 5 days
        assert len(stale) > 0
        assert all(f["severity"] == "WARN" for f in stale)

    def test_no_meetings_dir_no_stale(self, lint_project, lint_db):
        """If meetings dir doesn't exist, no stale findings."""
        # Make sure no meetings_refined dir
        meetings = lint_project / "meetings_refined"
        if meetings.exists():
            import shutil
            shutil.rmtree(meetings)

        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=False)
        db.close()

        stale = [f for f in findings if f["category"] == "STALE"]
        assert len(stale) == 0


# ── Check 5: Code coverage ──────────────────────────────────────

class TestCodeCoverage:
    def test_finds_uncovered_features(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        code_cov = [f for f in findings if f["category"] == "CODE_COVERAGE"]
        uncovered_ids = {f["artifact_id"] for f in code_cov}
        # F-01 has @trace in order.ts, but F-02, F-03 do not
        assert "F-01" not in uncovered_ids
        assert "F-02" in uncovered_ids or "F-03" in uncovered_ids

    def test_code_coverage_severity_is_info(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        code_cov = [f for f in findings if f["category"] == "CODE_COVERAGE"]
        assert all(f["severity"] == "INFO" for f in code_cov)


# ── CLI integration ──────────────────────────────────────────────

class TestLintCLI:
    def test_runs_and_outputs(self, lint_env):
        runner, root, db_path = lint_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "lint", "--quick"
        ])
        assert result.exit_code == 0
        assert "BA Lint" in result.output
        assert "Lint:" in result.output

    def test_single_artifact(self, lint_env):
        runner, root, db_path = lint_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "lint", "F-01", "--quick"
        ])
        assert result.exit_code == 0
        assert "F-01" in result.output

    def test_json_output(self, lint_env):
        runner, root, db_path = lint_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json",
            "lint", "--quick"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "summary" in data
        assert "findings" in data
        assert data["summary"]["total"] == len(data["findings"])

    def test_json_finding_structure(self, lint_env):
        runner, root, db_path = lint_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json",
            "lint", "--quick"
        ])
        data = json.loads(result.output)
        if data["findings"]:
            f = data["findings"][0]
            assert "severity" in f
            assert "category" in f
            assert "artifact_id" in f
            assert "file" in f
            assert "line" in f
            assert "message" in f

    def test_quick_flag_skips_stale(self, lint_env):
        runner, root, db_path = lint_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "--json",
            "lint", "--quick"
        ])
        data = json.loads(result.output)
        stale = [f for f in data["findings"] if f["category"] == "STALE"]
        assert len(stale) == 0

    def test_no_errors_exit_code_zero(self, lint_env):
        runner, root, db_path = lint_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "lint", "--quick"
        ])
        # No ERR findings in test data, so exit code should be 0
        assert result.exit_code == 0

    def test_categories_displayed(self, lint_env):
        runner, root, db_path = lint_env
        result = runner.invoke(cli, [
            "--root", str(root), "--db", str(db_path), "lint", "--quick"
        ])
        # Should have at least TODO and empty section categories
        assert "Incompleteness markers" in result.output or "TODO_TBD" in result.output


# ── do_lint function directly ────────────────────────────────────

class TestDoLint:
    def test_returns_list(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        assert isinstance(findings, list)
        assert len(findings) > 0

    def test_sorted_by_severity(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        findings = do_lint(db, lint_project, config, quick=True)
        db.close()

        sev_order = {"ERR": 0, "WARN": 1, "INFO": 2}
        for i in range(len(findings) - 1):
            a = sev_order[findings[i]["severity"]]
            b = sev_order[findings[i + 1]["severity"]]
            assert a <= b, f"Findings not sorted: {findings[i]} before {findings[i+1]}"

    def test_no_config_still_works(self, lint_project, lint_db):
        """Lint should work even without [lint] config — uses defaults."""
        db = get_db(lint_db)
        findings = do_lint(db, lint_project, config=None, quick=True)
        db.close()

        # Should at least find TODO markers (uses default patterns)
        todos = [f for f in findings if f["category"] == "TODO_TBD"]
        assert len(todos) > 0

    def test_node_id_filter(self, lint_project, lint_db):
        db = get_db(lint_db)
        from graph_ba.config import load_config
        config = load_config(lint_project)
        all_findings = do_lint(db, lint_project, config, quick=True)
        f01_findings = do_lint(db, lint_project, config, node_id="F-01", quick=True)
        db.close()

        assert len(f01_findings) < len(all_findings)
        # TODO and terminology checks are scoped to the single artifact
        scoped = [f for f in f01_findings
                  if f["category"] in ("TODO_TBD", "TERMINOLOGY")]
        assert all(f["artifact_id"] == "F-01" for f in scoped)

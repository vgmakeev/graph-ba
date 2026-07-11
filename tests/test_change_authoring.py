import pytest

from graph_ba.change_authoring import (
    ChangeAuthoringError,
    add_artifact,
    add_link,
)
from graph_ba.change_workflow import init_change


CONFIG = r"""
[scan]
dirs = ["docs", ".graphba"]

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\d{3})'
classify = 'AC-[A-Z]+-\d{3}'

[types.RULE]
label = "Rules"
origin = "canonical"
ref = '(RULE-[A-Z-]+)'
classify = 'RULE-[A-Z-]+'

[[definitions]]
type = "AC"
file = "docs/spec.md"
mode = "heading"
pattern = '^##\s+(AC-[A-Z]+-\d{3})\s+-\s+(.*)'

[graph_native]
dirs = [".graphba"]
"""


def _project(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text(
        "## AC-ORD-001 - Existing order\nLegacy definition.\n",
        encoding="utf-8",
    )
    (tmp_path / ".graphba").mkdir()
    init_change(tmp_path, "CHG-order-live", intent="Add live order behavior")
    return tmp_path


def test_add_artifact_and_link_are_structured_and_idempotent(tmp_path):
    root = _project(tmp_path)

    artifact = add_artifact(
        root,
        "CHG-order-live",
        "AC",
        "AC-ORD-002",
        title="Live order",
        body="The order updates live.",
        links=["TRACES_TO=RULE-ORDER-LIVE"],
    )
    link = add_link(
        root,
        "CHG-order-live",
        "AC-ORD-002",
        "DEPENDS_ON",
        "RULE-ORDER-LIVE",
    )
    add_link(
        root,
        "CHG-order-live",
        "AC-ORD-002",
        "DEPENDS_ON",
        "RULE-ORDER-LIVE",
    )

    content = (root / artifact["file"]).read_text(encoding="utf-8")
    assert 'id="AC-ORD-002"' in content
    assert 'traces_to="RULE-ORDER-LIVE"' in content
    assert content.count('depends_on="RULE-ORDER-LIVE"') == 1
    assert link["relation"] == "DEPENDS_ON"


def test_add_artifact_refuses_duplicate_without_migration(tmp_path):
    root = _project(tmp_path)

    with pytest.raises(ChangeAuthoringError, match="already has definition"):
        add_artifact(
            root,
            "CHG-order-live",
            "AC",
            "AC-ORD-001",
            title="Migrated order",
        )

    result = add_artifact(
        root,
        "CHG-order-live",
        "AC",
        "AC-ORD-001",
        title="Migrated order",
        migrate=True,
    )

    content = (root / result["file"]).read_text(encoding="utf-8")
    assert "<!-- graph-ba: canonical-owner -->" in content


def test_migration_marks_existing_target_and_refuses_same_file_duplicate(tmp_path):
    root = _project(tmp_path)
    target = ".graphba/contract/order-live.md"
    add_artifact(
        root,
        "CHG-order-live",
        "RULE",
        "RULE-ORDER-LIVE",
        title="Live order rule",
        target_file=target,
    )

    migrated = add_artifact(
        root,
        "CHG-order-live",
        "AC",
        "AC-ORD-001",
        title="Migrated order",
        target_file=target,
        migrate=True,
    )

    content = (root / migrated["file"]).read_text(encoding="utf-8")
    assert content.startswith("<!-- graph-ba: canonical-owner -->")
    with pytest.raises(ChangeAuthoringError, match="already exists in migration target"):
        add_artifact(
            root,
            "CHG-order-live",
            "AC",
            "AC-ORD-001",
            title="Duplicate migrated order",
            target_file=target,
            migrate=True,
        )

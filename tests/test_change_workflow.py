import json
import subprocess

from click.testing import CliRunner

from graph_ba.change_workflow import proposal_check, semantic_diff
from graph_ba.cli import cli


CONFIG = r"""
[scan]
dirs = ["docs", ".graphba"]

[types.CHG]
label = "Changes"
origin = "derived"
ref = '(CHG-[A-Za-z0-9-]+)'
classify = 'CHG-[A-Za-z0-9-]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\d{3})'
classify = 'AC-[A-Z]+-\d{3}'

[[definitions]]
type = "AC"
file = "docs/spec.md"
mode = "heading"
pattern = '^##\s+(AC-[A-Z]+-\d{3})\s+-\s+(.*)'

[graph_native]
dirs = [".graphba"]
change_files = [".graphba/changes/*/change.yaml"]
"""


def _git(root, *args):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _project(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(CONFIG.strip() + "\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text(
        "## AC-ORD-001 - Create order\nOriginal behavior.\n\n"
        "## AC-ORD-002 - Cancel order\nCancellation behavior.\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "graph-ba@example.test")
    _git(tmp_path, "config", "user.name", "graph-ba tests")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Accepted contract")
    return tmp_path


def test_semantic_diff_changes_only_edited_stable_id_section(tmp_path):
    root = _project(tmp_path)
    (root / "docs" / "spec.md").write_text(
        "## AC-ORD-001 - Create order\nChanged behavior.\n\n"
        "## AC-ORD-002 - Cancel order\nCancellation behavior.\n",
        encoding="utf-8",
    )

    payload = semantic_diff(root, base_ref="main")

    assert payload["summary"]["contract"] == 1
    assert [(item["operation"], item["id"]) for item in payload["contract"]] == [
        ("modify", "AC-ORD-001")
    ]
    assert payload["git"]["base_ref"] == "main"
    assert len(payload["proposal_fingerprint"]) == 64


def test_change_init_is_one_manifest_and_proposal_check_uses_computed_delta(tmp_path):
    root = _project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-order-create",
            "--intent",
            "Clarify order creation",
            "--base-ref",
            "main",
            "--source",
            "RAC-ORD-001",
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = root / ".graphba" / "changes" / "CHG-order-create.yaml"
    assert manifest.is_file()
    assert not (root / ".graphba" / "changes" / "CHG-order-create").exists()

    (root / "docs" / "spec.md").write_text(
        "## AC-ORD-001 - Create order\nChanged behavior.\n\n"
        "## AC-ORD-002 - Cancel order\nCancellation behavior.\n",
        encoding="utf-8",
    )
    diff_result = runner.invoke(
        cli,
        ["--root", str(root), "change", "diff", "CHG-order-create"],
    )
    assert diff_result.exit_code == 0, diff_result.output
    delta = json.loads(diff_result.output)
    assert [item["id"] for item in delta["contract"]] == ["AC-ORD-001"]

    check_result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "check",
            "CHG-order-create",
            "--stage",
            "proposal",
        ],
    )
    assert check_result.exit_code == 0, check_result.output
    check = json.loads(check_result.output)
    assert check["pass"] is True
    assert check["proposal_fingerprint"] == delta["proposal_fingerprint"]

    context_result = runner.invoke(
        cli,
        ["--root", str(root), "change", "context", "CHG-order-create"],
    )
    assert context_result.exit_code == 0, context_result.output
    assert json.loads(context_result.stdout)["seeds"] == ["AC-ORD-001"]

    compile_result = runner.invoke(
        cli,
        ["--root", str(root), "change", "compile", "CHG-order-create"],
    )
    assert compile_result.exit_code == 0, compile_result.output
    report_dir = root / "reports" / "graphba" / "changes" / "CHG-order-create"
    assert (report_dir / "semantic-diff.json").is_file()
    assert (report_dir / "context.json").is_file()
    assert (report_dir / "proposal-check.json").is_file()
    assert (report_dir / "delivery-check.json").is_file()

    release_result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "check",
            "CHG-order-create",
            "--stage",
            "release",
            "--mode",
            "review",
        ],
    )
    assert release_result.exit_code != 0
    assert json.loads(release_result.stdout)["targets"] == ["AC-ORD-001"]


def test_proposal_check_requires_intent_and_contract_delta():
    delta = {
        "contract": [],
        "proposal_fingerprint": "fingerprint",
        "git": {"base_commit": "base"},
    }

    result = proposal_check(delta, {})

    assert result["pass"] is False
    assert {finding["code"] for finding in result["findings"]} == {
        "missing_intent",
        "empty_contract_delta",
    }

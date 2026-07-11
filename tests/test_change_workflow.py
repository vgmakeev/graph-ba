import json
import subprocess

from click.testing import CliRunner

from graph_ba.change_workflow import ChangeWorkflowService, proposal_check, semantic_diff
from graph_ba.cli import cli
from graph_ba.db import do_import, get_db


CONFIG = r"""
[scan]
dirs = ["docs", ".graphba", "reports/graphba/observed"]

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

[types.ENT]
label = "Entities"
origin = "canonical"
ref = '(ENT-[A-Za-z0-9-]+)'
classify = 'ENT-[A-Za-z0-9-]+'

[[definitions]]
type = "AC"
file = "docs/spec.md"
mode = "heading"
pattern = '^##\s+(AC-[A-Z]+-\d{3})\s+-\s+(.*)'

[graph_native]
dirs = [".graphba", "reports/graphba/observed"]
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
    (tmp_path / ".graphba").mkdir()
    (tmp_path / ".graphba" / "linked.md").write_text(
        ':::artifact type="AC" id="AC-ORD-003" title="Legacy order rule" '
        'traces_to="AC-ORD-001"\nLegacy behavior.\n:::\n',
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
    assert [item["path"] for item in payload["contract_files"]] == ["docs/spec.md"]
    assert payload["supporting_files"] == []
    assert payload["delivery_files"] == []


def test_semantic_diff_separates_unrelated_delivery_files(tmp_path):
    root = _project(tmp_path)
    (root / "docs" / "spec.md").write_text(
        "## AC-ORD-001 - Create order\nChanged behavior.\n\n"
        "## AC-ORD-002 - Cancel order\nCancellation behavior.\n",
        encoding="utf-8",
    )
    (root / "scratch.txt").write_text("unrelated worktree edit\n", encoding="utf-8")
    (root / ".graphba" / "evidence-policy.json").write_text("{}\n", encoding="utf-8")

    payload = semantic_diff(root, base_ref="main")

    assert [item["path"] for item in payload["contract_files"]] == ["docs/spec.md"]
    assert [item["path"] for item in payload["supporting_files"]] == [
        ".graphba/evidence-policy.json"
    ]
    assert [item["path"] for item in payload["delivery_files"]] == ["scratch.txt"]
    assert payload["summary"]["contract_files"] == 1
    assert payload["summary"]["supporting_files"] == 1
    assert payload["summary"]["delivery_files"] == 1


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

    review_result = runner.invoke(
        cli,
        ["--root", str(root), "change", "review", "CHG-order-create"],
    )
    assert review_result.exit_code == 0, review_result.output
    assert "# graph-ba review: CHG-order-create" in review_result.output
    assert "## Semantic Delta" in review_result.output
    assert "AC-ORD-001" in review_result.output

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


def test_compiled_graph_keeps_removed_artifact_and_base_impact_path(tmp_path):
    root = _project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-remove-legacy",
            "--intent",
            "Remove legacy behavior",
            "--base-ref",
            "main",
        ],
    )
    assert result.exit_code == 0, result.output
    (root / ".graphba" / "linked.md").unlink()
    db = get_db(root / "reports" / "graph.db")
    do_import(root, db, quiet=True, force=True)

    compiled = ChangeWorkflowService(root, db).compile("CHG-remove-legacy")
    db.close()

    removed = [
        item for item in compiled["graph_delta"]["nodes"]
        if item["operation"] == "remove"
    ]
    assert [item["id"] for item in removed] == ["AC-ORD-003"]
    ac_001 = next(item for item in compiled["impact"]["nodes"] if item["id"] == "AC-ORD-001")
    assert ac_001["path"][0]["view"] == "base"
    assert ac_001["path"][0]["relation"] == "TRACES_TO"


def test_approval_is_invalidated_by_contract_edit(tmp_path):
    root = _project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-approve-order",
            "--intent",
            "Clarify order behavior",
            "--base-ref",
            "main",
        ],
    )
    assert result.exit_code == 0, result.output
    spec = root / "docs" / "spec.md"
    spec.write_text(spec.read_text().replace("Original behavior.", "Approved behavior."))
    service = ChangeWorkflowService(root)

    _git(root, "add", ".graphba/changes/CHG-approve-order.yaml", "docs/spec.md")
    _git(root, "commit", "-m", "Propose approved order behavior")
    service.approve(
        "CHG-approve-order",
        "reviewer@example.test",
        "https://example.test/reviews/1",
    )
    assert service.approval("CHG-approve-order")["reason"] == "approval_not_committed"
    _git(root, "add", ".graphba/approvals/CHG-approve-order.json")
    _git(root, "commit", "-m", "Record human approval")
    assert service.approval("CHG-approve-order")["valid"] is True

    approval = root / ".graphba" / "approvals" / "CHG-approve-order.json"
    approval.write_text(approval.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    status = service.approval("CHG-approve-order")
    assert status["valid"] is False
    assert status["reason"] == "approval_not_committed"
    _git(root, "checkout", "--", ".graphba/approvals/CHG-approve-order.json")

    spec.write_text(spec.read_text().replace("Approved behavior.", "Changed after approval."))
    assert service.approval("CHG-approve-order")["valid"] is False


def test_approval_is_invalidated_by_proposal_policy_edit(tmp_path):
    root = _project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-policy-order",
            "--intent",
            "Bind evidence policy",
            "--base-ref",
            "main",
            "--no-branch",
        ],
    )
    assert result.exit_code == 0, result.output
    spec = root / "docs" / "spec.md"
    spec.write_text(spec.read_text().replace("Original behavior.", "Policy-bound behavior."))
    service = ChangeWorkflowService(root)
    _git(root, "add", ".graphba/changes/CHG-policy-order.yaml", "docs/spec.md")
    _git(root, "commit", "-m", "Propose policy-bound behavior")
    service.approve(
        "CHG-policy-order",
        "reviewer@example.test",
        "https://example.test/reviews/2",
    )
    _git(root, "add", ".graphba/approvals/CHG-policy-order.json")
    _git(root, "commit", "-m", "Record policy approval")
    assert service.approval("CHG-policy-order")["valid"] is True

    (root / ".graphba" / "evidence-policy.json").write_text(
        '{"gate_blocking_modes":["release"]}\n',
        encoding="utf-8",
    )

    assert service.approval("CHG-policy-order")["valid"] is False


def test_change_init_can_create_isolated_worktree_from_dirty_checkout(tmp_path):
    root = _project(tmp_path)
    (root / "scratch.txt").write_text("parallel user work\n", encoding="utf-8")
    worktree = tmp_path / "order-change-worktree"

    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-worktree-order",
            "--intent",
            "Use an isolated worktree",
            "--worktree",
            str(worktree),
        ],
    )

    assert result.exit_code == 0, result.output
    assert _git(root, "branch", "--show-current") == "main"
    manifest = worktree / ".graphba" / "changes" / "CHG-worktree-order.yaml"
    content = manifest.read_text(encoding="utf-8")
    assert manifest.is_file()
    assert f'base_ref: "{_git(root, "rev-parse", "main")}"' in content
    assert 'target_ref: "main"' in content
    assert _git(worktree, "branch", "--show-current") == "change/chg-worktree-order"


def test_change_branch_starts_from_explicit_base_and_monitors_target(tmp_path):
    root = _project(tmp_path)
    _git(root, "switch", "-c", "accepted-base")
    spec = root / "docs" / "spec.md"
    spec.write_text(spec.read_text().replace("Original behavior.", "Accepted base behavior."))
    _git(root, "add", "docs/spec.md")
    _git(root, "commit", "-m", "Prepare accepted base")
    accepted_commit = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")

    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-explicit-base",
            "--intent",
            "Start from an explicit accepted base",
            "--base-ref",
            "accepted-base",
            "--target-ref",
            "main",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _git(root, "rev-parse", "HEAD") == accepted_commit
    manifest = (root / ".graphba" / "changes" / "CHG-explicit-base.yaml").read_text()
    assert f'base_ref: "{accepted_commit}"' in manifest
    assert 'target_ref: "main"' in manifest


def test_proposal_check_reports_semantic_rebase_conflict(tmp_path):
    root = _project(tmp_path)
    worktree = tmp_path / "conflict-worktree"
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-conflict-order",
            "--intent",
            "Change order creation",
            "--worktree",
            str(worktree),
        ],
    )
    assert result.exit_code == 0, result.output
    worktree_spec = worktree / "docs" / "spec.md"
    worktree_spec.write_text(
        worktree_spec.read_text().replace("Original behavior.", "Proposed behavior.")
    )

    root_spec = root / "docs" / "spec.md"
    root_spec.write_text(root_spec.read_text().replace("Original behavior.", "Upstream behavior."))
    _git(root, "add", "docs/spec.md")
    _git(root, "commit", "-m", "Change accepted order behavior")

    check = ChangeWorkflowService(worktree).proposal_check("CHG-conflict-order")

    assert check["pass"] is False
    assert check["rebase"]["status"] == "conflict"
    assert check["rebase"]["conflicts"] == [{"kind": "artifact", "id": "AC-ORD-001"}]


def test_proposal_requires_one_canonical_owner_for_migrated_id(tmp_path):
    root = _project(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-migrate-order",
            "--intent",
            "Migrate one canonical owner",
            "--base-ref",
            "main",
            "--no-branch",
        ],
    )
    assert result.exit_code == 0, result.output
    migrated = root / ".graphba" / "migrated.md"
    migrated.write_text(
        ':::artifact type="AC" id="AC-ORD-001" title="Migrated order"\n:::\n',
        encoding="utf-8",
    )
    service = ChangeWorkflowService(root)

    blocked = service.proposal_check("CHG-migrate-order")
    assert blocked["pass"] is False
    assert blocked["findings"][0]["code"] == "duplicate_canonical_owner"

    migrated.write_text(
        '<!-- graph-ba: canonical-owner -->\n'
        ':::artifact type="AC" id="AC-ORD-001" title="Migrated order"\n:::\n',
        encoding="utf-8",
    )
    assert service.proposal_check("CHG-migrate-order")["pass"] is True


def test_proposal_rejects_duplicate_canonical_id_inside_one_file(tmp_path):
    root = _project(tmp_path)
    init_result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-duplicate-order",
            "--intent",
            "Detect duplicate definitions",
            "--base-ref",
            "main",
            "--no-branch",
        ],
    )
    assert init_result.exit_code == 0, init_result.output
    (root / ".graphba" / "duplicate.md").write_text(
        ':::artifact type="AC" id="AC-ORD-004" title="First"\n:::\n\n'
        ':::artifact type="AC" id="AC-ORD-004" title="Second"\n:::\n',
        encoding="utf-8",
    )

    result = ChangeWorkflowService(root).proposal_check("CHG-duplicate-order")

    assert result["pass"] is False
    assert any(
        finding["code"] == "duplicate_canonical_owner"
        for finding in result["findings"]
    )


def test_discover_returns_source_location(tmp_path):
    root = _project(tmp_path)
    db = get_db(root / "reports" / "graph.db")
    do_import(root, db, quiet=True, force=True)

    payload = ChangeWorkflowService(root, db).discover("Create order")
    db.close()

    candidate = next(item for item in payload["candidates"] if item["id"] == "AC-ORD-001")
    assert candidate["source_file"] == "docs/spec.md"
    assert candidate["line_number"] == 1

    fallback_db = get_db(root / "reports" / "graph.db")
    payload = ChangeWorkflowService(root, fallback_db).discover("unmatched words plus order")
    fallback_db.close()
    assert payload["strategy"] == "any_term"
    assert any(item["id"] == "AC-ORD-001" for item in payload["candidates"])

    seeded_db = get_db(root / "reports" / "graph.db")
    payload = ChangeWorkflowService(root, seeded_db).discover(
        "unmatched words plus order",
        seed_ids=["AC-ORD-002"],
    )
    seeded_db.close()
    assert payload["strategy"] == "seeded"
    assert payload["seed_ids"] == ["AC-ORD-002"]
    assert [item["id"] for item in payload["candidates"]] == ["AC-ORD-002"]


def test_compiled_change_reuses_cached_base_graph(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    spec = root / "docs" / "spec.md"
    spec.write_text(spec.read_text().replace("Original behavior.", "Changed behavior."))
    db = get_db(root / "reports" / "graph.db")
    do_import(root, db, quiet=True, force=True)
    service = ChangeWorkflowService(root, db)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-cache-order",
            "--intent",
            "Cache the base graph",
            "--base-ref",
            "main",
            "--no-branch",
        ],
    )
    assert result.exit_code == 0, result.output

    first = service.compile("CHG-cache-order")
    cache_files = list((tmp_path / "cache" / "graph-ba" / "base-graphs").rglob("*.db"))
    assert len(cache_files) == 1
    second = service.compile("CHG-cache-order")
    db.close()

    assert second["graph_delta"] == first["graph_delta"]


def test_observed_alias_and_dangling_reference_are_not_contract_delta(tmp_path):
    root = _project(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "change",
            "init",
            "CHG-observed-order",
            "--intent",
            "Observe delivery without changing the contract",
            "--base-ref",
            "main",
            "--scope",
            "AC-ORD-001",
        ],
    )
    assert result.exit_code == 0, result.output
    observed = root / "reports" / "graphba" / "observed"
    observed.mkdir(parents=True)
    (observed / "mini.md").write_text(
        ':::artifact type="ENT" id="ENT-Order" state="observed" '
        'origin="implementation" title="Order" traces_to="AC-MISSING-999"\n'
        "Observed provider alias.\n:::\n",
        encoding="utf-8",
    )
    db = get_db(root / "reports" / "graph.db")
    do_import(root, db, quiet=True, force=True)

    compiled = ChangeWorkflowService(root, db).compile("CHG-observed-order")
    db.close()

    assert compiled["graph_delta"]["nodes"] == []

"""Click command line interface for graph-ba."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path

import click

from graph_ba.audit import _issue_fingerprints, run_audit
from graph_ba.change_workflow import ChangeWorkflowError, ChangeWorkflowService
from graph_ba.lint import do_lint
from graph_ba.review import run_review

from .artifact_state import _load_fingerprint_snapshot
from .gate_analysis import _change_payload
from .gates import _gate_payload, delivery_gate_payload

from .cli_core import (
    _change_manifest_path,
    _change_path,
    _conn,
    _delivery_target_ids,
    _json_out,
    _read_change_manifest,
    _require_graph,
    change_group,
    cli,
)


@change_group.command("context")
@click.argument("change_id")
@click.pass_context
def change_context(ctx, change_id):
    """Show bounded implementation context for the proposed contract delta."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    try:
        payload = ChangeWorkflowService(root, db).context(change_id)
    except ChangeWorkflowError as exc:
        db.close()
        raise click.ClickException(str(exc)) from exc
    db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@change_group.command("check")
@click.argument("change_id")
@click.option(
    "--stage",
    default=None,
    type=click.Choice(["proposal", "release"]),
    help="Check the contract proposal or delivered implementation",
)
@click.option(
    "--mode",
    default=None,
    type=click.Choice(["explore", "dev", "review", "release"]),
    help="Gate strictness; defaults to change.yaml mode or dev",
)
@click.option(
    "--snapshot",
    "snapshot_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Accepted fingerprint snapshot for stale checks",
)
@click.pass_context
def change_check(ctx, change_id, stage, mode, snapshot_path):
    """Evaluate proposal readability or the existing delivery gate."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    if stage == "proposal":
        try:
            result = ChangeWorkflowService(root).proposal_check(change_id)
        except ChangeWorkflowError as exc:
            raise click.ClickException(str(exc)) from exc
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if not result["pass"]:
            raise click.ClickException(f"Proposal check failed: {result['verdict']}")
        return
    if stage == "release":
        db = _conn(ctx)
        _require_graph(ctx, db)
        service = ChangeWorkflowService(root, db)
        try:
            semantic_payload = service.diff(change_id)
            manifest = service.manifest(change_id)
            approval = service.approval(change_id)
        except ChangeWorkflowError as exc:
            db.close()
            raise click.ClickException(str(exc)) from exc
        result = delivery_gate_payload(
            db,
            root,
            _delivery_target_ids(semantic_payload, manifest),
            proposal_fingerprint=semantic_payload["proposal_fingerprint"],
            mode=mode,
            snapshot_path=snapshot_path,
            approval=approval,
            require_approval=True,
        )
        db.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if not result["pass"]:
            raise click.ClickException(f"Release check failed: {result['verdict']}")
        return
    db = _conn(ctx)
    _require_graph(ctx, db)
    result = _gate_payload(db, root, change_id, mode, snapshot_path)
    db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["pass"]:
        raise click.ClickException(f"Gate failed: {result['verdict']}")


@change_group.command("approve")
@click.argument("change_id")
@click.option("--reviewer", required=True, help="Human reviewer identity")
@click.pass_context
def change_approve(ctx, change_id, reviewer):
    """Write a human approval attestation for the current contract fingerprint."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        payload = ChangeWorkflowService(root).approve(change_id, reviewer)
    except ChangeWorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@change_group.command("accept")
@click.argument("change_id")
@click.option(
    "--snapshot",
    "snapshot_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Project accepted fingerprint snapshot path",
)
@click.pass_context
def change_accept(ctx, change_id, snapshot_path):
    """Write accepted delta/snapshot for a change and optionally update project snapshot."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    manifest_path = _change_manifest_path(root, change_id)
    if manifest_path and manifest_path.parent == root / ".graphba" / "changes":
        raise click.ClickException(
            "Git-native changes are accepted by protected review and merge; "
            "change accept is only for the legacy directory layout"
        )
    db = _conn(ctx)
    _require_graph(ctx, db)
    data = _change_payload(db, root, change_id)
    db.close()
    change_dir = _change_path(root, change_id)
    archive_dir = change_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    accepted_delta = {
        "schema": "graph-ba.accepted-delta.v1",
        "change": data["change"],
        "scope": [item["id"] for item in data["scope"]],
    }
    accepted_snapshot = {
        "schema": "graph-ba.fingerprint-snapshot.v1",
        "artifacts": {
            item["id"]: {
                "lifecycle": item["lifecycle"],
                "fingerprints": item["fingerprints"],
            }
            for item in data["scope"]
        },
    }
    (archive_dir / "accepted-delta.json").write_text(
        json.dumps(accepted_delta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (archive_dir / "accepted-snapshot.json").write_text(
        json.dumps(accepted_snapshot, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    if snapshot_path:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        current = _load_fingerprint_snapshot(snapshot_path)
        artifacts = dict(current.get("artifacts", {}))
        artifacts.update(accepted_snapshot["artifacts"])
        snapshot_path.write_text(
            json.dumps(
                {"schema": "graph-ba.fingerprint-snapshot.v1", "artifacts": artifacts},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    _rewrite_change_state(change_dir / "change.yaml", "accepted")
    print(f"Accepted graph-ba change: {change_id}")


@change_group.command("archive")
@click.argument("change_id")
@click.pass_context
def change_archive(ctx, change_id):
    """Move a change under `.graphba/changes/archive/YYYY-MM-DD-<id>/`."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    manifest_path = _change_manifest_path(root, change_id)
    if manifest_path and manifest_path.parent == root / ".graphba" / "changes":
        raise click.ClickException(
            "Git already archives Git-native changes; change archive is only "
            "for the legacy directory layout"
        )
    src = _change_path(root, change_id)
    if not src.exists():
        raise click.ClickException(f"Change not found: {src}")
    dst = (
        root
        / ".graphba"
        / "changes"
        / "archive"
        / f"{date.today().isoformat()}-{change_id}"
    )
    if dst.exists():
        raise click.ClickException(f"Archive target already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    _rewrite_change_state(src / "change.yaml", "archived")
    shutil.move(str(src), str(dst))
    print(f"Archived graph-ba change: {dst}")


def _rewrite_change_state(path: Path, state: str) -> None:
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("state:"):
            lines[index] = f"state: {state}"
            replaced = True
            break
    if not replaced:
        lines.append(f"state: {state}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@cli.command()
@click.argument("node_id_or_file")
@click.option(
    "--lines",
    default=0,
    type=int,
    help="Max lines per artifact in --semantic mode (0 = no limit)",
)
@click.option("--nums", is_flag=True, help="Enable numeric conflict detection")
@click.option(
    "--semantic",
    is_flag=True,
    help="Full text of each linked artifact for semantic validation",
)
@click.option(
    "--types",
    default=None,
    help="Comma-separated artifact types to include in --semantic (e.g. ST,BR_REQ,BR_RULE,BP)",
)
@click.pass_context
def review(ctx, node_id_or_file, lines, nums, semantic, types):
    """Full review: validate + context in one call."""
    from graph_ba.config import load_config

    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        config = load_config(root)
    except FileNotFoundError:
        config = None
    data = run_review(
        db,
        root,
        config,
        node_id_or_file,
        semantic=semantic,
        lines=lines,
        nums=nums,
        types=types,
    )
    db.close()

    if _json_out(ctx, data):
        return
    if "error" in data:
        print(data["error"])
        return

    artifact = data["artifact"]
    print(f"{'═' * 70}")
    print(f"  REVIEW: {artifact['id']} — {artifact['title']}")
    print(
        f"  Type: {artifact['type']}  |  File: {artifact['source_file']}:{artifact['line_number']}"
    )
    print(f"{'═' * 70}")

    issues = data["issues"]
    if issues:
        print(f"\n┌─ Issues ({len(issues)}) ─────────────────────────────────")
        for issue in issues:
            print(f"│ [{issue['severity']:6s}] {issue['message']}")
        print(f"└{'─' * 55}")
    else:
        print("\n✓ No issues found")

    if data["clusters"]:
        print(f"\nClusters: {', '.join(data['clusters'])}")

    if semantic:
        linked = data["linked_artifacts"]
        print(f"\n{'═' * 70}")
        print(f"  LINKED ARTIFACTS ({len(linked)})")
        print(f"{'═' * 70}")
        for art in linked:
            if not art.get("defined", False):
                print(f"\n  ▸ {art['id']} — definition not found in DB")
                continue
            section = art.get("section")
            if section:
                print(f"\n{'─' * 70}")
                print(f"  {art['id']}: {art.get('title') or ''}")
                print(f"  File: {art.get('source_file')}:{art.get('line_number')}")
                print(f"{'─' * 70}")
                print(section)
        code_in = [r for r in data["incoming"] if r["ref_id"].startswith("CODE:")]
        if code_in:
            print(f"\n── Code References ({len(code_in)}) ──")
            for r in code_in:
                code_path = r["ref_id"].removeprefix("CODE:")
                print(f"  {code_path}:{r['line_number']}")
                if r.get("context"):
                    print(f"    {r['context'][:70]}")
        return

    print(f"\n── Outgoing references ({len(data['outgoing'])}) ──")
    for r in data["outgoing"]:
        print(
            f"  → [{r.get('type') or '?'}] {r['ref_id']} — {(r.get('title') or '')[:55]}"
        )
        if r.get("source_file"):
            print(f"    Ref in: {r['source_file']}:{r.get('line_number') or 0}")
        if r.get("context"):
            print(f"    Context: {r['context'][:70]}")

    ba_in = [r for r in data["incoming"] if not r["ref_id"].startswith("CODE:")]
    code_in = [r for r in data["incoming"] if r["ref_id"].startswith("CODE:")]
    if ba_in:
        print(f"\n── Incoming references ({len(ba_in)}) ──")
        for r in ba_in:
            print(
                f"  ← [{r.get('type') or '?'}] {r['ref_id']} — {(r.get('title') or '')[:55]}"
            )
            if r.get("source_file"):
                print(f"    Ref in: {r['source_file']}:{r.get('line_number') or 0}")
            if r.get("context"):
                print(f"    Context: {r['context'][:70]}")
    if code_in:
        print(f"\n── Code References ({len(code_in)}) ──")
        for r in code_in:
            code_path = r["ref_id"].removeprefix("CODE:")
            print(f"  {code_path}:{r['line_number']}")
            if r.get("context"):
                print(f"    {r['context'][:70]}")


def _emit_validate(ctx, artifact_id: str, checks: list):
    """Print validate result (human or JSON) and exit 1 on FAIL."""
    verdict = "FAIL" if any(c["status"] == "fail" for c in checks) else "PASS"
    if not _json_out(ctx, {"id": artifact_id, "verdict": verdict, "checks": checks}):
        symbols = {"pass": "✓", "fail": "✗", "warn": "⚠"}
        print(f"Validate: {artifact_id}")
        for c in checks:
            print(f"  {symbols[c['status']]} {c['name']} — {c['detail']}")
        print(f"\nVERDICT: {verdict}")
    if verdict == "FAIL":
        ctx.exit(1)


@cli.command()
@click.option("--top", default=30, help="Max review candidates to return")
@click.option(
    "--baseline",
    "baseline_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Compare issues against a baseline; exit 1 only on NEW issues",
)
@click.option(
    "--write-baseline",
    "write_baseline_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write current issue fingerprints to a baseline file",
)
@click.pass_context
def audit(ctx, top, baseline_path, write_baseline_path):
    """Global audit: anomalies + coverage gaps + prioritized review list."""
    from graph_ba.config import load_config

    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        config = load_config(root)
    except Exception:
        config = None
    result = run_audit(db, root, config, top)
    db.close()
    issues = result["issues"]

    if write_baseline_path:
        fps = sorted({fp for iss in issues for fp in _issue_fingerprints(iss)})
        write_baseline_path.parent.mkdir(parents=True, exist_ok=True)
        write_baseline_path.write_text(
            json.dumps(
                {"version": 1, "fingerprints": fps}, ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Baseline written: {write_baseline_path} ({len(fps)} fingerprints)")
        return

    baseline_cmp = None
    if baseline_path:
        try:
            baseline_data = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
            baseline_set = set(baseline_data.get("fingerprints", []))
        except (OSError, json.JSONDecodeError) as e:
            raise click.ClickException(f"Cannot read baseline {baseline_path}: {e}")
        current = {fp for iss in issues for fp in _issue_fingerprints(iss)}
        baseline_cmp = {
            "new": sorted(current - baseline_set),
            "known": sorted(current & baseline_set),
            "resolved": sorted(baseline_set - current),
        }
        result["new"] = baseline_cmp["new"]
        result["known"] = baseline_cmp["known"]
        result["resolved"] = baseline_cmp["resolved"]

    if _json_out(ctx, result):
        if baseline_cmp and baseline_cmp["new"]:
            ctx.exit(1)
        return

    if baseline_cmp:
        new, known, resolved = (
            baseline_cmp["new"],
            baseline_cmp["known"],
            baseline_cmp["resolved"],
        )
        print(
            f"Global Audit ({result['summary']['artifacts']} nodes, "
            f"{result['summary']['edges']} edges) vs baseline {baseline_path}"
        )
        print(
            f"Baseline: {len(new)} new / {len(known)} known / {len(resolved)} resolved"
        )
        if new:
            print(f"\n── New issues ({len(new)}) ──")
            for fp in new:
                print(f"  {fp}")
            ctx.exit(1)
        return

    print(
        f"Global Audit ({result['summary']['artifacts']} nodes, {result['summary']['edges']} edges)"
    )
    print()
    if not issues:
        print("No issues found.")
        return

    by_type = {}
    for iss in issues:
        by_type.setdefault(iss["type"], []).append(iss)
    print(f"── Issues ({len(issues)}) ──")
    for cat in [
        "CYCLE",
        "DANGLING",
        "COVERAGE_GAP",
        "MISSING_CROSS_LAYER",
        "MISSING_BIDIR",
        "BRIDGE",
        "BOTTLENECK",
    ]:
        items = by_type.get(cat, [])
        if not items:
            continue
        print(f"\n  {cat} ({len(items)}):")
        for iss in items[:10]:
            if cat == "CYCLE":
                print(f"    {' → '.join(iss['ids'])}")
            elif cat == "DANGLING":
                print(f"    {iss['id']} ← {', '.join(iss['referenced_by'][:5])}")
            elif cat == "COVERAGE_GAP":
                print(
                    f"    {iss['source']}→{iss['target']}: {iss['pct']}% "
                    f"(missing: {', '.join(iss['missing'][:10])})"
                )
            elif cat == "MISSING_CROSS_LAYER":
                print(f"    {iss['id']} → needs {iss['expected']} ({iss['label']})")
            elif cat == "MISSING_BIDIR":
                print(f"    {iss['id']} → {iss['target']} (one-way)")
            elif cat == "BRIDGE":
                print(f"    {iss['ids'][0]} — {iss['ids'][1]}")
            elif cat == "BOTTLENECK":
                print(f"    {iss['id']} degree={iss['degree']}")

    print()
    print(f"── Review Candidates ({len(result['candidates'])}) ──")
    for c in result["candidates"]:
        prio = "HIGH" if c["priority"] == "high" else "    "
        reasons = ", ".join(c["reasons"])
        print(f"  {prio}  {c['id']:12s} [{c['type']:4s}]  {reasons}")


@cli.command()
@click.argument("node_id", required=False, default=None)
@click.option("--quick", is_flag=True, help="Skip git-based checks (stale)")
@click.pass_context
def lint(ctx, node_id, quick):
    """Content quality lint: TODO markers, empty sections, terminology, staleness, code coverage."""
    from graph_ba.config import load_config

    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()

    try:
        config = load_config(root)
    except FileNotFoundError:
        config = None

    findings = do_lint(db, root, config, node_id, quick)
    db.close()

    # Count by severity
    counts = {"ERR": 0, "WARN": 0, "INFO": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    # JSON output
    if _json_out(
        ctx,
        {
            "summary": {
                "total": len(findings),
                "errors": counts["ERR"],
                "warnings": counts["WARN"],
                "info": counts["INFO"],
            },
            "findings": findings,
        },
    ):
        if counts["ERR"] > 0:
            sys.exit(1)
        return

    # Human-readable output
    scope = f" ({node_id})" if node_id else ""
    print(f"BA Lint{scope}")
    print(f"{'═' * 50}")

    if not findings:
        print("\n✓ No issues found")
        return

    # Group by category
    by_cat: dict = {}
    for f in findings:
        by_cat.setdefault(f["category"], []).append(f)

    cat_order = ["TODO_TBD", "EMPTY_SECTION", "TERMINOLOGY", "STALE", "CODE_COVERAGE"]
    cat_labels = {
        "TODO_TBD": "Incompleteness markers",
        "EMPTY_SECTION": "Empty sections",
        "TERMINOLOGY": "Terminology vs glossary",
        "STALE": "Stale artifacts",
        "CODE_COVERAGE": "Code coverage",
    }

    for cat in cat_order:
        items = by_cat.get(cat, [])
        if not items:
            continue
        label = cat_labels.get(cat, cat)
        print(f"\n── {label} ({len(items)}) ──")
        for f in items:
            loc = f["file"]
            if f["line"]:
                loc += f":{f['line']}"
            sev = f["severity"]
            print(f"  [{sev:4s}]  {f['artifact_id']:12s}  {loc}")
            print(f"          {f['message']}")

    # Summary
    print(f"\n{'─' * 50}")
    parts = []
    if counts["ERR"]:
        parts.append(f"{counts['ERR']} ERR")
    if counts["WARN"]:
        parts.append(f"{counts['WARN']} WARN")
    if counts["INFO"]:
        parts.append(f"{counts['INFO']} INFO")
    print(f"Lint: {', '.join(parts)}  (total {len(findings)})")

    if counts["ERR"] > 0:
        sys.exit(1)

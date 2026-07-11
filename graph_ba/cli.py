"""Click command line interface for graph-ba."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import click

from graph_ba.audit import run_anomalies, run_coverage
from graph_ba.change_workflow import ChangeWorkflowError, ChangeWorkflowService
from graph_ba.db import _load_nx, do_import
from graph_ba.review import run_validate

from .artifact_state import _artifact_state_payload
from .cli_core import (
    _change_manifest_path,
    _conn,
    _json_out,
    _read_change_manifest,
    _require_graph,
    change_group,
    cli,
    fmt_table,
)
from .gate_analysis import _change_payload, _pack_payload
from .gates import _gate_payload, _graph_slice_payload
from .rendering import (
    _render_evidence_plan_markdown,
    _render_graph_summary,
    _render_pack_markdown,
)


# ── Schema ────────────────────────────────────────────────────────

# ── CLI ───────────────────────────────────────────────────────────


@cli.command("import")
@click.option(
    "--force", is_flag=True, help="Force full rebuild even if files are unchanged"
)
@click.pass_context
def cmd_import(ctx, force):
    """Scan artifacts and populate the SQLite DB."""
    root = Path(ctx.obj.get("root", "."))
    db = _conn(ctx)
    do_import(root, db, force=force)
    db.close()


@cli.command("init")
@click.pass_context
def cmd_init(ctx):
    """Create a template graph-ba.toml in the project root."""
    from graph_ba.config import CONFIG_FILENAME

    root = Path(ctx.obj.get("root", "."))
    config_path = root / CONFIG_FILENAME
    if config_path.exists():
        print(f"Config already exists: {config_path}")
        return

    template = """\
# graph-ba.toml — project configuration for graph-ba
# Defines artifact types, scan rules, and cross-reference patterns.

[scan]
# Directories to scan for .md files (relative to project root)
dirs = ["docs"]

# ID normalization rules
[normalize]
# Character replacements (e.g. Cyrillic → Latin)
char_map = {}
# Zero-padding rules: { pattern = "regex with group(1)=number", format = "python format string" }
zero_pad = []

# Range expansion pattern (for references like REQ.1.1–REQ.1.5)
range_pattern = '((?:REQ|FUNC)\\.\\d+\\.)(\\d+)\\s*[–\\-]\\s*(?:(?:REQ|FUNC)\\.\\d+\\.)(\\d+)'

# ── Artifact origin enum ──
# graph-ba ships defaults: human, derived, canonical, evidence,
# implementation, container, unknown. Projects can override labels or add
# stricter provenance classes.
# [origins.human]
# label = "Human primary source"
# description = "Client, stakeholder, refined meeting or human dictation input."
#
# [origins.reviewed_derived]
# label = "Reviewed derived artifact"
# description = "Agent or analyst output reviewed by a human analyst."

# ── Edge relation enum ──
# graph-ba emits weak MENTIONS/INDEX plus semantic IMPLEMENTS/VERIFIES/RENDERS.
# It also suggests semantic relation names for agent-first traceability:
# DERIVES_FROM, NORMALIZES, IMPLEMENTS, VERIFIES, RENDERS, CONFLICTS_WITH,
# SUPERSEDES, TRACE_GAP. Projects can add their own relation types.
# [relations.NORMALIZES]
# label = "Normalizes"
# description = "Canonical AC normalizes raw source material."
# direction = "canonical_to_source"

# ── Artifact types ──
# Each type needs:
#   ref = regex to find references in text (group 1 = full ID)
#   classify = regex to classify an ID string (used with fullmatch)
#   label = human-readable name
#   origin = optional provenance class (human, derived, evidence, implementation, ...)
#   restrict_to = optional list of files/dirs where this pattern is allowed

[types.REQ]
label = "Requirements"
origin = "derived"
ref = '(?<![A-Za-z])(REQ-\\d{2,4})(?!\\d)'
classify = 'REQ-\\d{2,4}'

[types.FEAT]
label = "Features"
origin = "derived"
ref = '(?<![A-Za-z])(FEAT-\\d{2,4})(?!\\d)'
classify = 'FEAT-\\d{2,4}'

# ── Definition scan rules ──
# type = artifact type ID
# file = relative path (supports * glob patterns)
# mode = "heading" (match heading lines) or "table" (match table rows)
# pattern = regex (group 1 = ID, group 2 = title)

[[definitions]]
type = "REQ"
file = "docs/requirements.md"
mode = "table"
pattern = '^\\|\\s*(REQ-\\d{2,4})\\s*\\|'

[[definitions]]
type = "FEAT"
file = "docs/features.md"
mode = "heading"
pattern = '^##\\s+(FEAT-\\d{2,4})\\s*[—–\\-]\\s*(.*)'

# ── Index tables (extract cross-refs from table rows) ──
# file = path to index file
# first_col = regex matching the source ID in the first column

# [[index_tables]]
# file = "docs/features.md"
# first_col = '^\\|\\s*(FEAT-\\d{2,4})\\s*\\|'

# ── Coverage expectations ──
# source/target = type IDs, label = display name

# [[coverage]]
# source = "FEAT"
# target = "REQ"
# label = "FEAT → REQ"

# ── Review validation ──
[review]
# Required sections in artifacts of a given type
# required_sections = { "FEAT" = ["Goal", "Scope"] }

# Expected bidirectional links
# expected_bidir = { "FEAT" = ["REQ"] }

# Expected cross-layer links for review
# [[review.expected_cross_layer.FEAT]]
# type = "REQ"
# label = "requirements"

# ── Semantic clusters ──
[clusters]
# "Topic Name" = ["ID-01", "ID-02"]

# ── Code traceability ──
# Scan source files for @trace comments referencing artifacts.
# Example: // @trace: REQ-01, FEAT-02
# [code]
# dirs = ["src"]
# extensions = ["ts", "tsx", "py", "go"]
# marker = "@trace"
# coverage_types = ["FEAT", "REQ"]

# Optional: resolve each @trace location to its enclosing CodeGraph symbol.
# Falls back to the file-level CODE: node when the index or symbol is unavailable.
# [providers.codegraph]
# database = ".codegraph/codegraph.db"

# ── Test traceability ──
# Test files become TEST: nodes; any artifact ID found in a test file
# counts as test evidence (no marker needed).
# [tests]
# dirs = ["tests"]
# extensions = ["py", "ts", "tsx", "js", "dart"]
# coverage_types = ["REQ"]

# ── UI traceability ──
# UI trace sidecars (e.g. trace.json mapping data-testid -> AC IDs) become
# UI: nodes; any artifact ID found in them counts as a UI link.
# [ui]
# files = ["app/src/features/*/api/trace.json"]
# coverage_types = ["REQ"]
"""
    config_path.write_text(template, encoding="utf-8")
    print(f"Created template config: {config_path}")
    print("Edit it to match your project's artifact naming conventions.")


@cli.command()
@click.argument("node_id")
@click.option("--depth", default=10, help="Max traversal depth")
@click.pass_context
def impact(ctx, node_id, depth):
    """Cascade impact analysis: what does changing this artifact affect?"""
    import networkx as nx

    db = _conn(ctx)
    _require_graph(ctx, db)
    G = _load_nx(db)
    db.close()

    if node_id not in G:
        if _json_out(ctx, {"error": f"Node '{node_id}' not found", "node": node_id}):
            return
        print(f"Node '{node_id}' not found")
        return

    # BFS from node, follow outgoing edges
    reachable = nx.descendants(G, node_id)

    # Group descendants by type
    descendants_by_type: dict = {}
    for nid in reachable:
        t = G.nodes[nid].get("type", "?")
        descendants_by_type.setdefault(t, []).append(nid)

    # Reverse: what affects this node?
    ancestors = nx.ancestors(G, node_id)
    ancestors_by_type: dict = {}
    for nid in ancestors:
        t = G.nodes[nid].get("type", "?")
        ancestors_by_type.setdefault(t, []).append(nid)

    # JSON output
    if _json_out(
        ctx,
        {
            "node": node_id,
            "type": G.nodes[node_id].get("type", "?"),
            "descendants": {
                "total": len(reachable),
                "by_type": {
                    t: sorted(ids) for t, ids in sorted(descendants_by_type.items())
                },
            },
            "ancestors": {
                "total": len(ancestors),
                "by_type": {
                    t: sorted(ids) for t, ids in sorted(ancestors_by_type.items())
                },
            },
        },
    ):
        return

    # Text output
    if not reachable:
        print(f"{node_id}: no cascade impact (no outgoing paths)")
        return

    print(f"Cascade impact {node_id}: {len(reachable)} artifacts")
    print()
    for t in sorted(descendants_by_type):
        ids = sorted(descendants_by_type[t])
        print(f"  [{t}] ({len(ids)}): {', '.join(ids[:15])}")
        if len(ids) > 15:
            print(f"         ... and {len(ids) - 15} more")

    if ancestors:
        print(f"\nReverse impact (what affects {node_id}): {len(ancestors)} artifacts")
        for t in sorted(ancestors_by_type):
            ids = sorted(ancestors_by_type[t])
            print(f"  [{t}] ({len(ids)}): {', '.join(ids[:15])}")
            if len(ids) > 15:
                print(f"         ... and {len(ids) - 15} more")


@cli.command("sql")
@click.argument("query")
@click.pass_context
def raw_sql(ctx, query):
    """Execute raw SQL query."""
    db = _conn(ctx)
    try:
        rows = db.execute(query).fetchall()
        if not rows:
            print("(empty)")
            db.close()
            return
        headers = rows[0].keys()
        print(fmt_table([tuple(r) for r in rows], list(headers)))
    except sqlite3.Error as e:
        print(f"SQL error: {e}", file=sys.stderr)
    db.close()


@cli.command()
@click.pass_context
def coverage(ctx):
    """Show cross-layer coverage matrix."""
    from graph_ba.config import load_config

    root = Path(ctx.obj.get("root", ".")).resolve()
    config = load_config(root)

    db = _conn(ctx)
    _require_graph(ctx, db)
    has_any = (
        config.coverage_pairs
        or (config.code and config.code.coverage_types)
        or (config.tests and config.tests.coverage_types)
        or (config.ui and config.ui.coverage_types)
    )
    if not has_any:
        print("No coverage pairs defined in graph-ba.toml [coverage]")
        db.close()
        return
    data = run_coverage(db, config)
    db.close()

    if _json_out(ctx, data):
        return

    if data["pairs"]:
        print("Cross-layer coverage matrix:")
        print()
        for r in data["pairs"]:
            bar = "█" * int(r["pct"] / 5) + "░" * (20 - int(r["pct"] / 5))
            print(
                f"  {r['source']:8s} ↔ {r['target']:8s}  {r['linked']:3d}/{r['total']:<3d}  "
                f"{bar}  {r['pct']:5.1f}%  [{r['status']}]"
            )

    if data["code_coverage"]:
        print("\nCode reference coverage:")
        print()
        for r in data["code_coverage"]:
            bar = "█" * int(r["pct"] / 5) + "░" * (20 - int(r["pct"] / 5))
            print(
                f"  CODE → {r['type']:8s}  {r['linked']:3d}/{r['total']:<3d}  "
                f"{bar}  {r['pct']:5.1f}%  [{r['status']}]"
            )

    if data["test_coverage"]:
        print("\nTest coverage:")
        print()
        for r in data["test_coverage"]:
            bar = "█" * int(r["pct"] / 5) + "░" * (20 - int(r["pct"] / 5))
            print(
                f"  TEST → {r['type']:8s}  {r['linked']:3d}/{r['total']:<3d}  "
                f"{bar}  {r['pct']:5.1f}%  [{r['status']}]"
            )

    if data["ui_coverage"]:
        print("\nUI trace coverage:")
        print()
        for r in data["ui_coverage"]:
            bar = "█" * int(r["pct"] / 5) + "░" * (20 - int(r["pct"] / 5))
            print(
                f"  UI   → {r['type']:8s}  {r['linked']:3d}/{r['total']:<3d}  "
                f"{bar}  {r['pct']:5.1f}%  [{r['status']}]"
            )


@cli.command("artifact-state")
@click.argument("artifact_id", required=False, default=None)
@click.option(
    "--snapshot",
    "snapshot_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Accepted fingerprint snapshot JSON to compare against",
)
@click.option(
    "--write-snapshot",
    "write_snapshot_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write current fingerprints as accepted snapshot JSON",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write artifact state JSON to this file",
)
@click.pass_context
def artifact_state(ctx, artifact_id, snapshot_path, write_snapshot_path, out_path):
    """Compute lifecycle, fingerprints and implementation/evidence state."""
    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()
    payload = _artifact_state_payload(db, root, artifact_id, snapshot_path)
    db.close()

    if write_snapshot_path:
        snapshot = {
            "schema": "graph-ba.fingerprint-snapshot.v1",
            "artifacts": {
                item["id"]: {
                    "lifecycle": item["lifecycle"],
                    "fingerprints": item["fingerprints"],
                }
                for item in payload["artifacts"]
            },
        }
        write_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        write_snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        payload["snapshot_written"] = str(write_snapshot_path)

    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote artifact state JSON: {out_path}")
            if write_snapshot_path:
                print(f"Wrote accepted fingerprint snapshot: {write_snapshot_path}")
            return
    print(text)


@change_group.command("show")
@click.argument("change_id")
@click.pass_context
def change_show(ctx, change_id):
    """Show change metadata, scope and computed state."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    data = _change_payload(db, root, change_id)
    db.close()
    if _json_out(ctx, data):
        return
    meta = data["change"]
    print(f"{meta['id']} — {meta.get('title') or ''}")
    print(f"state={meta.get('state') or 'draft'} mode={meta.get('mode') or ''}")
    print(f"scope: {len(data['scope'])}")
    for item in data["scope"]:
        flags = item["computed"]
        print(
            f"  {item['id']} [{item['type']}] "
            f"implemented={flags['implemented']} verified={flags['verified']} stale={flags['stale']}"
        )


@change_group.command("diff")
@click.argument("change_id")
@click.pass_context
def change_diff(ctx, change_id):
    """Show the stable-ID contract delta against the Git base."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        payload = ChangeWorkflowService(root).diff(change_id)
    except ChangeWorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@change_group.command("discover")
@click.argument("change_id")
@click.argument("query", required=False, default="")
@click.option("--limit", default=20, type=click.IntRange(1, 100))
@click.pass_context
def change_discover(ctx, change_id, query, limit):
    """Find likely contract and source artifacts for a change intent."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    service = ChangeWorkflowService(root, db)
    try:
        manifest = service.manifest(change_id)
        payload = service.discover(
            query or str(manifest.get("intent") or ""),
            limit=limit,
            seed_ids=[*manifest.get("sources", []), *manifest.get("scope", [])],
        )
    except ChangeWorkflowError as exc:
        db.close()
        raise click.ClickException(str(exc)) from exc
    db.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@change_group.command("status")
@click.argument("change_id")
@click.pass_context
def change_status(ctx, change_id):
    """Show Git lifecycle, proposal fingerprint and approval state."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        payload = ChangeWorkflowService(root).status(change_id)
    except ChangeWorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@cli.command("gate")
@click.argument("target_id")
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
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write gate JSON to this file",
)
@click.pass_context
def gate(ctx, target_id, mode, snapshot_path, out_path):
    """Evaluate change/release readiness from graph facts."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    result = _gate_payload(db, root, target_id, mode, snapshot_path)
    db.close()
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote gate JSON: {out_path}")
            if not result["pass"]:
                raise click.ClickException(f"Gate failed: {result['verdict']}")
            return
    print(text)
    if not result["pass"]:
        raise click.ClickException(f"Gate failed: {result['verdict']}")


@cli.command("pack")
@click.argument("target_id")
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write pack markdown to this file",
)
@click.option(
    "--format", "output_format", type=click.Choice(["md", "json"]), default="md"
)
@click.pass_context
def pack(ctx, target_id, out_path, output_format):
    """Compile an agent pack for a change, screen family, screen or artifact."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    payload = _pack_payload(db, root, target_id)
    db.close()
    if output_format == "json":
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        text = _render_pack_markdown(payload)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text.rstrip() + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote graph-ba pack: {out_path}")
            return
    print(text)


@cli.command("graph")
@click.argument("target_id")
@click.option(
    "--mode",
    default=None,
    type=click.Choice(["explore", "dev", "review", "release"]),
    help="Gate strictness for findings; defaults to change.yaml mode or dev",
)
@click.option(
    "--snapshot",
    "snapshot_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Accepted fingerprint snapshot for stale checks",
)
@click.option(
    "--content",
    "content_mode",
    type=click.Choice(["none", "excerpt", "full"]),
    default="excerpt",
    help="How much artifact content to include",
)
@click.option(
    "--content-limit",
    default=1200,
    show_default=True,
    help="Maximum characters per content excerpt",
)
@click.option(
    "--include-mentions",
    is_flag=True,
    help="Alias for --view navigation; include bounded weak navigation context",
)
@click.option(
    "--view",
    type=click.Choice(["contract", "delivery", "navigation", "full"]),
    default="delivery",
    show_default=True,
    help="Project the full knowledge graph for contract, delivery, navigation or full exploration",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write graph slice JSON to this file",
)
@click.option(
    "--summary",
    is_flag=True,
    help="Print a concise readiness/worklist summary instead of full JSON",
)
@click.pass_context
def graph_slice(
    ctx,
    target_id,
    mode,
    snapshot_path,
    content_mode,
    content_limit,
    include_mentions,
    view,
    out_path,
    summary,
):
    """Export scoped nodes, typed edges, content excerpts and findings as JSON."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    payload = _graph_slice_payload(
        db,
        root,
        target_id,
        mode,
        snapshot_path,
        content_mode,
        content_limit,
        include_mentions,
        view=view,
    )
    db.close()
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text.rstrip() + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote graph-ba graph slice: {out_path}")
            return
    if summary and not ctx.obj.get("json"):
        print(_render_graph_summary(payload), end="")
    else:
        print(text)


@cli.command("evidence-plan")
@click.argument("target_id")
@click.option(
    "--mode",
    default=None,
    type=click.Choice(["explore", "dev", "review", "release"]),
    help="Gate strictness for evidence policy findings; defaults to change.yaml mode or dev",
)
@click.option(
    "--snapshot",
    "snapshot_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Accepted fingerprint snapshot for stale checks",
)
@click.option(
    "--format", "output_format", type=click.Choice(["json", "md"]), default="json"
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write evidence plan to this file",
)
@click.pass_context
def evidence_plan(ctx, target_id, mode, snapshot_path, output_format, out_path):
    """Explain which evidence kinds each scoped AC requires and why."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    payload = _gate_payload(db, root, target_id, mode, snapshot_path)["evidence_plan"]
    db.close()
    if output_format == "md":
        text = _render_evidence_plan_markdown(payload)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text.rstrip() + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote graph-ba evidence plan: {out_path}")
            return
    print(text)


# ── Code references CLI ──────────────────────────────────────────


@cli.command("code-refs")
@click.option(
    "--by-artifact", is_flag=True, help="Group by artifact instead of by file"
)
@click.option(
    "--type", "art_type", default=None, help="Filter to artifact type (e.g. F, BR_REQ)"
)
@click.pass_context
def code_refs(ctx, by_artifact, art_type):
    """Show code-to-artifact traceability links."""
    db = _conn(ctx)
    _require_graph(ctx, db)

    query = (
        "SELECT e.source_id as code_node, e.target_id as artifact_id, "
        "a.type as art_type, a.title, e.relation_type, "
        "e.source_file, e.line_number, e.context, "
        "c.title as code_title, c.source_file as code_file "
        "FROM edges e "
        "LEFT JOIN artifacts a ON e.target_id = a.id "
        "LEFT JOIN artifacts c ON e.source_id = c.id "
        "WHERE e.source_id LIKE 'CODE:%'"
    )
    params: list = []
    if art_type:
        query += " AND a.type = ?"
        params.append(art_type)
    query += " ORDER BY e.source_id, e.line_number"

    rows = db.execute(query, params).fetchall()
    db.close()

    if not rows:
        print(
            "No code references found. Add @trace comments to source files "
            "and configure [code] in graph-ba.toml."
        )
        return

    if _json_out(ctx, {"code_refs": [dict(r) for r in rows]}):
        return

    if by_artifact:
        groups: dict = {}
        for r in rows:
            groups.setdefault(r["artifact_id"], []).append(r)

        print(f"Code references by artifact ({len(groups)} artifacts):\n")
        for aid in sorted(groups):
            refs = groups[aid]
            art_type_str = refs[0]["art_type"] or "?"
            title = refs[0]["title"] or ""
            print(f"  [{art_type_str}] {aid} — {title[:50]}")
            for r in refs:
                code_source = r["code_file"] or r["source_file"]
                code_label = r["code_title"] or code_source
                print(f"    {code_label} — {code_source}:{r['line_number']}")
            print()
    else:
        groups = {}
        for r in rows:
            groups.setdefault(r["code_node"], []).append(r)

        print(f"Code references by source ({len(groups)} sources):\n")
        for code_node in sorted(groups):
            refs = groups[code_node]
            code_source = refs[0]["code_file"] or refs[0]["source_file"]
            code_label = refs[0]["code_title"] or code_source
            suffix = f" ({code_source})" if code_label != code_source else ""
            print(f"  {code_label}{suffix}")
            for r in refs:
                print(
                    f"    L{r['line_number']:>4d}  → [{r['art_type'] or '?'}] "
                    f"{r['artifact_id']} — {(r['title'] or '')[:40]}"
                )
            print()


# ── NetworkX loader (for path/impact commands) ────────────────────


@cli.command()
@click.argument("artifact_id")
@click.pass_context
def validate(ctx, artifact_id):
    """Deterministic quality gate for one artifact (exit 0=PASS, 1=FAIL)."""
    from graph_ba.config import load_config

    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        config = load_config(root)
    except FileNotFoundError:
        config = None
    data = run_validate(db, root, config, artifact_id)
    db.close()

    if not _json_out(ctx, data):
        symbols = {"pass": "✓", "fail": "✗", "warn": "⚠"}
        print(f"Validate: {artifact_id}")
        for c in data["checks"]:
            print(f"  {symbols[c['status']]} {c['name']} — {c['detail']}")
        print(f"\nVERDICT: {data['verdict']}")
    if data["verdict"] == "FAIL":
        ctx.exit(1)


# ── Anomaly detection ─────────────────────────────────────────────


@cli.command()
@click.option("--min-component", default=2, help="Min size of islands to report")
@click.pass_context
def anomalies(ctx, min_component):
    """Detect graph anomalies: islands, cycles, weak nodes, broken chains."""
    db = _conn(ctx)
    _require_graph(ctx, db)
    data = run_anomalies(db, min_component)
    db.close()

    if _json_out(ctx, data):
        return

    issues = [(i["type"], i["message"]) for i in data["issues"]]
    if not issues:
        print("No anomalies found.")
        return

    print(f"Graph anomalies ({data['nodes']} nodes, {data['edges']} edges):")
    print()
    current_cat = None
    for cat, msg in issues:
        if cat != current_cat:
            current_cat = cat
            print(f"── {cat} ──")
        print(f"  {msg}")
    print()
    total = sum(1 for _, msg in issues if not msg.startswith("  "))
    print(f"Total: {total} anomaly types detected")


# ── Global audit ──────────────────────────────────────────────────

# ── Lint command ──────────────────────────────────────────────────

_TODO_DEFAULT = re.compile(r"(?:TODO|TBD|FIXME|\?\?\?)", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)")
_GLOSSARY_ROW_RE = re.compile(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|")
_CODE_FENCE_RE = re.compile(r"^```")

# Register commands split out of this composition root after `cli` and the
# shared change group have been fully defined.
from . import cli_review as _cli_review  # noqa: E402,F401


if __name__ == "__main__":
    cli()

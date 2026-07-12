"""Human-readable renderers for graph-ba change and evidence outputs."""

from __future__ import annotations

from typing import Any


def _render_diff_review_summary(payload: dict[str, Any], *, item_limit: int = 20) -> str:
    semantic = payload["semantic"]
    git = semantic["git"]
    summary = semantic["summary"]
    graph_summary = payload["graph_delta"]["summary"]
    contract = semantic.get("contract", [])
    operation_counts = {
        operation: sum(item["operation"] == operation for item in contract)
        for operation in ("add", "modify", "remove")
    }
    lines = [
        f"graph-ba diff {git['base_ref']}...worktree",
        (
            f"Git: {summary['files']} files — contract={summary['contract_files']} "
            f"supporting={summary['supporting_files']} delivery={summary['delivery_files']}"
        ),
        (
            f"Semantic: {summary['contract']} canonical — "
            f"+{operation_counts['add']} ~{operation_counts['modify']} "
            f"-{operation_counts['remove']}"
        ),
        (
            f"Graph: nodes +{graph_summary['nodes_added']} ~{graph_summary['nodes_modified']} "
            f"-{graph_summary['nodes_removed']}; edges +{graph_summary['edges_added']} "
            f"-{graph_summary['edges_removed']}"
        ),
    ]

    scope = payload.get("scope")
    if scope:
        before = _short_readiness(scope["base"])
        after = _short_readiness(scope["proposed"])
        scoped = scope["semantic"]
        gaps = scope["gaps"]
        lines.extend(
            [
                "",
                f"Scope: {scope['target']} — {len(scoped['in_scope'])} changed in scope, "
                f"{len(scoped['outside_scope'])} outside",
                f"Readiness: {before} -> {after}",
                (
                    f"Gaps: +{len(gaps['introduced'])} introduced, "
                    f"-{len(gaps['resolved'])} resolved, "
                    f"={len(gaps['persistent'])} persistent"
                ),
            ]
        )
        for change in scope.get("quality_axis_changes", []):
            lines.append(
                f"- axis {change['axis']}: {change['before']} -> {change['after']}"
            )

    displayed = scope["semantic"]["in_scope"] if scope else contract
    heading = "In-scope canonical artifacts" if scope else "Canonical artifacts"
    lines.extend(["", f"{heading}: {len(displayed)}"])
    symbols = {"add": "+", "modify": "~", "remove": "-"}
    for item in displayed[:item_limit]:
        current = item.get("after") or item.get("before") or {}
        title = current.get("title") or ""
        suffix = f" — {title}" if title else ""
        lines.append(
            f"- {symbols.get(item['operation'], '?')} {item['id']} [{item['type']}]{suffix}"
        )
    if len(displayed) > item_limit:
        lines.append(f"- … {len(displayed) - item_limit} more; use --json")

    if scope:
        _append_gap_items(
            lines, "Introduced gaps", scope["gaps"]["introduced"], item_limit
        )
        _append_gap_items(
            lines, "Resolved gaps", scope["gaps"]["resolved"], item_limit
        )
        current_worklist = [
            item
            for item in scope["gaps"]["persistent"] + scope["gaps"]["introduced"]
            if item["source"] == "worklist"
        ]
        _append_gap_items(lines, "Current worklist", current_worklist, item_limit)
    return "\n".join(lines) + "\n"


def _short_readiness(value: dict[str, Any]) -> str:
    if not value.get("present"):
        return "ABSENT"
    return f"{value['verdict']}/{value['readiness']}"


def _append_gap_items(
    lines: list[str],
    title: str,
    items: list[dict[str, Any]],
    limit: int,
) -> None:
    lines.extend(["", f"{title}: {len(items)}"])
    for item in items[:limit]:
        lines.append(
            f"- {item.get('priority') or item.get('severity') or '-'} "
            f"{item['kind']} {item['artifact']}: {item['reason']}"
        )
    if len(items) > limit:
        lines.append(f"- … {len(items) - limit} more; use --json")


def _render_graph_summary(payload: dict[str, Any], *, worklist_limit: int = 12) -> str:
    summary = payload["summary"]
    scope_parts = [f"scope={summary['scope']}"]
    if "nodes" in summary:
        scope_parts.append(f"nodes={summary['nodes']}")
    if "edges" in summary:
        scope_parts.append(f"edges={summary['edges']}")
    scope_parts.append(f"evidence_gaps={summary['evidence_plan']['gap']}")
    if "view" in summary:
        scope_parts.append(f"view={summary['view']}")
    lines = [
        f"graph-ba {payload['target']}",
        (
            f"{payload['verdict']} readiness={payload.get('readiness', 'UNKNOWN')} "
            f"confidence={payload['overall_confidence']} mode={payload['mode']}"
        ),
        " ".join(scope_parts),
        "",
        "Quality axes:",
    ]
    for name, axis in payload.get("quality_axes", {}).items():
        lines.append(f"- {name}: {axis['status']} — {axis['reason']}")
        missing = axis.get("missing") or []
        if missing:
            lines.append(f"  missing: {', '.join(missing)}")
    worklist = payload.get("agent_worklist", [])
    lines.extend(["", f"Worklist: {len(worklist)}"])
    for item in worklist[:worklist_limit]:
        lines.append(
            f"- {item['priority']} {item['kind']} {item['artifact']}: {item['reason']}"
        )
    if len(worklist) > worklist_limit:
        lines.append(
            f"- … {len(worklist) - worklist_limit} more; use --worklist-only"
        )
    return "\n".join(lines) + "\n"


def _render_change_ready_summary(payload: dict[str, Any]) -> str:
    proposal = payload["proposal"]
    approval = payload["approval"]
    delivery = payload["delivery"]
    provider = payload["provider_refresh"]
    lines = [
        f"graph-ba change ready {payload['change']}",
        f"Provider refresh: {'PASS' if provider.get('pass', True) else 'FAIL'}"
        + (" (refreshed)" if provider.get("refreshed") else ""),
        f"Import: {payload['import']['status']} — {payload['import']['artifacts']} artifacts, {payload['import']['edges']} edges",
        f"Proposal: {proposal['verdict']} — fingerprint {proposal.get('proposal_fingerprint', '')}",
        (
            "Approval: PASS"
            if approval.get("valid")
            else f"Approval: REQUIRED — {approval.get('reason', 'missing_approval')}"
        ),
        (
            f"Delivery: {delivery['verdict']} — "
            f"{payload['delivery_evidence_gaps']} evidence gaps, "
            f"{delivery.get('summary', {}).get('findings', 0)} findings"
        ),
    ]
    codegraph = payload.get("codegraph", {})
    if codegraph.get("status") == "missing":
        lines.append(
            "CodeGraph: MISSING — using file-level code traces; "
            + str(codegraph.get("suggested_action") or "restore the index")
        )
    next_action = payload.get("next_action", {})
    if next_action:
        lines.extend(["", f"Next: {next_action['reason']}", next_action["command"]])
    lines.extend(["", f"Outputs: {payload['outputs']}"])
    return "\n".join(lines) + "\n"


def _render_pack_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# graph-ba pack: {payload['target']}", ""]
    lines.append("## Artifacts")
    for item in payload["artifacts"]:
        content = _pack_markdown_content(str(item["type"]), str(item["content"]))
        lines.extend(
            [
                "",
                f"### {item['id']} [{item['type']}]",
                f"Title: {item['title']}",
                f"Source: {item['source_file']}:{item['line_number']}",
                "",
                "```",
                content,
                "```",
            ]
        )
    lines.extend(["", "## Edges", ""])
    for edge in payload["edges"]:
        lines.append(
            f"- `{edge['source_id']}` --{edge['relation_type']}--> `{edge['target_id']}`"
        )
    return "\n".join(lines)


def _pack_markdown_content(artifact_type: str, content: str) -> str:
    content = content.strip()
    limit = 1200 if artifact_type in {"TEST", "CODE", "REACT_COMPONENT"} else 3500
    if len(content) <= limit:
        return content
    return (
        content[:limit].rstrip()
        + f"\n\n[graph-ba pack truncated {len(content) - limit} chars; use source file for full content]"
    )


def _render_change_state_yaml(payload: dict[str, Any]) -> str:
    lines = [
        "schema: graph-ba.change-state.v1",
        f"id: {payload['change']['id']}",
        f"state: {payload['change'].get('state') or 'draft'}",
        f"mode: {payload['change'].get('mode') or 'dev'}",
        "scope:",
    ]
    for item in payload.get("scope", []):
        computed = item.get("computed", {})
        lines.extend(
            [
                f"  - id: {item['id']}",
                f"    type: {item['type']}",
                f"    lifecycle: {item['lifecycle']}",
                f"    implemented: {str(computed.get('implemented', False)).lower()}",
                f"    verified: {str(computed.get('verified', False)).lower()}",
                f"    stale: {str(computed.get('stale', False)).lower()}",
            ]
        )
    return "\n".join(lines) + "\n"


def _render_gaps_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# graph-ba gaps: {payload['target']}",
        "",
        f"Mode: `{payload['mode']}`",
        f"Verdict: `{payload['verdict']}`",
        f"Summary: fail={payload['summary']['fail']} warn={payload['summary']['warn']} scope={payload['summary']['scope']}",
        "",
    ]
    findings = payload.get("findings", [])
    if not findings:
        lines.append("No gaps.")
        return "\n".join(lines) + "\n"
    for finding in findings:
        lines.extend(
            [
                f"## {finding.get('gap_type', 'GAP-SPEC')} {finding['artifact']}",
                "",
                f"- code: `{finding['code']}`",
                f"- severity: `{finding['severity']}`",
                f"- blocking: `{str(finding.get('blocking', False)).lower()}`",
                f"- message: {finding['message']}",
            ]
        )
        suggestions = finding.get("suggested_fix") or []
        if suggestions:
            lines.append("- suggested_fix:")
            lines.extend(f"  - {item}" for item in suggestions)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_worklist_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# graph-ba worklist: {payload['target']}",
        "",
        f"Mode: `{payload['mode']}`",
        "",
    ]
    items = payload.get("agent_worklist", [])
    if not items:
        lines.append("No worklist items.")
        return "\n".join(lines) + "\n"
    for item in items:
        lines.extend(
            [
                f"## {item['priority']} {item['kind']} {item['artifact']}",
                "",
                f"- artifact_type: `{item.get('artifact_type', '')}`",
                f"- reason: {item['reason']}",
                f"- blocking_in: {', '.join(item.get('blocking_in', [])) or 'none'}",
            ]
        )
        related = item.get("related_nodes") or []
        if related:
            lines.append(
                f"- related_nodes: {', '.join(f'`{node}`' for node in related)}"
            )
        actions = item.get("suggested_actions") or []
        if actions:
            lines.append("- suggested_actions:")
            lines.extend(f"  - {action}" for action in actions)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_evidence_plan_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# graph-ba evidence plan",
        "",
        f"AC: {summary.get('ac_total', 0)}  OK: {summary.get('ok', 0)}  GAP: {summary.get('gap', 0)}",
        "",
    ]
    providers = payload.get("policy", {}).get("providers", [])
    if providers:
        lines.extend(
            [
                f"Policy: {', '.join(f'`{provider}`' for provider in providers)}",
                "",
            ]
        )
    items = payload.get("items", [])
    if not items:
        lines.append("No AC artifacts in scope.")
        return "\n".join(lines) + "\n"
    for item in items:
        missing = item.get("missing_required_evidence") or []
        status = "GAP" if missing else "OK"
        lines.extend(
            [
                f"## {status} {item['artifact']}",
                "",
                f"- kinds: {', '.join(f'`{kind}`' for kind in item.get('kinds', [])) or 'none'}",
                f"- required_evidence: {', '.join(f'`{kind}`' for kind in item.get('required_evidence', [])) or 'none'}",
                f"- observed_evidence: {', '.join(f'`{kind}`' for kind in item.get('observed_kinds', [])) or 'none'}",
                f"- reason: {item.get('reason', '')}",
            ]
        )
        if missing:
            lines.append(f"- missing: {', '.join(f'`{kind}`' for kind in missing)}")
            lines.append("- suggested_fix:")
            lines.extend(
                f"  - {action}"
                for action in _required_evidence_suggested_fix(
                    item["artifact"], missing
                )
            )
        observed = item.get("observed_evidence") or []
        if observed:
            lines.append("- evidence_sources:")
            for evidence in observed[:8]:
                lines.append(
                    f"  - `{evidence.get('kind', '')}` `{evidence.get('source', '')}` "
                    f"({evidence.get('source_file', '')})"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_projection_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# graph-ba projection: {payload['target']}",
        "",
        f"Mode: `{payload['mode']}`",
        f"Nodes: {payload['summary']['nodes']}",
        f"Edges: {payload['summary']['edges']}",
        "",
        "## Scope",
        "",
    ]
    for node in payload.get("nodes", []):
        computed = node.get("computed", {})
        flags = []
        if computed:
            flags.append(
                f"implemented={str(computed.get('implemented', False)).lower()}"
            )
            flags.append(f"verified={str(computed.get('verified', False)).lower()}")
            flags.append(f"stale={str(computed.get('stale', False)).lower()}")
        lines.append(
            f"- `{node['id']}` [{node['type']}] {node.get('title') or ''} {' '.join(flags)}".rstrip()
        )
        proofs = node.get("implementation_proofs") or []
        for proof in proofs[:2]:
            path = " -> ".join(
                f"{edge['from']} -{edge['relation']}-> {edge['to']}"
                for edge in proof.get("path", [])
            )
            lines.append(f"  - proof: {path or proof.get('reason', '')}")
    lines.extend(["", "## Findings", ""])
    findings = payload.get("findings", [])
    if not findings:
        lines.append("No findings.")
    else:
        for finding in findings:
            lines.append(
                f"- `{finding.get('gap_type', 'GAP-SPEC')}` `{finding['artifact']}` "
                f"{finding['severity']}: {finding['message']}"
            )
    return "\n".join(lines).rstrip() + "\n"


def _required_evidence_suggested_fix(artifact: str, missing: list[str]) -> list[str]:
    actions = []
    for kind in missing:
        if kind == "unit":
            actions.append(f"add a unit test for deterministic logic behind {artifact}")
        elif kind == "backend_integration":
            actions.append(
                f"add a backend integration test for runtime/database/service behavior behind {artifact}"
            )
        elif kind == "frontend_unit":
            actions.append(f"add a frontend model/mapper/selector test for {artifact}")
        elif kind == "e2e_ui":
            actions.append(
                f"add or link a Playwright/e2e UI test for visible or interactive behavior in {artifact}"
            )
        elif kind == "contract":
            actions.append(f"add a contract/schema/API test for {artifact}")
        elif kind == "static_source":
            actions.append(f"add a source/trace metadata test for {artifact}")
        elif kind == "graph_validation":
            actions.append(f"add graph-ba validation evidence for {artifact}")
        else:
            actions.append(f"add {kind} evidence for {artifact}")
    return actions


def fmt_table(rows: list, headers: list) -> str:
    """Format rows as a compact aligned table."""
    if not rows:
        return "(empty)"
    widths = [len(h) for h in headers]
    str_rows = []
    for r in rows:
        sr = [str(c) if c is not None else "" for c in r]
        str_rows.append(sr)
        for i, c in enumerate(sr):
            if i < len(widths):
                widths[i] = max(widths[i], len(c))
    sep = "  "
    lines = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append(sep.join("─" * w for w in widths))
    for sr in str_rows:
        lines.append(
            sep.join(
                sr[i].ljust(widths[i]) if i < len(widths) else ""
                for i in range(len(headers))
            )
        )
    return "\n".join(lines)

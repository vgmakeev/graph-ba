"""Human-readable renderers for graph-ba change and evidence outputs."""

from __future__ import annotations

from typing import Any


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

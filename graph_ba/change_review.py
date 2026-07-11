"""Consolidated human review payload and Markdown rendering for changes."""

from __future__ import annotations

from typing import Any


def build_change_review(
    change_id: str,
    manifest: dict[str, Any],
    compiled: dict[str, Any],
    proposal: dict[str, Any],
    approval: dict[str, Any],
    delivery: dict[str, Any],
) -> dict[str, Any]:
    semantic = compiled["semantic"]
    graph_delta = compiled["graph_delta"]
    impact = compiled["impact"]
    return {
        "schema": "graph-ba.change-review.v1",
        "change": {
            "id": change_id,
            "title": manifest.get("title") or change_id,
            "intent": manifest.get("intent") or "",
            "sources": list(manifest.get("sources", [])),
            "scope": list(manifest.get("scope", [])),
            "base_ref": manifest.get("base_ref") or "",
            "target_ref": manifest.get("target_ref") or "",
        },
        "git": semantic["git"],
        "proposal_fingerprint": semantic["proposal_fingerprint"],
        "policy": semantic.get("policy", {}),
        "files": {
            "contract": semantic.get("contract_files", []),
            "supporting": semantic.get("supporting_files", []),
            "delivery": semantic.get("delivery_files", []),
        },
        "contract_delta": semantic["contract"],
        "graph_delta": graph_delta,
        "impact": impact,
        "proposal": proposal,
        "rebase": proposal.get("rebase", {}),
        "approval": approval,
        "delivery": delivery,
    }


def render_change_review(payload: dict[str, Any]) -> str:
    change = payload["change"]
    proposal = payload["proposal"]
    delivery = payload["delivery"]
    rebase = payload["rebase"]
    approval = payload["approval"]
    graph_summary = payload["graph_delta"]["summary"]
    impact_summary = payload["impact"]["summary"]
    lines = [
        f"# graph-ba review: {change['id']}",
        "",
        f"**Title:** {change['title']}",
        f"**Intent:** {change['intent'] or '(missing)'}",
        f"**Proposal:** {proposal['verdict']}",
        f"**Delivery:** {delivery['verdict']}",
        f"**Rebase:** {rebase.get('status', 'unknown')}",
        f"**Approval:** {'valid' if approval.get('valid') else approval.get('reason', 'missing')}",
        f"**Fingerprint:** `{payload['proposal_fingerprint']}`",
        "",
        "## Sources And Scope",
        "",
    ]
    lines.extend(f"- source: `{item}`" for item in change["sources"])
    lines.extend(f"- scope: `{item}`" for item in change["scope"])
    if not change["sources"] and not change["scope"]:
        lines.append("- none")

    lines.extend(["", "## Semantic Delta", ""])
    for item in payload["contract_delta"]:
        current = item.get("after") or item.get("before") or {}
        lines.append(
            f"- `{item['operation']}` `{item['id']}` [{item['type']}] "
            f"{current.get('title', '')}"
        )
    if not payload["contract_delta"]:
        lines.append("- no canonical artifact changes")
    lines.append(
        f"- graph: nodes +{graph_summary['nodes_added']} "
        f"~{graph_summary['nodes_modified']} -{graph_summary['nodes_removed']}; "
        f"edges +{graph_summary['edges_added']} -{graph_summary['edges_removed']}"
    )

    lines.extend(["", "## Files", ""])
    for group in ("contract", "supporting", "delivery"):
        entries = payload["files"][group]
        rendered = ", ".join(
            f"`{item.get('path') or item.get('old_path')}`" for item in entries
        ) or "none"
        lines.append(f"- {group}: {rendered}")

    lines.extend(
        [
            "",
            "## Impact",
            "",
            f"- nodes: {impact_summary['nodes']}",
            f"- implementation: {impact_summary['implementation']}",
            f"- evidence: {impact_summary['evidence']}",
            f"- truncated: {str(impact_summary['truncated']).lower()}",
            "",
            "## Findings",
            "",
        ]
    )
    findings = [
        *proposal.get("findings", []),
        *delivery.get("findings", []),
    ]
    if findings:
        for finding in findings:
            lines.append(
                f"- `{finding.get('severity', 'info')}` "
                f"`{finding.get('code', 'finding')}` "
                f"`{finding.get('artifact', '')}` {finding.get('message', '')}"
            )
    else:
        lines.append("- no findings")
    return "\n".join(lines).rstrip() + "\n"

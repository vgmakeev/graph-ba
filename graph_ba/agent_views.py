"""Scoped quality gates, evidence planning and agent graph projections."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .gate_analysis import (
    _has_outgoing_to_type,
    _is_interactive_or_visible_uic,
    _priority_rank,
    _raw_to_canonical_synthesis_gap,
    _screen_has_acceptance_trace,
    _semantic_scope_relations,
    _worklist_blocking_modes,
    _worklist_priority,
    _worklist_related_nodes,
    _worklist_suggested_actions,
)


def _agent_worklist(
    gate_data: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a compact deterministic next-action list for agents.

    Keep this intentionally small. The graph already carries the rich context;
    worklist only answers "what should I fix next?".
    """
    by_id = {node["id"]: node for node in nodes}
    incoming: dict[str, list[dict[str, Any]]] = {}
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        incoming.setdefault(edge["to"], []).append(edge)
        outgoing.setdefault(edge["from"], []).append(edge)

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    sequence = 1

    def add(
        kind: str, artifact: str, reason: str, *, source: dict[str, Any] | None = None
    ) -> None:
        nonlocal sequence
        key = (kind, artifact, reason)
        if key in seen:
            return
        seen.add(key)
        node = by_id.get(artifact, {})
        item = {
            "id": f"WL-{sequence:03d}",
            "priority": _worklist_priority(kind, source),
            "kind": kind,
            "artifact": artifact,
            "artifact_type": node.get("type") or (source or {}).get("type", ""),
            "reason": reason,
            "suggested_actions": _worklist_suggested_actions(kind, artifact),
            "related_nodes": _worklist_related_nodes(artifact, incoming, outgoing),
            "blocking_in": _worklist_blocking_modes(kind),
        }
        if source:
            item["source"] = {
                k: v for k, v in source.items() if k in {"code", "gap_type", "severity"}
            }
        sequence += 1
        items.append(item)

    for finding in gate_data.get("findings", []):
        artifact = finding.get("artifact") or gate_data.get("target", "")
        code = finding.get("code", "")
        if code in {"unimplemented", "missing_implementation"}:
            add(
                "add_implementation",
                artifact,
                "artifact has no typed implementation proof path",
                source=finding,
            )
        elif code in {"unverified", "missing_evidence"}:
            add(
                "add_evidence",
                artifact,
                "artifact has no TEST/EVD verification path",
                source=finding,
            )
        elif code == "missing_required_evidence":
            add(
                "add_required_evidence",
                artifact,
                finding.get("message") or "artifact lacks required evidence kind",
                source=finding,
            )
        elif code in {"stale", "missing_snapshot"}:
            add(
                "refresh_acceptance",
                artifact,
                "accepted fingerprint or evidence is stale or missing",
                source=finding,
            )
        else:
            add(
                "add_trace",
                artifact,
                finding.get("message") or "artifact needs an explicit typed trace",
                source=finding,
            )

    behavior_axis = gate_data.get("quality_axes", {}).get("behavior_model", {})
    if behavior_axis.get("status") == "PARTIAL":
        missing = behavior_axis.get("missing", [])
        add(
            "add_behavior_rule",
            gate_data.get("target", ""),
            f"dynamic behavior model is partial; missing {', '.join(missing) or 'behavior artifacts'}",
            source={"code": "behavior_model_partial", "severity": "warn"},
        )
    evidence_profile = gate_data.get("evidence_profile", {})
    for artifact in evidence_profile.get("trace_only_ac", [])[:20]:
        add(
            "add_evidence",
            artifact,
            "AC has only trace evidence; add behavior/runtime evidence",
            source={"code": "trace_only_evidence", "severity": "warn"},
        )
    for node in nodes:
        artifact = node["id"]
        artifact_type = node["type"]
        computed = node.get("computed") or {}
        if artifact_type == "SCR":
            if not _has_outgoing_to_type(artifact, "CONTAINS", "UIC", outgoing, by_id):
                add(
                    "add_trace",
                    artifact,
                    "screen readiness lint: screen has no scoped UIC artifact",
                )
            if not _screen_has_acceptance_trace(artifact, outgoing, by_id):
                add(
                    "add_trace",
                    artifact,
                    "screen readiness lint: screen has no reachable AC trace",
                )
        if artifact_type == "AC" and computed:
            if computed.get("implemented") and not computed.get("verified"):
                add("add_evidence", artifact, "AC is implemented but not verified")
            elif computed.get("verified") and not computed.get("implemented"):
                add(
                    "add_implementation",
                    artifact,
                    "AC is verified but has no implementation proof",
                )
        if (
            artifact_type == "UIC"
            and _is_interactive_or_visible_uic(node)
            and not _has_outgoing_to_type(artifact, "TRACES_TO", "AC", outgoing, by_id)
        ):
            add("add_trace", artifact, "visible UI zone has no canonical AC trace")
        synthesis_gap = _raw_to_canonical_synthesis_gap(
            node, incoming, by_id, gate_data
        )
        if synthesis_gap:
            add("synthesize_ac", artifact, synthesis_gap)
        if computed.get("stale"):
            add(
                "refresh_acceptance",
                artifact,
                "artifact fingerprint changed after acceptance",
            )

    return sorted(
        items, key=lambda item: (_priority_rank(item["priority"]), item["id"])
    )


def _graph_slice_edges(
    db: sqlite3.Connection,
    ids: set[str],
    include_mentions: bool,
) -> list[dict[str, Any]]:
    if not ids:
        return []
    relations = set(_semantic_scope_relations())
    if include_mentions:
        relations.add("MENTIONS")
    placeholders = ",".join("?" for _ in ids)
    relation_placeholders = ",".join("?" for _ in relations)
    rows = db.execute(
        "SELECT source_id, target_id, relation_type, context, source_file, line_number "
        f"FROM edges WHERE relation_type IN ({relation_placeholders}) "
        f"AND source_id IN ({placeholders}) AND target_id IN ({placeholders}) "
        "ORDER BY source_id, relation_type, target_id, source_file, line_number",
        (*sorted(relations), *tuple(sorted(ids)), *tuple(sorted(ids))),
    ).fetchall()
    return [
        {
            "from": row["source_id"],
            "relation": row["relation_type"],
            "to": row["target_id"],
            "source": {
                "file": row["source_file"],
                "line": row["line_number"],
            },
            "context": row["context"],
        }
        for row in rows
    ]


def _graph_class_matrices(root: Path) -> list[dict[str, Any]]:
    """Load project/adapter sparse class matrices for agent graph slices."""
    candidates = [
        root / ".graphba" / "artifact-class-matrix.json",
        root / "reports" / "graphba" / "mini-artifact-class-matrix.json",
    ]
    matrices: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path in candidates:
        _append_class_matrix(matrices, seen, path)
        if path.name == "artifact-class-matrix.json" and matrices:
            for upstream in matrices[-1].get("upstream_matrices", []):
                upstream_path = (
                    upstream.get("path") if isinstance(upstream, dict) else None
                )
                if upstream_path:
                    _append_class_matrix(matrices, seen, root / upstream_path)
    return matrices


def _append_class_matrix(
    matrices: list[dict[str, Any]], seen: set[Path], path: Path
) -> None:
    resolved = path.resolve()
    if resolved in seen or not resolved.exists():
        return
    seen.add(resolved)
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    matrices.append(
        {
            "source": str(path),
            "schema": data.get("schema", ""),
            "provider": data.get("provider") or data.get("project") or "",
            "description": data.get("description", ""),
            "entries": data.get("entries", []),
        }
    )

"""Manifest-free semantic and graph review of a Git worktree delta."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .change_workflow import ChangeWorkflowError, semantic_diff
from .gates import _gate_payload
from .graph_snapshots import GraphSnapshotError, graph_delta, graph_views, impact_paths


def diff_review(
    root: Path,
    db: sqlite3.Connection,
    *,
    base_ref: str | None = None,
    target_id: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Explain a Git delta without requiring a CHG manifest.

    The complete repository delta stays visible. When ``target_id`` is given,
    current and historical gate scopes are also compared so a reviewer can see
    introduced and resolved gaps instead of only the resulting graph state.
    """

    root = root.resolve()
    semantic = semantic_diff(root, base_ref=base_ref)
    try:
        with graph_views(
            root,
            db,
            semantic["git"]["base_commit"],
            materialize_base=bool(target_id),
        ) as views:
            delta = graph_delta(views, semantic)
            impact = impact_paths(
                views,
                delta,
                scope_hints=(target_id,) if target_id else (),
            )
            scoped = (
                _scoped_review(
                    views.base,
                    db,
                    views.base_root,
                    root,
                    target_id,
                    mode,
                    semantic,
                )
                if target_id
                else None
            )
    except GraphSnapshotError as exc:
        raise ChangeWorkflowError(str(exc)) from exc

    return {
        "schema": "graph-ba.diff-review.v1",
        "semantic": semantic,
        "graph_delta": delta,
        "impact": impact,
        "scope": scoped,
    }


def _scoped_review(
    base_db: sqlite3.Connection,
    proposed_db: sqlite3.Connection,
    base_root: Path,
    proposed_root: Path,
    target_id: str,
    mode: str | None,
    semantic: dict[str, Any],
) -> dict[str, Any]:
    base_gate = _gate_if_present(base_db, base_root, target_id, mode)
    proposed_gate = _gate_if_present(proposed_db, proposed_root, target_id, mode)
    if not base_gate and not proposed_gate:
        raise ChangeWorkflowError(f"Artifact not found in base or worktree: {target_id}")
    scope_ids = {
        item["id"]
        for gate in (base_gate, proposed_gate)
        if gate
        for item in gate.get("scope", [])
    }
    contract = list(semantic.get("contract", []))
    in_scope = [item for item in contract if item["id"] in scope_ids]
    outside_scope = [item for item in contract if item["id"] not in scope_ids]
    return {
        "target": target_id,
        "base": _readiness_snapshot(base_gate),
        "proposed": _readiness_snapshot(proposed_gate),
        "quality_axis_changes": _quality_axis_changes(base_gate, proposed_gate),
        "gaps": _gap_delta(base_gate, proposed_gate),
        "semantic": {
            "in_scope": in_scope,
            "outside_scope": outside_scope,
        },
    }


def _gate_if_present(
    db: sqlite3.Connection,
    root: Path,
    target_id: str,
    mode: str | None,
) -> dict[str, Any] | None:
    row = db.execute("SELECT 1 FROM artifacts WHERE id = ?", (target_id,)).fetchone()
    if not row:
        return None
    return _gate_payload(
        db,
        root,
        target_id,
        mode,
        None,
        require_snapshot=False,
    )


def _readiness_snapshot(gate: dict[str, Any] | None) -> dict[str, Any]:
    if not gate:
        return {"present": False}
    return {
        "present": True,
        "verdict": gate["verdict"],
        "readiness": gate["readiness"],
        "confidence": gate["overall_confidence"],
        "summary": gate["summary"],
        "quality_axes": gate["quality_axes"],
    }


def _quality_axis_changes(
    base: dict[str, Any] | None,
    proposed: dict[str, Any] | None,
) -> list[dict[str, str]]:
    before = (base or {}).get("quality_axes", {})
    after = (proposed or {}).get("quality_axes", {})
    result: list[dict[str, str]] = []
    for name in sorted(set(before) | set(after)):
        old_status = before.get(name, {}).get("status", "ABSENT")
        new_status = after.get(name, {}).get("status", "ABSENT")
        if old_status != new_status:
            result.append({"axis": name, "before": old_status, "after": new_status})
    return result


def _gap_delta(
    base: dict[str, Any] | None,
    proposed: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    before = _gap_items(base)
    after = _gap_items(proposed)
    before_keys = set(before)
    after_keys = set(after)
    return {
        "introduced": [after[key] for key in sorted(after_keys - before_keys)],
        "resolved": [before[key] for key in sorted(before_keys - after_keys)],
        "persistent": [after[key] for key in sorted(before_keys & after_keys)],
    }


def _gap_items(gate: dict[str, Any] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    if not gate:
        return {}
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in gate.get("findings", []):
        normalized = {
            "source": "finding",
            "kind": item.get("code", "finding"),
            "artifact": item.get("artifact", ""),
            "severity": item.get("severity", ""),
            "blocking": bool(item.get("blocking", False)),
            "reason": item.get("message", ""),
        }
        key = (normalized["source"], normalized["kind"], normalized["artifact"])
        result[key] = normalized
    for item in gate.get("agent_worklist", []):
        normalized = {
            "source": "worklist",
            "kind": item.get("kind", "work"),
            "artifact": item.get("artifact", ""),
            "priority": item.get("priority", ""),
            "reason": item.get("reason", ""),
        }
        key = (normalized["source"], normalized["kind"], normalized["artifact"])
        result[key] = normalized
    return result

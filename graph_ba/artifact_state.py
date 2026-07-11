"""Artifact lifecycle, fingerprint, implementation and evidence state."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import click

from graph_ba.db import _resolve_file
from graph_ba.review import _artifact_section_text

IMPLEMENTATION_ARTIFACT_TYPES = {
    "ADMIN_PAGE_ACTION",
    "CRUDL_RESOURCE",
    "CUSTOM_METHOD",
    "DATA_SOURCE",
    "INTEGRATION_ACTION",
    "INTEGRATION_CONNECTION",
    "INTEGRATION_TRIGGER",
    "JOB",
    "REACT_COMPONENT",
    "REGISTRY_DECLARATION",
    "RUNTIME",
    "RUNTIME_CONFIG",
}

PROOF_INCOMING_RELATIONS = {
    "CODE_TRACE",
    "CONTAINS",
    "DEPENDS_ON",
    "IMPLEMENTS",
    "RENDERS",
    "TRACES_TO",
    "UI_TRACE",
}

PROOF_OUTGOING_RELATIONS = {
    "DEPENDS_ON",
    "TRACES_TO",
}


def _artifact_state_payload(
    db: sqlite3.Connection,
    root: Path,
    artifact_id: str | None,
    snapshot_path: Path | None,
) -> dict[str, Any]:
    snapshot = _load_fingerprint_snapshot(snapshot_path)
    lifecycle_overrides = _graph_native_lifecycle_map(root)
    change_states = _graph_native_change_state_map(root)
    rows = _artifact_rows(db, artifact_id)
    artifacts = [
        _artifact_state_item(db, row, root, lifecycle_overrides, change_states, snapshot)
        for row in rows
    ]
    return {
        "schema": "graph-ba.artifact-state.v1",
        "snapshot_path": str(snapshot_path) if snapshot_path else "",
        "snapshot_loaded": bool(snapshot),
        "artifacts": artifacts,
    }


def _artifact_rows(db: sqlite3.Connection, artifact_id: str | None) -> list[sqlite3.Row]:
    if artifact_id:
        rows = db.execute(
            "SELECT * FROM artifacts WHERE id = ? ORDER BY id",
            (artifact_id,),
        ).fetchall()
        if not rows:
            raise click.ClickException(f"Artifact not found: {artifact_id}")
        return rows
    return db.execute("SELECT * FROM artifacts ORDER BY id").fetchall()


def _artifact_state_item(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    root: Path,
    lifecycle_overrides: dict[str, str],
    change_states: dict[str, dict[str, str]],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    artifact_id = row["id"]
    lifecycle = _artifact_lifecycle(row, lifecycle_overrides)
    fingerprints = _artifact_fingerprints(db, row)
    active_changes = _active_changes_for_artifact(db, artifact_id, change_states)
    implementation_proofs = _artifact_implementation_proofs(db, row)
    implemented = bool(implementation_proofs)
    verified = _artifact_has_evidence(db, artifact_id)
    baseline = snapshot.get("artifacts", {}).get(artifact_id, {}) if snapshot else {}
    baseline_fingerprints = baseline.get("fingerprints", {}) if isinstance(baseline, dict) else {}
    stale_reasons = _stale_reasons(fingerprints, baseline_fingerprints)
    return {
        "id": artifact_id,
        "type": row["type"],
        "origin": row["origin"],
        "title": row["title"],
        "source_file": row["source_file"],
        "line_number": row["line_number"],
        "lifecycle": lifecycle,
        "computed": {
            "implemented": implemented,
            "verified": verified,
            "changing": bool(active_changes),
            "stale": bool(stale_reasons),
            "unimplemented": lifecycle in {"accepted", "planned"} and not implemented,
            "unverified": lifecycle in {"accepted", "planned"} and not verified,
        },
        "implementation_proofs": implementation_proofs[:5],
        "active_changes": active_changes,
        "fingerprints": fingerprints,
        "baseline": {
            "present": bool(baseline_fingerprints),
            "stale_reasons": stale_reasons,
        },
    }


def _artifact_lifecycle(row: sqlite3.Row, overrides: dict[str, str]) -> str:
    artifact_id = row["id"]
    if artifact_id in overrides:
        return overrides[artifact_id]
    if row["type"] == "CHG":
        return overrides.get(artifact_id, "draft")
    if row["origin"] in {"canonical", "implementation", "evidence"}:
        return "accepted"
    return "draft" if int(row["defined"] or 0) else "unknown"


def _artifact_fingerprints(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, str]:
    artifact_id = row["id"]
    content = _artifact_content(db, row)
    links = _edge_tuples(db, "WHERE source_id = ?", (artifact_id,))
    observed = _edge_tuples(
        db,
        "WHERE (source_id = ? OR target_id = ?) "
        "AND relation_type IN ('IMPLEMENTS', 'RENDERS', 'DEPENDS_ON')",
        (artifact_id, artifact_id),
    )
    evidence = _edge_tuples(
        db,
        "WHERE (source_id = ? OR target_id = ?) AND relation_type IN ('TEST_EVIDENCE', 'VERIFIES')",
        (artifact_id, artifact_id),
    )
    return {
        "content": _sha256_text(content),
        "links": _sha256_json(links),
        "observed": _sha256_json(observed),
        "evidence": _sha256_json(evidence),
    }


def _artifact_content(db: sqlite3.Connection, row: sqlite3.Row) -> str:
    full_path = _resolve_file(db, row["source_file"]) if row["source_file"] else None
    if not full_path:
        return f"{row['id']}\n{row['type']}\n{row['title']}"
    path = Path(full_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return f"{row['id']}\n{row['type']}\n{row['title']}"
    line_number = int(row["line_number"] or 0)
    if 1 <= line_number <= len(lines) and lines[line_number - 1].lstrip().startswith(":::artifact"):
        collected = []
        for line in lines[line_number - 1 :]:
            collected.append(line)
            if line.strip() == ":::":
                break
        return "\n".join(collected)
    if line_number > 0:
        return _artifact_section_text(str(path), line_number)
    return "\n".join(lines)


def _edge_tuples(
    db: sqlite3.Connection,
    where_clause: str,
    params: tuple[Any, ...],
) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT source_id, target_id, relation_type, context "
        f"FROM edges {where_clause} "
        "ORDER BY source_id, target_id, relation_type, context",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _artifact_has_implementation(db: sqlite3.Connection, row: sqlite3.Row) -> bool:
    return bool(_artifact_implementation_proofs(db, row))


def _artifact_implementation_proofs(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    max_depth: int = 4,
    max_proofs: int = 8,
) -> list[dict[str, Any]]:
    """Find short typed-edge paths from implementation facts to this artifact.

    The traversal intentionally ignores weak MENTIONS edges. It follows incoming
    semantic edges toward implementation nodes, and follows outgoing DEPENDS_ON
    edges from bridge nodes such as UIC/DATA_SOURCE when the implementation is a
    dependency rather than the source of the semantic trace.
    """
    artifact_id = row["id"]
    if _is_implementation_artifact(row):
        return [
            {
                "source": artifact_id,
                "source_type": row["type"],
                "path": [],
                "reason": "artifact_is_observed_implementation",
            }
        ]

    proofs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = {(artifact_id, 0)}
    queue: list[tuple[str, list[dict[str, Any]], int]] = [(artifact_id, [], 0)]
    incoming_placeholders = ",".join("?" for _ in PROOF_INCOMING_RELATIONS)
    outgoing_placeholders = ",".join("?" for _ in PROOF_OUTGOING_RELATIONS)
    while queue and len(proofs) < max_proofs:
        current_id, path, depth = queue.pop(0)
        if depth >= max_depth:
            continue

        incoming = db.execute(
            "SELECT e.source_id, e.target_id, e.relation_type, s.type AS source_type, "
            "s.origin AS source_origin, s.title AS source_title "
            "FROM edges e JOIN artifacts s ON e.source_id = s.id "
            "WHERE e.target_id = ? "
            f"AND e.relation_type IN ({incoming_placeholders})",
            (current_id, *sorted(PROOF_INCOMING_RELATIONS)),
        ).fetchall()
        for edge in incoming:
            next_path = [
                {
                    "from": edge["source_id"],
                    "relation": edge["relation_type"],
                    "to": edge["target_id"],
                },
                *path,
            ]
            if _is_implementation_type(edge["source_type"], edge["source_origin"]):
                proofs.append(
                    {
                        "source": edge["source_id"],
                        "source_type": edge["source_type"],
                        "path": next_path,
                        "reason": "typed_implementation_path",
                    }
                )
                if len(proofs) >= max_proofs:
                    break
            else:
                key = (edge["source_id"], depth + 1)
                if key not in seen:
                    seen.add(key)
                    queue.append((edge["source_id"], next_path, depth + 1))
        if len(proofs) >= max_proofs:
            break

        outgoing = db.execute(
            "SELECT e.source_id, e.target_id, e.relation_type, t.type AS target_type, "
            "t.origin AS target_origin, t.title AS target_title "
            "FROM edges e JOIN artifacts t ON e.target_id = t.id "
            "WHERE e.source_id = ? "
            f"AND e.relation_type IN ({outgoing_placeholders})",
            (current_id, *sorted(PROOF_OUTGOING_RELATIONS)),
        ).fetchall()
        for edge in outgoing:
            next_path = [
                *path,
                {
                    "from": edge["source_id"],
                    "relation": edge["relation_type"],
                    "to": edge["target_id"],
                },
            ]
            if _is_implementation_type(edge["target_type"], edge["target_origin"]):
                proofs.append(
                    {
                        "source": edge["target_id"],
                        "source_type": edge["target_type"],
                        "path": next_path,
                        "reason": "typed_dependency_path",
                    }
                )
                if len(proofs) >= max_proofs:
                    break
            else:
                key = (edge["target_id"], depth + 1)
                if key not in seen:
                    seen.add(key)
                    queue.append((edge["target_id"], next_path, depth + 1))
    return proofs


def _is_implementation_artifact(row: sqlite3.Row) -> bool:
    return _is_implementation_type(row["type"], row["origin"])


def _is_implementation_type(artifact_type: str, origin: str | None) -> bool:
    return origin == "implementation" or artifact_type in IMPLEMENTATION_ARTIFACT_TYPES


def _artifact_has_evidence(db: sqlite3.Connection, artifact_id: str) -> bool:
    found = db.execute(
        "SELECT 1 FROM edges e JOIN artifacts s ON e.source_id = s.id "
        "WHERE e.target_id = ? "
        "AND e.relation_type IN ('TEST_EVIDENCE', 'VERIFIES') "
        "AND (s.origin = 'evidence' OR s.type IN ('TEST', 'EVD', 'UI')) LIMIT 1",
        (artifact_id,),
    ).fetchone()
    return bool(found)


def _active_changes_for_artifact(
    db: sqlite3.Connection,
    artifact_id: str,
    change_states: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    rows = db.execute(
        "SELECT e.source_id, a.title FROM edges e "
        "JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? AND a.type = 'CHG' "
        "AND e.relation_type IN ('CONTAINS', 'DEPENDS_ON') "
        "ORDER BY e.source_id",
        (artifact_id,),
    ).fetchall()
    active = []
    for row in rows:
        state = change_states.get(row["source_id"], {}).get("state", "draft")
        if state in {"accepted", "archived"}:
            continue
        active.append(
            {
                "id": row["source_id"],
                "title": row["title"] or "",
                "state": state,
                "mode": change_states.get(row["source_id"], {}).get("mode", ""),
            }
        )
    return active


def _stale_reasons(current: dict[str, str], baseline: dict[str, str]) -> list[str]:
    if not baseline:
        return []
    return [
        key
        for key in ("content", "links", "observed", "evidence")
        if baseline.get(key) and baseline.get(key) != current.get(key)
    ]


def _load_fingerprint_snapshot(snapshot_path: Path | None) -> dict[str, Any]:
    if not snapshot_path or not snapshot_path.exists():
        return {}
    try:
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise click.ClickException(f"Cannot read snapshot {snapshot_path}: {exc}") from exc


def _graph_native_lifecycle_map(root: Path) -> dict[str, str]:
    try:
        from graph_ba.config import load_config, normalize_id
        from graph_ba.traceability import _graph_native_artifact_files, _parse_graph_native_attrs

        config = load_config(root)
    except Exception:
        return {}
    result: dict[str, str] = {}
    for filepath in _graph_native_artifact_files(root, config):
        try:
            lines = filepath.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            marker = re.match(r"^\s*:::artifact\s+(.+?)\s*$", line)
            if not marker:
                continue
            attrs = _parse_graph_native_attrs(marker.group(1))
            if attrs.get("id") and attrs.get("state"):
                result[normalize_id(attrs["id"], config)] = attrs["state"]
    result.update(
        {key: value.get("state", "") for key, value in _graph_native_change_state_map(root).items()}
    )
    return {key: value for key, value in result.items() if value}


def _graph_native_change_state_map(root: Path) -> dict[str, dict[str, str]]:
    try:
        from graph_ba.config import load_config, normalize_id
        from graph_ba.traceability import _graph_native_change_files, _read_graph_native_change

        config = load_config(root)
    except Exception:
        return {}
    result: dict[str, dict[str, str]] = {}
    for filepath in _graph_native_change_files(root, config):
        change = _read_graph_native_change(filepath)
        change_id = change.get("id")
        if not isinstance(change_id, str) or not change_id:
            continue
        result[normalize_id(change_id, config)] = {
            "state": str(change.get("state") or "draft"),
            "mode": str(change.get("mode") or ""),
        }
    return result


def _sha256_text(value: str) -> str:
    normalized = "\n".join(line.rstrip() for line in value.splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

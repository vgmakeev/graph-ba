"""Scoped quality gates, evidence planning and agent graph projections."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import click

from .artifact_state import (
    _artifact_content,
    _artifact_state_item,
    _graph_native_change_state_map,
    _graph_native_lifecycle_map,
    _load_fingerprint_snapshot,
)
from .rendering import _required_evidence_suggested_fix

CONTRACT_ARTIFACT_TYPES = {"AC", "RULE", "DER", "STATE", "EVT", "ENT", "MTH"}

DEFAULT_AC_EVIDENCE_MATRIX = {
    "algorithm": ["unit"],
    "backend_behavior": ["backend_integration"],
    "ui_visible": ["e2e_ui"],
    "ui_interaction": ["e2e_ui"],
    "permission": ["backend_integration", "e2e_ui"],
    "async_event": ["backend_integration", "e2e_ui"],
    "integration": ["contract"],
    "contract": ["static_source"],
    "traceability": ["graph_validation"],
}

DEFAULT_EVIDENCE_KIND_LABELS = {
    "unit": "Pure/unit test for deterministic rules, functions and mappers.",
    "backend_integration": "Backend integration test with runtime/database/service behavior.",
    "frontend_unit": "Frontend unit/model test, usually Vitest.",
    "e2e_ui": "End-to-end UI test, usually Playwright.",
    "contract": "API/schema/provider/contract test.",
    "static_source": "Static source or trace metadata test.",
    "manual": "Manual/runtime acceptance evidence.",
    "graph_validation": "graph-ba validation/audit evidence.",
}

DEFAULT_EVIDENCE_KIND_RULES = [
    {"kind": "graph_validation", "pattern": r"graphba|graph-ba|validate|audit"},
    {"kind": "static_source", "pattern": r"trace\.test|trace-strict|traceability|sources\.test"},
    {"kind": "e2e_ui", "pattern": r"playwright|admin/e2e|/e2e/|\.spec\.|acceptance"},
    {"kind": "frontend_unit", "pattern": r"vitest|admin/src|\.test\.ts|\.test\.tsx"},
    {"kind": "unit", "pattern": r"/tests/unit/|tests/unit/|/unit/"},
    {
        "kind": "backend_integration",
        "pattern": r"/tests/integration/|tests/integration/|/integration/",
    },
    {"kind": "contract", "pattern": r"contract|schema|api_contract"},
]

DEFAULT_AC_KIND_TARGET_TYPES = {
    "algorithm": {"RULE", "DER", "FUNC"},
    "backend_behavior": {
        "ADMIN_PAGE_ACTION",
        "CUSTOM_METHOD",
        "ENT",
        "JOB",
        "MTH",
        "RUNTIME",
        "STATE",
    },
    "ui_visible": {"SCR", "REACT_COMPONENT", "UI_TEST_ID"},
    "permission": {"PERM", "RL"},
    "async_event": {"EVT", "INTEGRATION_TRIGGER"},
    "integration": {"INT", "INTEGRATION_ACTION", "INTEGRATION_CONNECTION", "INTEGRATION_TRIGGER"},
    "contract": {"REGISTRY_DECLARATION", "CRUDL_RESOURCE", "CUSTOM_METHOD", "DATA_SOURCE"},
}


def delivery_gate_payload(
    db: sqlite3.Connection,
    root: Path,
    target_ids: list[str],
    *,
    proposal_fingerprint: str,
    mode: str | None,
    snapshot_path: Path | None,
) -> dict[str, Any]:
    """Evaluate delivery gates for the canonical artifacts in a semantic delta."""
    checks = [
        _gate_payload(db, root, target_id, mode or "release", snapshot_path)
        for target_id in sorted(set(target_ids))
    ]
    findings = [
        {**finding, "gate_target": check["target"]}
        for check in checks
        for finding in check.get("findings", [])
    ]
    passed = bool(checks) and all(check["pass"] for check in checks)
    return {
        "schema": "graph-ba.change-check.v1",
        "stage": "release",
        "verdict": "PASS" if passed else "FAIL",
        "pass": passed,
        "proposal_fingerprint": proposal_fingerprint,
        "targets": [check["target"] for check in checks],
        "summary": {
            "targets": len(checks),
            "passed": sum(check["pass"] for check in checks),
            "findings": len(findings),
        },
        "findings": findings,
        "checks": checks,
    }


def _change_payload(db: sqlite3.Connection, root: Path, change_id: str) -> dict[str, Any]:
    change_states = _graph_native_change_state_map(root)
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (change_id,)).fetchone()
    if not row:
        raise click.ClickException(f"Change not found in graph: {change_id}")
    scope_rows = _scope_rows(db, change_id)
    state_items = [
        _artifact_state_item(
            db,
            item,
            root,
            _graph_native_lifecycle_map(root),
            change_states,
            {},
        )
        for item in scope_rows
    ]
    return {
        "schema": "graph-ba.change.v1",
        "change": {
            "id": change_id,
            "title": row["title"],
            "state": change_states.get(change_id, {}).get("state", "draft"),
            "mode": change_states.get(change_id, {}).get("mode", ""),
        },
        "scope": state_items,
    }


def _scope_rows(db: sqlite3.Connection, change_id: str) -> list[sqlite3.Row]:
    ids = _scope_ids(db, change_id)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT * FROM artifacts WHERE id IN ({placeholders}) ORDER BY type, id",
            tuple(sorted(ids)),
        ).fetchall()
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (change_id,)).fetchone()
    return [row] if row else []


def _scope_ids(db: sqlite3.Connection, root_id: str) -> set[str]:
    scope_relations = _semantic_scope_relations()
    incoming_scope_relations = _incoming_scope_relations()
    result: set[str] = set()
    seen = {root_id}
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        outgoing_rows = db.execute(
            "SELECT target_id FROM edges WHERE source_id = ? "
            f"AND relation_type IN ({','.join('?' for _ in scope_relations)}) "
            "ORDER BY target_id",
            (current, *sorted(scope_relations)),
        ).fetchall()
        incoming_rows = db.execute(
            "SELECT source_id AS target_id FROM edges WHERE target_id = ? "
            f"AND relation_type IN ({','.join('?' for _ in incoming_scope_relations)}) "
            "ORDER BY source_id",
            (current, *sorted(incoming_scope_relations)),
        ).fetchall()
        for row in outgoing_rows:
            target_id = row["target_id"]
            if target_id in seen:
                continue
            seen.add(target_id)
            result.add(target_id)
            queue.append(target_id)
        for row in incoming_rows:
            target_id = row["target_id"]
            if target_id in seen:
                continue
            seen.add(target_id)
            result.add(target_id)
    return result


def _semantic_scope_relations() -> set[str]:
    return {
        "CONTAINS",
        "DEPENDS_ON",
        "TRACES_TO",
        "NORMALIZES",
        "IMPLEMENTS",
        "VERIFIES",
        "RENDERS",
        "TEST_EVIDENCE",
        "CODE_TRACE",
        "UI_TRACE",
    }


def _incoming_scope_relations() -> set[str]:
    return {
        "CODE_TRACE",
        "CONTAINS",
        "DEPENDS_ON",
        "IMPLEMENTS",
        "RENDERS",
        "TEST_EVIDENCE",
        "TRACES_TO",
        "UI_TRACE",
        "VERIFIES",
    }


def _direct_scope_rows(db: sqlite3.Connection, change_id: str) -> list[sqlite3.Row]:
    rows = db.execute(
        "SELECT DISTINCT a.* FROM edges e JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? AND e.relation_type IN ('CONTAINS', 'DEPENDS_ON') "
        "ORDER BY a.id",
        (change_id,),
    ).fetchall()
    if rows:
        return rows
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (change_id,)).fetchone()
    return [row] if row else []


def _gate_payload(
    db: sqlite3.Connection,
    root: Path,
    target_id: str,
    mode: str | None,
    snapshot_path: Path | None,
) -> dict[str, Any]:
    change_states = _graph_native_change_state_map(root)
    selected_mode = mode or change_states.get(target_id, {}).get("mode") or "dev"
    snapshot = _load_fingerprint_snapshot(snapshot_path)
    rows = _scope_rows(db, target_id)
    states = [
        _artifact_state_item(
            db,
            row,
            root,
            _graph_native_lifecycle_map(root),
            change_states,
            snapshot,
        )
        for row in rows
    ]
    evidence_plan = _evidence_plan_for_states(db, root, states)
    findings = _gate_findings(states, selected_mode, bool(snapshot))
    findings.extend(_evidence_plan_findings(evidence_plan, selected_mode))
    evidence_profile = _evidence_profile(db, states)
    quality_axes = _quality_axes(states, findings, evidence_profile, bool(snapshot))
    evidence_plan_summary = evidence_plan.get("summary", {})
    fail_count = sum(1 for item in findings if item["severity"] == "fail")
    warn_count = sum(1 for item in findings if item["severity"] == "warn")
    return {
        "schema": "graph-ba.gate.v1",
        "target": target_id,
        "mode": selected_mode,
        "pass": fail_count == 0,
        "verdict": "PASS" if fail_count == 0 else "FAIL",
        "summary": {
            "fail": fail_count,
            "warn": warn_count,
            "scope": len(states),
            "evidence_plan": {
                "ac_total": evidence_plan_summary.get("ac_total", 0),
                "ok": evidence_plan_summary.get("ok", 0),
                "gap": evidence_plan_summary.get("gap", 0),
            },
        },
        "quality_axes": quality_axes,
        "overall_confidence": _overall_confidence(quality_axes),
        "evidence_profile": evidence_profile,
        "evidence_plan": evidence_plan,
        "findings": findings,
        "scope": [
            {
                "id": item["id"],
                "type": item["type"],
                "lifecycle": item["lifecycle"],
                "computed": item["computed"],
                "implementation_proofs": item.get("implementation_proofs", []),
            }
            for item in states
        ],
    }


def _gate_findings(
    states: list[dict[str, Any]], mode: str, snapshot_loaded: bool
) -> list[dict[str, Any]]:
    if mode == "explore":
        return []
    findings: list[dict[str, Any]] = []
    strict = mode in {"review", "release"}
    release = mode == "release"
    for item in states:
        computed = item["computed"]
        if item["type"] in CONTRACT_ARTIFACT_TYPES and not computed["implemented"]:
            findings.append(_gate_finding(item, "unimplemented", "fail" if strict else "warn"))
        if item["type"] == "AC" and not computed["verified"]:
            findings.append(_gate_finding(item, "unverified", "fail" if strict else "warn"))
        if release and computed["stale"]:
            findings.append(_gate_finding(item, "stale", "fail"))
    if release and not snapshot_loaded:
        findings.append(
            {
                "artifact": "",
                "type": "",
                "code": "missing_snapshot",
                "severity": "fail",
                "message": "release gate requires accepted fingerprint snapshot",
            }
        )
    return findings


def _evidence_profile(db: sqlite3.Connection, states: list[dict[str, Any]]) -> dict[str, Any]:
    ac_ids = [item["id"] for item in states if item["type"] == "AC"]
    by_target: dict[str, list[dict[str, Any]]] = {artifact_id: [] for artifact_id in ac_ids}
    if ac_ids:
        placeholders = ",".join("?" for _ in ac_ids)
        rows = db.execute(
            "SELECT e.target_id, e.relation_type, s.id AS source_id, s.type AS source_type, "
            "s.origin AS source_origin, s.title AS source_title, s.source_file AS source_file "
            "FROM edges e JOIN artifacts s ON e.source_id = s.id "
            f"WHERE e.target_id IN ({placeholders}) "
            "AND e.relation_type IN ('TEST_EVIDENCE', 'VERIFIES') "
            "ORDER BY e.target_id, s.id",
            tuple(ac_ids),
        ).fetchall()
        for row in rows:
            kind = _evidence_kind(row)
            by_target.setdefault(row["target_id"], []).append(
                {
                    "source": row["source_id"],
                    "source_type": row["source_type"],
                    "relation": row["relation_type"],
                    "kind": kind,
                }
            )

    weak_only = []
    behavior_or_runtime = []
    runtime = []
    verified = []
    for artifact_id, evidence in by_target.items():
        if evidence:
            verified.append(artifact_id)
        kinds = {item["kind"] for item in evidence}
        if kinds and kinds <= {"trace"}:
            weak_only.append(artifact_id)
        if kinds & {"behavior", "runtime", "manual"}:
            behavior_or_runtime.append(artifact_id)
        if kinds & {"runtime", "manual"}:
            runtime.append(artifact_id)

    return {
        "ac_total": len(ac_ids),
        "ac_verified": len(verified),
        "ac_with_behavior_or_runtime_evidence": len(behavior_or_runtime),
        "ac_with_runtime_or_manual_evidence": len(runtime),
        "ac_trace_only": len(weak_only),
        "trace_only_ac": weak_only[:50],
        "sample": {
            artifact_id: evidence[:5]
            for artifact_id, evidence in list(by_target.items())[:20]
            if evidence
        },
    }


def _evidence_plan_for_states(
    db: sqlite3.Connection,
    root: Path,
    states: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify AC artifacts and compute required vs observed evidence kinds."""
    ac_ids = [item["id"] for item in states if item["type"] == "AC"]
    policy = _load_evidence_policy(root)
    if not ac_ids:
        return _empty_evidence_plan(policy)

    ids = {item["id"] for item in states}
    rows = _rows_for_ids(db, ids)
    by_id = {
        row["id"]: {
            "id": row["id"],
            "type": row["type"],
            "title": row["title"],
            "source_file": row["source_file"],
            "line_number": row["line_number"],
        }
        for row in rows
    }
    edges = _graph_slice_edges(db, ids, include_mentions=False)
    evidence_by_ac = _observed_evidence_by_ac(db, ac_ids, policy)
    items: list[dict[str, Any]] = []
    for ac_id in sorted(ac_ids):
        row = next((item for item in rows if item["id"] == ac_id), None)
        if row is None:
            continue
        kinds = _infer_ac_kinds(ac_id, row["title"] or "", edges, by_id, policy)
        required = _required_evidence_for_ac(ac_id, kinds, policy)
        observed = evidence_by_ac.get(ac_id, [])
        observed_kinds = sorted({item["kind"] for item in observed})
        missing = [
            kind
            for kind in required
            if not _evidence_requirement_satisfied(kind, observed_kinds, policy)
        ]
        related_targets = _related_target_summary(ac_id, edges, by_id)
        items.append(
            {
                "artifact": ac_id,
                "kinds": kinds,
                "required_evidence": required,
                "observed_evidence": observed,
                "observed_kinds": observed_kinds,
                "missing_required_evidence": missing,
                "missing_evidence": missing,
                "status": "GAP" if missing else "OK",
                "reason": _evidence_plan_reason(kinds, related_targets),
                "related_targets": related_targets,
            }
        )

    gaps = [item for item in items if item["missing_required_evidence"]]
    return {
        "schema": "graph-ba.evidence-plan.v1",
        "policy": {
            "providers": policy["providers"],
            "gate_blocking_modes": policy["gate_blocking_modes"],
            "synthesis_policy": policy.get("synthesis_policy", {}),
            "evidence_kind_rules": policy.get("evidence_kind_rules", []),
        },
        "evidence_kinds": DEFAULT_EVIDENCE_KIND_LABELS,
        "summary": {
            "ac_total": len(items),
            "ok": len(items) - len(gaps),
            "gap": len(gaps),
        },
        "items": items,
    }


def _empty_evidence_plan(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "graph-ba.evidence-plan.v1",
        "policy": {
            "providers": policy["providers"],
            "gate_blocking_modes": policy["gate_blocking_modes"],
            "synthesis_policy": policy.get("synthesis_policy", {}),
            "evidence_kind_rules": policy.get("evidence_kind_rules", []),
        },
        "evidence_kinds": DEFAULT_EVIDENCE_KIND_LABELS,
        "summary": {"ac_total": 0, "ok": 0, "gap": 0},
        "items": [],
    }


def _load_evidence_policy(root: Path) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "providers": ["graph-ba-default"],
        "matrix": {key: list(value) for key, value in DEFAULT_AC_EVIDENCE_MATRIX.items()},
        "overrides": {},
        "kind_target_types": {
            key: set(value) for key, value in DEFAULT_AC_KIND_TARGET_TYPES.items()
        },
        "satisfies": {},
        "evidence_kind_rules": list(DEFAULT_EVIDENCE_KIND_RULES),
        "gate_blocking_modes": [],
        "synthesis_policy": {},
    }
    for data in _evidence_policy_files(root):
        provider = str(
            data.get("provider") or data.get("project") or data.get("schema") or "project"
        )
        policy["providers"].append(provider)
        matrix = data.get("evidence_matrix")
        if isinstance(matrix, dict):
            for kind, requirements in matrix.items():
                if isinstance(requirements, list):
                    policy["matrix"][str(kind)] = [str(item) for item in requirements]
        overrides = data.get("overrides")
        if isinstance(overrides, dict):
            for artifact_id, override in overrides.items():
                if isinstance(override, dict):
                    policy["overrides"][str(artifact_id)] = override
        target_types = data.get("kind_target_types")
        if isinstance(target_types, dict):
            for kind, values in target_types.items():
                if isinstance(values, list):
                    policy["kind_target_types"].setdefault(str(kind), set()).update(
                        str(item) for item in values
                    )
        satisfies = data.get("satisfies")
        if isinstance(satisfies, dict):
            for required, observed in satisfies.items():
                if isinstance(observed, list):
                    policy["satisfies"][str(required)] = {str(item) for item in observed}
        evidence_kind_rules = data.get("evidence_kind_rules")
        if isinstance(evidence_kind_rules, list):
            normalized_rules = []
            for item in evidence_kind_rules:
                if not isinstance(item, dict):
                    continue
                kind = item.get("kind")
                pattern = item.get("pattern")
                if kind and pattern:
                    normalized_rules.append({"kind": str(kind), "pattern": str(pattern)})
            if normalized_rules:
                policy["evidence_kind_rules"] = normalized_rules
        blocking = data.get("gate_blocking_modes")
        if isinstance(blocking, list):
            policy["gate_blocking_modes"] = [str(item) for item in blocking]
        synthesis = data.get("synthesis_policy")
        if isinstance(synthesis, dict):
            policy["synthesis_policy"] = synthesis
    return policy


def _evidence_policy_files(root: Path) -> list[dict[str, Any]]:
    candidates = [
        root / "reports" / "graphba" / "mini-evidence-policy.json",
        root / ".graphba" / "evidence-policy.json",
    ]
    loaded: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path in candidates:
        _append_evidence_policy(loaded, seen, path)
        if path.name == "evidence-policy.json" and loaded:
            for upstream in loaded[-1].get("upstream_policies", []):
                upstream_path = upstream.get("path") if isinstance(upstream, dict) else None
                if upstream_path:
                    _append_evidence_policy(loaded, seen, root / upstream_path)
    return loaded


def _append_evidence_policy(loaded: list[dict[str, Any]], seen: set[Path], path: Path) -> None:
    resolved = path.resolve()
    if resolved in seen or not resolved.exists():
        return
    seen.add(resolved)
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict):
        loaded.append(data)


def _infer_ac_kinds(
    ac_id: str,
    title: str,
    edges: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> list[str]:
    override = policy["overrides"].get(ac_id, {})
    if isinstance(override, dict) and isinstance(override.get("kinds"), list):
        return _dedupe_worklist_ids([str(item) for item in override["kinds"]])

    related_types = {
        by_id.get(edge["from"], {}).get("type") for edge in edges if edge["to"] == ac_id
    } | {by_id.get(edge["to"], {}).get("type") for edge in edges if edge["from"] == ac_id}
    related_types.discard(None)
    kinds: list[str] = []
    for kind, target_types in policy["kind_target_types"].items():
        if related_types & target_types:
            kinds.append(kind)

    text = f"{ac_id} {title}".lower()
    keyword_rules = {
        "algorithm": (
            "algorithm",
            "calculate",
            "computed",
            "derived",
            "formula",
            "mapper",
            "round",
            "sort",
            "threshold",
            "алгоритм",
            "вычис",
            "порог",
            "процент",
            "расчет",
            "расчёт",
            "сорт",
        ),
        "backend_behavior": (
            "backend",
            "command",
            "database",
            "db",
            "recalculate",
            "service",
            "пересчет",
            "пересчёт",
        ),
        "ui_visible": (
            "banner",
            "display",
            "render",
            "screen",
            "show",
            "timeline",
            "visible",
            "баннер",
            "виден",
            "видна",
            "отображ",
            "показы",
            "экран",
        ),
        "ui_interaction": (
            "action",
            "button",
            "click",
            "confirm",
            "dialog",
            "input",
            "modal",
            "reject",
            "диалог",
            "кноп",
            "модал",
            "нажим",
            "отклон",
            "подтверж",
        ),
        "permission": ("access", "permission", "role", "доступ", "прав", "роль"),
        "async_event": (
            "async",
            "event",
            "live",
            "polling",
            "sse",
            "update",
            "websocket",
            "обнов",
            "событ",
        ),
        "integration": ("external", "integration", "provider", "webhook", "интеграц", "провайдер"),
        "contract": ("api", "contract", "custommethod", "dto", "registry", "schema"),
    }
    for kind, markers in keyword_rules.items():
        if any(marker in text for marker in markers):
            kinds.append(kind)
    if not kinds:
        kinds.append("contract")
    return sorted(_dedupe_worklist_ids(kinds))


def _required_evidence_for_ac(ac_id: str, kinds: list[str], policy: dict[str, Any]) -> list[str]:
    override = policy["overrides"].get(ac_id, {})
    if isinstance(override, dict) and isinstance(override.get("required_evidence"), list):
        return _dedupe_worklist_ids([str(item) for item in override["required_evidence"]])
    required: list[str] = []
    for kind in kinds:
        required.extend(policy["matrix"].get(kind, []))
    return sorted(_dedupe_worklist_ids(required))


def _observed_evidence_by_ac(
    db: sqlite3.Connection,
    ac_ids: list[str],
    policy: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    by_target: dict[str, list[dict[str, Any]]] = {artifact_id: [] for artifact_id in ac_ids}
    if not ac_ids:
        return by_target
    placeholders = ",".join("?" for _ in ac_ids)
    rows = db.execute(
        "SELECT e.target_id, e.relation_type, s.id AS source_id, s.type AS source_type, "
        "s.origin AS source_origin, s.title AS source_title, s.source_file AS source_file "
        "FROM edges e JOIN artifacts s ON e.source_id = s.id "
        f"WHERE e.target_id IN ({placeholders}) "
        "AND e.relation_type IN ('TEST_EVIDENCE', 'VERIFIES') "
        "ORDER BY e.target_id, s.id",
        tuple(ac_ids),
    ).fetchall()
    for row in rows:
        by_target.setdefault(row["target_id"], []).append(
            {
                "source": row["source_id"],
                "source_type": row["source_type"],
                "relation": row["relation_type"],
                "kind": _observed_evidence_kind(row, policy),
                "source_file": row["source_file"],
            }
        )
    return by_target


def _observed_evidence_kind(row: sqlite3.Row, policy: dict[str, Any] | None = None) -> str:
    source_type = row["source_type"]
    source = f"{row['source_id']} {row['source_title']} {row['source_file']}".lower()
    if source_type == "EVD":
        return "manual"
    for rule in (policy or {}).get("evidence_kind_rules", DEFAULT_EVIDENCE_KIND_RULES):
        if not isinstance(rule, dict):
            continue
        kind = rule.get("kind")
        pattern = rule.get("pattern")
        if kind and pattern and re.search(str(pattern), source, flags=re.IGNORECASE):
            return str(kind)
    if source_type == "TEST" and source.endswith(".py"):
        return "backend_integration"
    if source_type == "TEST":
        return "unit"
    return "manual" if row["source_origin"] == "evidence" else "static_source"


def _evidence_requirement_satisfied(
    required: str, observed_kinds: list[str], policy: dict[str, Any]
) -> bool:
    accepted = {required}
    accepted.update(policy.get("satisfies", {}).get(required, set()))
    return bool(accepted & set(observed_kinds))


def _related_target_summary(
    ac_id: str,
    edges: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    related: list[dict[str, str]] = []
    for edge in edges:
        if edge["to"] == ac_id:
            node = by_id.get(edge["from"], {})
            related.append(
                {"id": edge["from"], "type": node.get("type", ""), "relation": edge["relation"]}
            )
        elif edge["from"] == ac_id:
            node = by_id.get(edge["to"], {})
            related.append(
                {"id": edge["to"], "type": node.get("type", ""), "relation": edge["relation"]}
            )
    return related[:12]


def _evidence_plan_reason(kinds: list[str], related_targets: list[dict[str, str]]) -> str:
    related_types = sorted({item["type"] for item in related_targets if item.get("type")})
    if related_types:
        return f"AC classified as {', '.join(kinds)} from typed links to {', '.join(related_types)}"
    return f"AC classified as {', '.join(kinds)} from title/content heuristics"


def _evidence_plan_findings(evidence_plan: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    if mode == "explore":
        return []
    blocking_modes = set(evidence_plan.get("policy", {}).get("gate_blocking_modes", []))
    severity = "fail" if mode in blocking_modes else "warn"
    findings = []
    for item in evidence_plan.get("items", []):
        missing = item.get("missing_required_evidence") or []
        if not missing:
            continue
        findings.append(
            {
                "artifact": item["artifact"],
                "type": "AC",
                "code": "missing_required_evidence",
                "gap_type": "GAP-TEST",
                "severity": severity,
                "blocking": severity == "fail",
                "message": f"{item['artifact']} missing required evidence: {', '.join(missing)}",
                "suggested_fix": _required_evidence_suggested_fix(item["artifact"], missing),
            }
        )
    return findings


def _evidence_kind(row: sqlite3.Row) -> str:
    source_type = row["source_type"]
    source = f"{row['source_id']} {row['source_title']} {row['source_file']}".lower()
    if source_type == "EVD":
        return "manual"
    if any(
        marker in source
        for marker in ("trace.test", "trace-strict", "traceability", "graphba", "graph-ba")
    ):
        return "trace"
    if any(marker in source for marker in ("e2e", "playwright", ".spec.", "acceptance")):
        return "runtime"
    if source_type == "TEST":
        return "behavior"
    return "manual" if row["source_origin"] == "evidence" else "trace"


def _quality_axes(
    states: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    evidence_profile: dict[str, Any],
    snapshot_loaded: bool,
) -> dict[str, Any]:
    finding_codes = {item.get("code") for item in findings}
    fail_codes = {item.get("code") for item in findings if item.get("severity") == "fail"}
    types = {item["type"] for item in states}
    dynamic = _is_dynamic_scope(states)
    behavior_missing = _behavior_model_missing(types, dynamic)
    axes = {
        "traceability": _axis(
            "FAIL" if "missing_trace" in fail_codes else "PASS",
            "typed graph scope is connected; weak MENTIONS are not counted as proof",
        ),
        "implementation_proof": _axis(
            "FAIL"
            if "unimplemented" in fail_codes
            else ("PARTIAL" if "unimplemented" in finding_codes else "PASS"),
            "implemented means a typed path from observed implementation artifacts exists",
        ),
        "test_evidence": _axis(
            "FAIL" if "unverified" in fail_codes else _test_evidence_status(evidence_profile),
            _test_evidence_reason(evidence_profile),
        ),
        "behavior_model": _axis(
            "PARTIAL" if behavior_missing else "PASS",
            "dynamic behavior scopes should expose RULE/DER plus STATE/EVT artifacts"
            if behavior_missing
            else "required behavior artifact classes are present or scope is static",
            missing=behavior_missing,
        ),
        "runtime_acceptance": _axis(
            _runtime_acceptance_status(evidence_profile),
            "runtime/manual evidence is tracked separately from unit/API/trace evidence",
        ),
        "drift": _axis(
            "UNKNOWN" if not snapshot_loaded else ("FAIL" if "stale" in fail_codes else "PASS"),
            "accepted fingerprint snapshot is required for drift confidence",
        ),
    }
    return axes


def _axis(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    result = {"status": status, "reason": reason}
    result.update(extra)
    return result


def _test_evidence_status(profile: dict[str, Any]) -> str:
    if profile["ac_total"] == 0:
        return "UNKNOWN"
    if profile["ac_verified"] < profile["ac_total"]:
        return "FAIL"
    if profile["ac_trace_only"] > 0:
        return "PARTIAL"
    return "PASS"


def _test_evidence_reason(profile: dict[str, Any]) -> str:
    if profile["ac_total"] == 0:
        return "no AC artifacts in scope"
    return (
        f"{profile['ac_verified']}/{profile['ac_total']} AC have evidence; "
        f"{profile['ac_trace_only']} AC have only trace evidence"
    )


def _runtime_acceptance_status(profile: dict[str, Any]) -> str:
    if profile["ac_total"] == 0:
        return "UNKNOWN"
    if profile["ac_with_runtime_or_manual_evidence"] == 0:
        return "UNKNOWN"
    if profile["ac_with_runtime_or_manual_evidence"] < profile["ac_total"]:
        return "PARTIAL"
    return "PASS"


def _is_dynamic_scope(states: list[dict[str, Any]]) -> bool:
    dynamic_types = {"EVT", "STATE", "MTH", "CUSTOM_METHOD", "DATA_SOURCE"}
    if any(item["type"] in dynamic_types for item in states):
        return True
    dynamic_terms = (
        "polling",
        "live",
        "event",
        "state",
        "slot",
        "capacity",
        "cascade",
        "concurrent",
        "stale",
        "order",
        "заказ",
        "слот",
        "ёмк",
        "емк",
        "каскад",
        "конкур",
        "событ",
        "состоя",
    )
    return any(
        item["type"] == "AC"
        and any(term in f"{item['id']} {item['title']}".lower() for term in dynamic_terms)
        for item in states
    )


def _behavior_model_missing(types: set[str], dynamic: bool) -> list[str]:
    if not dynamic:
        return []
    missing = []
    if not ({"RULE", "DER"} & types):
        missing.append("RULE_OR_DER")
    if "STATE" not in types:
        missing.append("STATE")
    if "EVT" not in types:
        missing.append("EVT")
    return missing


def _overall_confidence(axes: dict[str, Any]) -> str:
    statuses = {axis.get("status") for axis in axes.values()}
    if "FAIL" in statuses:
        return "FAIL"
    if "PARTIAL" in statuses:
        return "PARTIAL"
    if "UNKNOWN" in statuses:
        return "UNKNOWN"
    return "PASS"


def _gate_finding(item: dict[str, Any], code: str, severity: str) -> dict[str, Any]:
    gap_type = _gap_type(item["type"], code)
    return {
        "artifact": item["id"],
        "type": item["type"],
        "code": code,
        "gap_type": gap_type,
        "severity": severity,
        "blocking": severity == "fail",
        "message": f"{item['id']} {code.replace('_', ' ')}",
        "suggested_fix": _gap_suggested_fix(item, code),
    }


def _gap_type(artifact_type: str, code: str) -> str:
    if code == "unverified":
        return "GAP-TEST"
    if code == "stale":
        return "GAP-DRIFT"
    if code == "unimplemented":
        return {
            "AC": "GAP-AC",
            "DER": "GAP-DER",
            "ENT": "GAP-SPEC",
            "EVT": "GAP-EVT",
            "MTH": "GAP-MTH",
            "RULE": "GAP-RULE",
            "STATE": "GAP-STATE",
        }.get(artifact_type, "GAP-SPEC")
    return "GAP-SPEC"


def _gap_suggested_fix(item: dict[str, Any], code: str) -> list[str]:
    artifact_type = item["type"]
    artifact_id = item["id"]
    if code == "unverified":
        return [
            f"add or link TEST/EVD that verifies {artifact_id}",
            "mention the canonical AC id in the deterministic test or evidence artifact",
        ]
    if code == "stale":
        return [
            f"refresh evidence for {artifact_id}",
            "accept the changed fingerprint through a new graph-ba change",
        ]
    if code != "unimplemented":
        return []
    if artifact_type == "AC":
        return [
            f"link {artifact_id} to implemented UI/domain artifacts through UIC, MTH, ENT, STATE or EVT",
            "ensure observed implementation has a typed path using IMPLEMENTS, RENDERS, DEPENDS_ON or TRACES_TO",
        ]
    if artifact_type == "MTH":
        return [
            f"add observed CUSTOM_METHOD, ADMIN_PAGE_ACTION or JOB implementing {artifact_id}",
            "declare the method in mini metadata or graph-native source",
        ]
    if artifact_type == "ENT":
        return [
            f"add observed CRUDL_RESOURCE implementing {artifact_id}",
            "declare the entity as a mini registry resource or link existing resource alias",
        ]
    if artifact_type in {"STATE", "EVT"}:
        return [
            f"link {artifact_id} to observed FSM/resource/custom method facts",
            "add transition/state metadata in mini registry or graph-native source",
        ]
    return [
        f"add a typed implementation path for {artifact_id}",
        "avoid relying on weak MENTIONS edges for acceptance proof",
    ]


def _pack_payload(db: sqlite3.Connection, root: Path, target_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (target_id,)).fetchone()
    if not row:
        raise click.ClickException(f"Artifact not found: {target_id}")
    if row["type"] == "CHG":
        rows = [row, *_scope_rows(db, target_id)]
    else:
        related_ids = _scope_ids(db, target_id)
        related_ids.add(target_id)
        if related_ids:
            semantic_relations = _semantic_scope_relations()
            placeholders = ",".join("?" for _ in related_ids)
            relation_placeholders = ",".join("?" for _ in semantic_relations)
            adjacent = db.execute(
                "SELECT source_id, target_id FROM edges "
                f"WHERE relation_type IN ({relation_placeholders}) "
                f"AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))",
                (
                    *sorted(semantic_relations),
                    *tuple(sorted(related_ids)),
                    *tuple(sorted(related_ids)),
                ),
            ).fetchall()
            for edge in adjacent:
                related_ids.add(edge["source_id"])
                related_ids.add(edge["target_id"])
        placeholders = ",".join("?" for _ in related_ids)
        rows = db.execute(
            f"SELECT * FROM artifacts WHERE id IN ({placeholders}) ORDER BY type, id",
            tuple(sorted(related_ids)),
        ).fetchall()
    artifacts = [
        {
            "id": item["id"],
            "type": item["type"],
            "title": item["title"],
            "source_file": item["source_file"],
            "line_number": item["line_number"],
            "content": _artifact_content(db, item),
        }
        for item in rows
    ]
    ids = [item["id"] for item in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        semantic_relations = _semantic_scope_relations()
        relation_placeholders = ",".join("?" for _ in semantic_relations)
        edge_rows = db.execute(
            "SELECT source_id, target_id, relation_type, context, source_file, line_number "
            f"FROM edges WHERE relation_type IN ({relation_placeholders}) "
            f"AND (source_id IN ({placeholders}) OR target_id IN ({placeholders})) "
            "ORDER BY source_id, target_id, relation_type",
            (*sorted(semantic_relations), *ids, *ids),
        ).fetchall()
    else:
        edge_rows = []
    return {
        "schema": "graph-ba.pack.v1",
        "target": target_id,
        "artifacts": artifacts,
        "edges": [dict(edge) for edge in edge_rows],
    }


def _graph_slice_payload(
    db: sqlite3.Connection,
    root: Path,
    target_id: str,
    mode: str | None,
    snapshot_path: Path | None,
    content_mode: str,
    content_limit: int,
    include_mentions: bool,
) -> dict[str, Any]:
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (target_id,)).fetchone()
    if not row:
        raise click.ClickException(f"Artifact not found: {target_id}")
    ids = _scope_ids(db, target_id)
    ids.add(target_id)
    gate_data = _gate_payload(db, root, target_id, mode, snapshot_path)
    computed_by_id = {item["id"]: item for item in gate_data["scope"]}
    ids.update(_proof_artifact_ids(gate_data))
    rows = _rows_for_ids(db, ids)
    nodes = [
        _graph_slice_node(
            db, item, computed_by_id.get(item["id"]), content_mode, max(0, content_limit)
        )
        for item in rows
    ]
    edges = _graph_slice_edges(db, ids, include_mentions)
    worklist = _agent_worklist(gate_data, nodes, edges)
    return {
        "schema": "graph-ba.graph-slice.v1",
        "target": target_id,
        "mode": gate_data["mode"],
        "summary": {
            **gate_data["summary"],
            "nodes": len(nodes),
            "edges": len(edges),
            "mentions_included": include_mentions,
            "content": content_mode,
            "content_limit": max(0, content_limit),
        },
        "quality_axes": gate_data["quality_axes"],
        "overall_confidence": gate_data["overall_confidence"],
        "evidence_profile": gate_data["evidence_profile"],
        "evidence_plan": gate_data["evidence_plan"],
        "relation_catalog": _relation_catalog(include_mentions),
        "class_matrices": _graph_class_matrices(root),
        "findings": gate_data["findings"],
        "agent_worklist": worklist,
        "nodes": nodes,
        "edges": edges,
    }


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

    def add(kind: str, artifact: str, reason: str, *, source: dict[str, Any] | None = None) -> None:
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
                    "add_trace", artifact, "screen readiness lint: screen has no reachable AC trace"
                )
        if artifact_type == "AC" and computed:
            if computed.get("implemented") and not computed.get("verified"):
                add("add_evidence", artifact, "AC is implemented but not verified")
            elif computed.get("verified") and not computed.get("implemented"):
                add(
                    "add_implementation", artifact, "AC is verified but has no implementation proof"
                )
        if (
            artifact_type == "UIC"
            and _is_interactive_or_visible_uic(node)
            and not _has_outgoing_to_type(artifact, "TRACES_TO", "AC", outgoing, by_id)
        ):
            add("add_trace", artifact, "visible UI zone has no canonical AC trace")
        synthesis_gap = _raw_to_canonical_synthesis_gap(node, incoming, by_id, gate_data)
        if synthesis_gap:
            add("synthesize_ac", artifact, synthesis_gap)
        if computed.get("stale"):
            add("refresh_acceptance", artifact, "artifact fingerprint changed after acceptance")

    return sorted(items, key=lambda item: (_priority_rank(item["priority"]), item["id"]))


def _worklist_priority(kind: str, source: dict[str, Any] | None) -> str:
    if source and source.get("severity") == "fail":
        return "P0"
    return {
        "add_implementation": "P0",
        "add_evidence": "P1",
        "add_required_evidence": "P1",
        "add_trace": "P1",
        "add_behavior_rule": "P1",
        "synthesize_ac": "P2",
        "refresh_acceptance": "P0",
    }.get(kind, "P2")


def _priority_rank(priority: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2}.get(priority, 9)


def _worklist_suggested_actions(kind: str, artifact: str) -> list[str]:
    if kind == "add_implementation":
        return [
            f"add a typed implementation path to {artifact}",
            "use IMPLEMENTS, RENDERS, DEPENDS_ON or TRACES_TO; do not rely on MENTIONS",
        ]
    if kind == "add_evidence":
        return [
            f"add or link TEST/EVD that verifies {artifact}",
            "mention the canonical artifact id in deterministic test/evidence",
        ]
    if kind == "add_required_evidence":
        return [
            f"add the missing evidence kind for {artifact}",
            "choose unit/backend integration/frontend unit/e2e UI/contract/static source evidence according to AC classification",
        ]
    if kind == "add_behavior_rule":
        return [
            f"add graph-native RULE/DER/STATE/EVT artifacts for {artifact}",
            "describe dynamic behavior before relying on implementation/test traces",
        ]
    if kind == "refresh_acceptance":
        return [
            f"refresh evidence or accepted fingerprint for {artifact}",
            "accept the delta through a graph-ba change",
        ]
    if kind == "synthesize_ac":
        return [
            f"synthesize a draft canonical AC from {artifact}",
            "link it with NORMALIZES/TRACES_TO and keep human/OpenSpec acceptance separate",
        ]
    return [
        f"add an explicit typed trace for {artifact}",
        "prefer the project class matrix relation instead of text-only mention",
    ]


def _worklist_blocking_modes(kind: str) -> list[str]:
    if kind == "refresh_acceptance":
        return ["release"]
    if kind == "synthesize_ac":
        return ["review", "release"]
    return ["review", "release"]


def _raw_to_canonical_synthesis_gap(
    node: dict[str, Any],
    incoming: dict[str, list[dict[str, Any]]],
    by_id: dict[str, dict[str, Any]],
    gate_data: dict[str, Any],
) -> str:
    policy = gate_data.get("evidence_plan", {}).get("policy", {}).get("synthesis_policy", {})
    raw_to_canonical = policy.get("raw_to_canonical") if isinstance(policy, dict) else None
    if not isinstance(raw_to_canonical, dict):
        return ""
    raw_types = set(raw_to_canonical.get("raw_types") or raw_to_canonical.get("source_types") or [])
    canonical_types = set(
        raw_to_canonical.get("canonical_types") or [raw_to_canonical.get("output_type", "AC")]
    )
    relation = str(raw_to_canonical.get("required_relation") or "NORMALIZES")
    if node.get("type") not in raw_types:
        return ""
    has_canonical = any(
        edge["relation"] == relation and by_id.get(edge["from"], {}).get("type") in canonical_types
        for edge in incoming.get(node["id"], [])
    )
    if has_canonical:
        return ""
    return (
        f"{node['id']} is raw source material without canonical "
        f"{'/'.join(sorted(canonical_types)) or 'AC'} via {relation}"
    )


def _worklist_related_nodes(
    artifact: str,
    incoming: dict[str, list[dict[str, Any]]],
    outgoing: dict[str, list[dict[str, Any]]],
) -> list[str]:
    ids = []
    for edge in incoming.get(artifact, [])[:5]:
        ids.append(edge["from"])
    for edge in outgoing.get(artifact, [])[:5]:
        ids.append(edge["to"])
    return _dedupe_worklist_ids(ids)[:8]


def _dedupe_worklist_ids(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _is_interactive_or_visible_uic(node: dict[str, Any]) -> bool:
    title = f"{node.get('id', '')} {node.get('title', '')}".lower()
    if any(
        marker in title
        for marker in ("button", "dialog", "form", "input", "action", "confirm", "modal")
    ):
        return True
    return node.get("type") == "UIC"


def _has_outgoing_to_type(
    artifact: str,
    relation: str,
    target_type: str,
    outgoing: dict[str, list[dict[str, Any]]],
    by_id: dict[str, dict[str, Any]],
) -> bool:
    return any(
        edge["relation"] == relation and by_id.get(edge["to"], {}).get("type") == target_type
        for edge in outgoing.get(artifact, [])
    )


def _screen_has_acceptance_trace(
    artifact: str,
    outgoing: dict[str, list[dict[str, Any]]],
    by_id: dict[str, dict[str, Any]],
) -> bool:
    seen = {artifact}
    queue = [artifact]
    allowed = {"CONTAINS", "TRACES_TO", "NORMALIZES"}
    while queue:
        current = queue.pop(0)
        for edge in outgoing.get(current, []):
            if edge["relation"] not in allowed:
                continue
            target = edge["to"]
            if by_id.get(target, {}).get("type") == "AC":
                return True
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return False


def _proof_artifact_ids(gate_data: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in gate_data.get("scope", []):
        for proof in item.get("implementation_proofs", []):
            source = proof.get("source")
            if source:
                ids.add(source)
            for edge in proof.get("path", []):
                if edge.get("from"):
                    ids.add(edge["from"])
                if edge.get("to"):
                    ids.add(edge["to"])
    return ids


def _rows_for_ids(db: sqlite3.Connection, ids: set[str]) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT * FROM artifacts WHERE id IN ({placeholders})",
        tuple(sorted(ids)),
    ).fetchall()
    return sorted(rows, key=_artifact_sort_key)


def _artifact_sort_key(row: sqlite3.Row) -> tuple[int, str]:
    priority = {
        "CHG": 0,
        "SCR": 1,
        "UIC": 2,
        "AC": 3,
        "RAC": 4,
        "RULE": 5,
        "DER": 6,
        "REACT_COMPONENT": 7,
        "DATA_SOURCE": 8,
        "CRUDL_RESOURCE": 9,
        "CUSTOM_METHOD": 10,
        "ENT": 11,
        "MTH": 12,
        "PERM": 13,
        "STATE": 14,
        "EVT": 15,
        "TEST": 16,
        "EVD": 17,
        "CODE": 18,
    }.get(row["type"], 100)
    return (priority, row["id"])


def _graph_slice_node(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    computed: dict[str, Any] | None,
    content_mode: str,
    content_limit: int,
) -> dict[str, Any]:
    content = "" if content_mode == "none" else _artifact_content(db, row)
    truncated = False
    if content_mode == "excerpt" and len(content) > content_limit:
        content = content[:content_limit].rstrip()
        truncated = True
    node = {
        "id": row["id"],
        "type": row["type"],
        "origin": row["origin"],
        "title": row["title"],
        "defined": bool(row["defined"]),
        "source": {
            "file": row["source_file"],
            "line": row["line_number"],
        },
        "computed": computed.get("computed", {}) if computed else {},
    }
    if computed and computed.get("implementation_proofs"):
        node["implementation_proofs"] = computed["implementation_proofs"]
    if content_mode != "none":
        node["content"] = {
            "mode": "full" if content_mode == "full" else "excerpt",
            "text": content,
            "truncated": truncated,
        }
    return node


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


def _relation_catalog(include_mentions: bool = False) -> list[dict[str, str]]:
    meanings = {
        "CODE_TRACE": "source/code observation connects implementation to graph artifact",
        "CONTAINS": "source artifact scopes or structurally contains target artifact",
        "DEPENDS_ON": "source artifact has a runtime, data, permission or semantic dependency on target",
        "IMPLEMENTS": "implementation artifact realizes target contract/entity/method",
        "MENTIONS": "weak text occurrence only; not proof and excluded from agent graph slices by default",
        "NORMALIZES": "canonical artifact normalizes raw/source artifact",
        "RENDERS": "UI/component artifact renders target screen or UI zone",
        "TEST_EVIDENCE": "automated test mentions and provides deterministic evidence for target",
        "TRACES_TO": "author-declared semantic trace from source to target",
        "UI_TRACE": "UI observation or metadata trace connects source to target",
        "VERIFIES": "source evidence verifies target artifact",
    }
    relations = sorted(_semantic_scope_relations() | ({"MENTIONS"} if include_mentions else set()))
    return [{"relation": relation, "meaning": meanings.get(relation, "")} for relation in relations]


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
                upstream_path = upstream.get("path") if isinstance(upstream, dict) else None
                if upstream_path:
                    _append_class_matrix(matrices, seen, root / upstream_path)
    return matrices


def _append_class_matrix(matrices: list[dict[str, Any]], seen: set[Path], path: Path) -> None:
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

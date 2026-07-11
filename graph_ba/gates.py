"""Scoped quality gates, evidence planning and agent graph projections."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import click

from .agent_views import _agent_worklist, _graph_class_matrices, _graph_slice_edges
from .gate_analysis import (
    _dedupe_worklist_ids,
    _evidence_profile,
    _gate_finding,
    _infer_ac_kinds,
    _quality_axes,
    _scope_ids,
    _scope_rows,
    _semantic_scope_relations,
)
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
    {
        "kind": "static_source",
        "pattern": r"trace\.test|trace-strict|traceability|sources\.test",
    },
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
    "integration": {
        "INT",
        "INTEGRATION_ACTION",
        "INTEGRATION_CONNECTION",
        "INTEGRATION_TRIGGER",
    },
    "contract": {
        "REGISTRY_DECLARATION",
        "CRUDL_RESOURCE",
        "CUSTOM_METHOD",
        "DATA_SOURCE",
    },
}


def delivery_gate_payload(
    db: sqlite3.Connection,
    root: Path,
    target_ids: list[str],
    *,
    proposal_fingerprint: str,
    mode: str | None,
    snapshot_path: Path | None,
    approval: dict[str, Any] | None = None,
    require_approval: bool = False,
) -> dict[str, Any]:
    """Evaluate delivery gates for the canonical artifacts in a semantic delta."""
    checks = [
        _gate_payload(
            db,
            root,
            target_id,
            mode or "release",
            snapshot_path,
            require_snapshot=False,
        )
        for target_id in sorted(set(target_ids))
    ]
    findings = [
        {**finding, "gate_target": check["target"]}
        for check in checks
        for finding in check.get("findings", [])
    ]
    if require_approval and not (approval or {}).get("valid"):
        findings.append(
            {
                "code": "missing_or_stale_approval",
                "severity": "FAIL",
                "blocking": True,
                "artifact": "CHG",
                "message": "release requires a human approval matching the proposal fingerprint",
                "approval": approval or {},
            }
        )
    passed = (
        bool(checks)
        and all(check["pass"] for check in checks)
        and (not require_approval or bool((approval or {}).get("valid")))
    )
    return {
        "schema": "graph-ba.change-check.v1",
        "stage": "release",
        "verdict": "PASS" if passed else "FAIL",
        "pass": passed,
        "proposal_fingerprint": proposal_fingerprint,
        "approval": approval or {"present": False, "valid": False},
        "targets": [check["target"] for check in checks],
        "summary": {
            "targets": len(checks),
            "passed": sum(check["pass"] for check in checks),
            "findings": len(findings),
        },
        "findings": findings,
        "checks": checks,
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
    *,
    require_snapshot: bool = True,
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
    findings = _gate_findings(
        states,
        selected_mode,
        bool(snapshot),
        require_snapshot=require_snapshot,
    )
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
    states: list[dict[str, Any]],
    mode: str,
    snapshot_loaded: bool,
    *,
    require_snapshot: bool = True,
) -> list[dict[str, Any]]:
    if mode == "explore":
        return []
    findings: list[dict[str, Any]] = []
    strict = mode in {"review", "release"}
    release = mode == "release"
    for item in states:
        computed = item["computed"]
        if item["type"] in CONTRACT_ARTIFACT_TYPES and not computed["implemented"]:
            findings.append(
                _gate_finding(item, "unimplemented", "fail" if strict else "warn")
            )
        if item["type"] == "AC" and not computed["verified"]:
            findings.append(
                _gate_finding(item, "unverified", "fail" if strict else "warn")
            )
        if release and computed["stale"]:
            findings.append(_gate_finding(item, "stale", "fail"))
    if release and require_snapshot and not snapshot_loaded:
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
        "matrix": {
            key: list(value) for key, value in DEFAULT_AC_EVIDENCE_MATRIX.items()
        },
        "overrides": {},
        "kind_target_types": {
            key: set(value) for key, value in DEFAULT_AC_KIND_TARGET_TYPES.items()
        },
        "satisfies": {
            "unit": {"frontend_unit"},
        },
        "evidence_kind_rules": list(DEFAULT_EVIDENCE_KIND_RULES),
        "gate_blocking_modes": [],
        "synthesis_policy": {},
    }
    for data in _evidence_policy_files(root):
        provider = str(
            data.get("provider")
            or data.get("project")
            or data.get("schema")
            or "project"
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
                    policy["satisfies"].setdefault(str(required), set()).update(
                        str(item) for item in observed
                    )
        evidence_kind_rules = data.get("evidence_kind_rules")
        if isinstance(evidence_kind_rules, list):
            normalized_rules = []
            for item in evidence_kind_rules:
                if not isinstance(item, dict):
                    continue
                kind = item.get("kind")
                pattern = item.get("pattern")
                if kind and pattern:
                    normalized_rules.append(
                        {"kind": str(kind), "pattern": str(pattern)}
                    )
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
                upstream_path = (
                    upstream.get("path") if isinstance(upstream, dict) else None
                )
                if upstream_path:
                    _append_evidence_policy(loaded, seen, root / upstream_path)
    return loaded


def _append_evidence_policy(
    loaded: list[dict[str, Any]], seen: set[Path], path: Path
) -> None:
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


def _required_evidence_for_ac(
    ac_id: str, kinds: list[str], policy: dict[str, Any]
) -> list[str]:
    override = policy["overrides"].get(ac_id, {})
    if isinstance(override, dict) and isinstance(
        override.get("required_evidence"), list
    ):
        return _dedupe_worklist_ids(
            [str(item) for item in override["required_evidence"]]
        )
    required: list[str] = []
    for kind in kinds:
        required.extend(policy["matrix"].get(kind, []))
    return sorted(_dedupe_worklist_ids(required))


def _observed_evidence_by_ac(
    db: sqlite3.Connection,
    ac_ids: list[str],
    policy: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    by_target: dict[str, list[dict[str, Any]]] = {
        artifact_id: [] for artifact_id in ac_ids
    }
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


def _observed_evidence_kind(
    row: sqlite3.Row, policy: dict[str, Any] | None = None
) -> str:
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
                {
                    "id": edge["from"],
                    "type": node.get("type", ""),
                    "relation": edge["relation"],
                }
            )
        elif edge["from"] == ac_id:
            node = by_id.get(edge["to"], {})
            related.append(
                {
                    "id": edge["to"],
                    "type": node.get("type", ""),
                    "relation": edge["relation"],
                }
            )
    return related[:12]


def _evidence_plan_reason(
    kinds: list[str], related_targets: list[dict[str, str]]
) -> str:
    related_types = sorted(
        {item["type"] for item in related_targets if item.get("type")}
    )
    if related_types:
        return f"AC classified as {', '.join(kinds)} from typed links to {', '.join(related_types)}"
    return f"AC classified as {', '.join(kinds)} from title/content heuristics"


def _evidence_plan_findings(
    evidence_plan: dict[str, Any], mode: str
) -> list[dict[str, Any]]:
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
                "suggested_fix": _required_evidence_suggested_fix(
                    item["artifact"], missing
                ),
            }
        )
    return findings


def _overall_confidence(axes: dict[str, Any]) -> str:
    statuses = {axis.get("status") for axis in axes.values()}
    if "FAIL" in statuses:
        return "FAIL"
    if "PARTIAL" in statuses:
        return "PARTIAL"
    if "UNKNOWN" in statuses:
        return "UNKNOWN"
    return "PASS"


def _graph_slice_payload(
    db: sqlite3.Connection,
    root: Path,
    target_id: str,
    mode: str | None,
    snapshot_path: Path | None,
    content_mode: str,
    content_limit: int,
    include_mentions: bool,
    *,
    gate_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (target_id,)).fetchone()
    if not row:
        raise click.ClickException(f"Artifact not found: {target_id}")
    ids = _scope_ids(db, target_id)
    ids.add(target_id)
    gate_data = gate_data or _gate_payload(db, root, target_id, mode, snapshot_path)
    computed_by_id = {item["id"]: item for item in gate_data["scope"]}
    ids.update(_proof_artifact_ids(gate_data))
    rows = _rows_for_ids(db, ids)
    nodes = [
        _graph_slice_node(
            db,
            item,
            computed_by_id.get(item["id"]),
            content_mode,
            max(0, content_limit),
        )
        for item in rows
    ]
    edges = _graph_slice_edges(db, ids, include_mentions)
    worklist = _agent_worklist(gate_data, nodes, edges)
    return {
        "schema": "graph-ba.graph-slice.v1",
        "target": target_id,
        "mode": gate_data["mode"],
        "pass": gate_data["pass"],
        "verdict": gate_data["verdict"],
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
        "lifecycle": computed.get("lifecycle", "") if computed else "",
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
    relations = sorted(
        _semantic_scope_relations() | ({"MENTIONS"} if include_mentions else set())
    )
    return [
        {"relation": relation, "meaning": meanings.get(relation, "")}
        for relation in relations
    ]

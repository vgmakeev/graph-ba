"""Scoped quality gates, evidence planning and agent graph projections."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import click

from .class_matrix import ClassMatrixPolicy, EMPTY_CLASS_MATRIX, load_class_matrix_policy
from .config import DEFAULT_RELATIONS, ProjectConfig, load_config
from .graph_views import (
    scoped_artifact_ids,
    scoped_gate_artifact_ids,
    semantic_relation_ids,
)
from .artifact_state import (
    _artifact_content,
    _artifact_state_item,
    _graph_native_change_state_map,
    _graph_native_lifecycle_map,
)


def _change_payload(
    db: sqlite3.Connection, root: Path, change_id: str
) -> dict[str, Any]:
    change_states = _graph_native_change_state_map(root)
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (change_id,)).fetchone()
    if not row:
        raise click.ClickException(f"Change not found in graph: {change_id}")
    scope_rows = _scope_rows(
        db,
        change_id,
        load_config(root),
        class_policy=load_class_matrix_policy(root),
    )
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


def _scope_rows(
    db: sqlite3.Connection,
    change_id: str,
    config: ProjectConfig,
    *,
    view: str = "delivery",
    class_policy: ClassMatrixPolicy = EMPTY_CLASS_MATRIX,
) -> list[sqlite3.Row]:
    ids = _scope_ids(
        db, change_id, config, view=view, class_policy=class_policy
    )
    if ids:
        placeholders = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT * FROM artifacts WHERE id IN ({placeholders}) ORDER BY type, id",
            tuple(sorted(ids)),
        ).fetchall()
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (change_id,)).fetchone()
    return [row] if row else []


def _scope_ids(
    db: sqlite3.Connection,
    root_id: str,
    config: ProjectConfig,
    *,
    view: str = "delivery",
    class_policy: ClassMatrixPolicy = EMPTY_CLASS_MATRIX,
) -> set[str]:
    return scoped_artifact_ids(
        db, root_id, config, view=view, class_policy=class_policy
    )


def _gate_scope_ids(
    db: sqlite3.Connection,
    root_id: str,
    config: ProjectConfig,
    *,
    view: str = "delivery",
    class_policy: ClassMatrixPolicy = EMPTY_CLASS_MATRIX,
) -> set[str]:
    return scoped_gate_artifact_ids(
        db, root_id, config, view=view, class_policy=class_policy
    )


def _semantic_scope_relations() -> set[str]:
    """Core contract relations retained for callers without project config."""
    return {
        relation
        for relation, definition in DEFAULT_RELATIONS.items()
        if definition.get("traversal") in {"expand", "context"}
    }


def _incoming_scope_relations() -> set[str]:
    return _semantic_scope_relations()


def _evidence_profile(
    db: sqlite3.Connection, states: list[dict[str, Any]]
) -> dict[str, Any]:
    ac_ids = [item["id"] for item in states if item["type"] == "AC"]
    by_target: dict[str, list[dict[str, Any]]] = {
        artifact_id: [] for artifact_id in ac_ids
    }
    if ac_ids:
        placeholders = ",".join("?" for _ in ac_ids)
        rows = db.execute(
            "SELECT e.target_id, e.relation_type, s.id AS source_id, s.type AS source_type, "
            "s.origin AS source_origin, s.title AS source_title, s.source_file AS source_file "
            "FROM edges e JOIN artifacts s ON e.source_id = s.id "
            f"WHERE e.target_id IN ({placeholders}) "
            "AND e.relation_type = 'VERIFIES' "
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
    runtime_tests = []
    verified = []
    for artifact_id, evidence in by_target.items():
        if evidence:
            verified.append(artifact_id)
        kinds = {item["kind"] for item in evidence}
        if kinds and kinds <= {"trace"}:
            weak_only.append(artifact_id)
        if kinds & {"behavior", "runtime_test", "runtime", "manual"}:
            behavior_or_runtime.append(artifact_id)
        if "runtime_test" in kinds:
            runtime_tests.append(artifact_id)
        if kinds & {"runtime", "manual"}:
            runtime.append(artifact_id)

    return {
        "ac_total": len(ac_ids),
        "ac_verified": len(verified),
        "ac_with_behavior_or_runtime_evidence": len(behavior_or_runtime),
        "ac_with_runtime_test_definitions": len(runtime_tests),
        "ac_with_runtime_or_manual_evidence": len(runtime),
        "ac_trace_only": len(weak_only),
        "trace_only_ac": weak_only[:50],
        "sample": {
            artifact_id: evidence[:5]
            for artifact_id, evidence in list(by_target.items())[:20]
            if evidence
        },
    }


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
    } | {
        by_id.get(edge["to"], {}).get("type") for edge in edges if edge["from"] == ac_id
    }
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
        "integration": (
            "external",
            "integration",
            "provider",
            "webhook",
            "интеграц",
            "провайдер",
        ),
        "contract": ("api", "contract", "custommethod", "dto", "registry", "schema"),
    }
    for kind, markers in keyword_rules.items():
        if any(marker in text for marker in markers):
            kinds.append(kind)
    if not kinds:
        kinds.append("contract")
    return sorted(_dedupe_worklist_ids(kinds))


def _evidence_kind(row: sqlite3.Row) -> str:
    source_type = row["source_type"]
    source = f"{row['source_id']} {row['source_title']} {row['source_file']}".lower()
    if source_type == "EVD":
        if any(marker in source for marker in ("e2e", "playwright", "runtime", "acceptance")):
            return "runtime"
        return "manual"
    if any(
        marker in source
        for marker in (
            "trace.test",
            "trace-strict",
            "traceability",
            "graphba",
            "graph-ba",
        )
    ):
        return "trace"
    if source_type == "TEST":
        if any(
            marker in source
            for marker in ("e2e", "playwright", ".spec.", "acceptance")
        ):
            return "runtime_test"
        return "behavior"
    return "manual" if row["source_origin"] == "evidence" else "trace"


def _quality_axes(
    states: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    evidence_profile: dict[str, Any],
    snapshot_loaded: bool,
    *,
    target_type: str = "",
    config: ProjectConfig,
    weak_behavior_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    finding_codes = {item.get("code") for item in findings}
    fail_codes = {
        item.get("code") for item in findings if item.get("severity") == "fail"
    }
    dynamic = _is_dynamic_scope(states)
    behavior_missing = _behavior_model_missing(states, dynamic, target_type, config)
    weak_behavior_candidates = weak_behavior_candidates or []
    axes = {
        "traceability": _axis(
            "FAIL"
            if {
                "missing_trace",
                "undefined_artifact",
                "undeclared_class_edge",
                "ambiguous_class_direction",
            }
            & fail_codes
            else "PASS",
            "typed graph scope is connected and every scoped artifact is defined; weak MENTIONS are not proof",
        ),
        "implementation_proof": _axis(
            "FAIL"
            if "unimplemented" in fail_codes
            else ("PARTIAL" if "unimplemented" in finding_codes else "PASS"),
            "implemented means a typed path from observed implementation artifacts exists",
        ),
        "test_evidence": _axis(
            "FAIL"
            if "unverified" in fail_codes
            else _test_evidence_status(evidence_profile),
            _test_evidence_reason(evidence_profile),
        ),
        "behavior_model": _axis(
            "PARTIAL" if behavior_missing else "PASS",
            "dynamic behavior scopes are checked by configured semantic capabilities, not artifact type names"
            if behavior_missing
            else "required behavior capabilities are present or scope is static",
            missing=behavior_missing,
            weak_candidates=weak_behavior_candidates,
        ),
        "runtime_acceptance": _axis(
            _runtime_acceptance_status(evidence_profile),
            _runtime_acceptance_reason(evidence_profile),
        ),
        "drift": _axis(
            "UNKNOWN"
            if not snapshot_loaded
            else ("FAIL" if "stale" in fail_codes else "PASS"),
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


def _runtime_acceptance_reason(profile: dict[str, Any]) -> str:
    return (
        f"{profile.get('ac_with_runtime_or_manual_evidence', 0)}/"
        f"{profile.get('ac_total', 0)} AC have linked runtime/manual EVD; "
        f"{profile.get('ac_with_runtime_test_definitions', 0)} AC have runtime test definitions"
    )


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
        and any(
            term in f"{item['id']} {item['title']}".lower() for term in dynamic_terms
        )
        for item in states
    )


def _behavior_model_missing(
    states: list[dict[str, Any]],
    dynamic: bool,
    target_type: str,
    config: ProjectConfig,
) -> list[str]:
    if not dynamic:
        return []
    target = config.types.get(target_type)
    if not target or not (
        set(target.capabilities) & set(config.behavior_model.target_capabilities)
    ):
        return []
    present = {
        capability
        for item in states
        for capability in (
            config.types.get(item["type"]).capabilities
            if item["type"] in config.types
            else []
        )
    }
    return [
        capability
        for capability in config.behavior_model.required_capabilities
        if capability not in present
    ]


def _weak_behavior_candidates(
    db: sqlite3.Connection,
    states: list[dict[str, Any]],
    missing: list[str],
    config: ProjectConfig,
) -> list[dict[str, Any]]:
    """Find behavior artifacts reachable only through weak MENTIONS edges."""
    missing_capabilities = set(missing)
    candidate_types = {
        type_id
        for type_id, definition in config.types.items()
        if missing_capabilities & set(definition.capabilities)
    }
    scope_ids = sorted(item["id"] for item in states)
    if not candidate_types or not scope_ids:
        return []
    id_placeholders = ",".join("?" for _ in scope_ids)
    type_placeholders = ",".join("?" for _ in candidate_types)
    params = (*sorted(candidate_types), *scope_ids, *scope_ids)
    rows = db.execute(
        "SELECT DISTINCT a.id, a.type, a.title FROM artifacts a "
        "JOIN edges e ON (e.source_id = a.id OR e.target_id = a.id) "
        f"WHERE a.type IN ({type_placeholders}) AND e.relation_type = 'MENTIONS' "
        f"AND ((e.source_id = a.id AND e.target_id IN ({id_placeholders})) "
        f"OR (e.target_id = a.id AND e.source_id IN ({id_placeholders}))) "
        "ORDER BY a.type, a.id",
        params,
    ).fetchall()
    scoped = set(scope_ids)
    return [
        {
            **dict(row),
            "capabilities": sorted(
                missing_capabilities
                & set(config.types[row["type"]].capabilities)
            ),
        }
        for row in rows
        if row["id"] not in scoped
    ]


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
    config = load_config(root)
    class_policy = load_class_matrix_policy(root)
    related_ids = _scope_ids(
        db, target_id, config, view="delivery", class_policy=class_policy
    )
    if row["type"] == "CHG":
        rows = [
            row,
            *_scope_rows(db, target_id, config, class_policy=class_policy),
        ]
    else:
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
        semantic_relations = semantic_relation_ids(config, view="delivery")
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


def _worklist_priority(kind: str, source: dict[str, Any] | None) -> str:
    if source and source.get("severity") == "fail":
        return "P0"
    return {
        "add_implementation": "P0",
        "add_evidence": "P1",
        "add_required_evidence": "P1",
        "record_runtime_evidence": "P1",
        "add_trace": "P1",
        "add_behavior_capability": "P0",
        "upgrade_relation": "P0",
        "resolve_artifact": "P0",
        "add_matrix_rule": "P1",
        "fix_matrix_direction": "P0",
        "synthesize_ac": "P2",
        "refresh_acceptance": "P0",
    }.get(kind, "P2")


def _priority_rank(priority: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2}.get(priority, 9)


def _worklist_suggested_actions(
    kind: str,
    artifact: str,
    *,
    source: dict[str, Any] | None = None,
) -> list[str]:
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
    if kind == "record_runtime_evidence":
        return [
            f"run the scoped runtime/manual verification for {artifact}",
            "record the observed result as a linked EVD artifact; a TEST file alone is only a verification definition",
        ]
    if kind == "add_behavior_capability":
        return [
            f"connect artifacts that provide the missing behavior capabilities for {artifact}",
            "reuse project-native artifact classes first; introduce a new type only when no existing class owns that meaning",
        ]
    if kind == "upgrade_relation":
        return [
            f"replace weak-only MENTIONS wiring for {artifact} with a typed relation",
            "use CONTAINS for flow assembly or TRACES_TO/NORMALIZES/DEPENDS_ON for explicit semantics",
        ]
    if kind == "resolve_artifact":
        return [
            f"restore or define {artifact}",
            "if it is adapter-owned, run `graph-ba change ready <CHG-ID> --refresh-providers` before editing the contract",
        ]
    if kind == "add_matrix_rule":
        edge = (source or {}).get("edge") or {}
        if edge:
            return [
                (
                    "declare the canonical matrix edge "
                    f"{edge.get('source_type', '?')} --{edge.get('relation_type', '?')}--> "
                    f"{edge.get('target_type', '?')}"
                ),
                (
                    "add one artifact-class matrix entry for that orientation, or replace "
                    f"{edge.get('source_id', artifact)} -> {edge.get('target_id', '?')}; "
                    "do not add a reverse duplicate"
                ),
            ]
        return [
            f"review the undeclared class edge involving {artifact}",
            "add one canonical source_class/relation/target_class rule or replace the edge; do not add a reverse duplicate",
        ]
    if kind == "fix_matrix_direction":
        return [
            "choose one canonical orientation for the class pair",
            "remove reverse matrix entries and use incoming-edge queries for the reverse view; symmetric/self relations are exempt",
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
    policy = (
        gate_data.get("evidence_plan", {}).get("policy", {}).get("synthesis_policy", {})
    )
    raw_to_canonical = (
        policy.get("raw_to_canonical") if isinstance(policy, dict) else None
    )
    if not isinstance(raw_to_canonical, dict):
        return ""
    raw_types = set(
        raw_to_canonical.get("raw_types") or raw_to_canonical.get("source_types") or []
    )
    canonical_types = set(
        raw_to_canonical.get("canonical_types")
        or [raw_to_canonical.get("output_type", "AC")]
    )
    relation = str(raw_to_canonical.get("required_relation") or "NORMALIZES")
    if node.get("type") not in raw_types:
        return ""
    has_canonical = any(
        edge["relation"] == relation
        and by_id.get(edge["from"], {}).get("type") in canonical_types
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
        for marker in (
            "button",
            "dialog",
            "form",
            "input",
            "action",
            "confirm",
            "modal",
        )
    ):
        return True
    return node.get("type") == "UIC"


def _has_outgoing_to_capability(
    artifact: str,
    relation: str,
    target_capability: str,
    outgoing: dict[str, list[dict[str, Any]]],
    by_id: dict[str, dict[str, Any]],
) -> bool:
    return any(
        edge["relation"] == relation
        and target_capability
        in by_id.get(edge["to"], {}).get("capabilities", [])
        for edge in outgoing.get(artifact, [])
    )


def _has_reachable_capability(
    artifact: str,
    target_capability: str,
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
            if target_capability in by_id.get(target, {}).get("capabilities", []):
                return True
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return False

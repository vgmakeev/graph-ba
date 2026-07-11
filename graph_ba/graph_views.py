"""Policy-driven projections over the full traceability knowledge graph.

The database intentionally keeps every discovered artifact and edge. Agent and
gate views must not use the same unbounded traversal: contract semantics expand,
context is attached without cascading, proof is summarized, and navigation is
opt-in.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .class_matrix import ClassMatrixPolicy, EMPTY_CLASS_MATRIX
from .config import ProjectConfig


GRAPH_VIEWS = {"contract", "delivery", "navigation", "full"}

PROOF_ORIGINS = {"evidence", "implementation"}
SOURCE_ORIGINS = {"human", "human_primary", "refined_human", "source"}
NAVIGATION_TYPES = {"FILE", "LINK"}


def artifact_view_role(
    config: ProjectConfig,
    artifact_type: str,
    origin: str | None,
) -> str:
    """Classify one stored artifact for graph projection purposes."""
    normalized_origin = (origin or "").lower()
    configured = config.types.get(artifact_type)
    if configured and configured.view_role:
        return configured.view_role
    # A project can keep semantic bridge identities in the contract view even
    # when their current definition is emitted by an implementation adapter.
    if configured and {"screen", "ui_zone", "contract"} & set(
        configured.capabilities
    ):
        return "contract"
    if normalized_origin in PROOF_ORIGINS:
        return "proof"
    if normalized_origin == "container" or artifact_type in NAVIGATION_TYPES:
        return "navigation"
    if artifact_type == config.graph_native.change_type or artifact_type == "CHG":
        return "change"
    if normalized_origin in {
        "canonical",
        "human_designed",
    }:
        return "contract"
    if normalized_origin in SOURCE_ORIGINS:
        return "source"
    return "context"


def relation_view_policy(config: ProjectConfig, relation: str) -> dict[str, str]:
    """Return the role and traversal mode for a relation id."""
    definition = config.relation_types.get(relation)
    if definition is None:
        return {"role": "semantic", "traversal": "context"}
    return {
        "role": definition.role or "semantic",
        "traversal": definition.traversal or "context",
    }


def scoped_artifact_ids(
    db: sqlite3.Connection,
    root_id: str,
    config: ProjectConfig,
    *,
    view: str = "delivery",
    navigation_limit: int = 200,
    class_policy: ClassMatrixPolicy = EMPTY_CLASS_MATRIX,
) -> set[str]:
    """Project a bounded semantic scope from the full knowledge graph."""
    if view not in GRAPH_VIEWS:
        raise ValueError(f"unknown graph view: {view}")
    if view == "full":
        return _full_scope_ids(db, root_id)

    result = {root_id}
    seen = {root_id}
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        for edge in _outgoing_edges(db, current):
            rule = class_policy.rule_for(
                edge["source_type"], edge["relation_type"], edge["target_type"]
            )
            if class_policy.enforce and rule is None:
                continue
            policy = relation_view_policy(config, edge["relation_type"])
            traversal = rule.traversal if rule and rule.traversal else policy["traversal"]
            if traversal in {"hidden", "terminal"}:
                continue
            target_id = edge["target_id"]
            target_role = artifact_view_role(
                config, edge["target_type"], edge["target_origin"]
            )
            if target_role in {"proof", "navigation"}:
                continue
            if view != "navigation" and target_role in {"source", "context"}:
                continue
            result.add(target_id)
            if traversal == "expand" and target_id not in seen:
                seen.add(target_id)
                queue.append(target_id)

        # Contract owners may point at a target rather than be contained by it.
        # Only attach semantic ancestors to the selected root;
        # recursively following every incoming trace recreates the old graph
        # explosion through shared AC and implementation facts.
        if current == root_id:
            for edge in _incoming_edges(db, current):
                rule = class_policy.rule_for(
                    edge["source_type"], edge["relation_type"], edge["target_type"]
                )
                if class_policy.enforce and rule is None:
                    continue
                policy = relation_view_policy(config, edge["relation_type"])
                traversal = rule.traversal if rule and rule.traversal else policy["traversal"]
                if traversal not in {"expand", "context"}:
                    continue
                source_role = artifact_view_role(
                    config, edge["source_type"], edge["source_origin"]
                )
                if source_role not in {"contract", "change"}:
                    continue
                source_id = edge["source_id"]
                result.add(source_id)
                if source_id not in seen:
                    seen.add(source_id)
                    queue.append(source_id)

    if view == "navigation":
        result.update(_navigation_neighbors(db, result, config, navigation_limit))
    return result


def semantic_relation_ids(config: ProjectConfig, *, view: str) -> set[str]:
    """Relations rendered as explicit edges for a selected projection."""
    if view == "full":
        return set(config.relation_types)
    allowed = {
        relation
        for relation in config.relation_types
        if relation_view_policy(config, relation)["traversal"]
        in {"expand", "context"}
    }
    if view == "navigation":
        allowed.update(
            relation
            for relation in config.relation_types
            if relation_view_policy(config, relation)["role"] == "navigation"
        )
    return allowed


def _outgoing_edges(db: sqlite3.Connection, artifact_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT e.target_id, e.relation_type, s.type AS source_type, "
        "t.type AS target_type, t.origin AS target_origin FROM edges e "
        "JOIN artifacts s ON s.id = e.source_id "
        "JOIN artifacts t ON t.id = e.target_id "
        "WHERE e.source_id = ? ORDER BY e.relation_type, e.target_id",
        (artifact_id,),
    ).fetchall()


def _incoming_edges(db: sqlite3.Connection, artifact_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT e.source_id, e.relation_type, s.type AS source_type, "
        "s.origin AS source_origin, t.type AS target_type FROM edges e "
        "JOIN artifacts s ON s.id = e.source_id "
        "JOIN artifacts t ON t.id = e.target_id "
        "WHERE e.target_id = ? ORDER BY e.relation_type, e.source_id",
        (artifact_id,),
    ).fetchall()


def _navigation_neighbors(
    db: sqlite3.Connection,
    semantic_ids: set[str],
    config: ProjectConfig,
    limit: int,
) -> set[str]:
    if not semantic_ids or limit <= 0:
        return set()
    navigation_relations = sorted(
        relation
        for relation in config.relation_types
        if relation_view_policy(config, relation)["role"] == "navigation"
    )
    if not navigation_relations:
        return set()
    ids = sorted(semantic_ids)
    id_placeholders = ",".join("?" for _ in ids)
    relation_placeholders = ",".join("?" for _ in navigation_relations)
    rows = db.execute(
        "SELECT source_id, target_id FROM edges "
        f"WHERE relation_type IN ({relation_placeholders}) AND "
        f"(source_id IN ({id_placeholders}) OR target_id IN ({id_placeholders})) "
        "ORDER BY source_id, target_id LIMIT ?",
        (*navigation_relations, *ids, *ids, limit),
    ).fetchall()
    return {
        neighbor
        for row in rows
        for neighbor in (row["source_id"], row["target_id"])
        if neighbor not in semantic_ids
    }


def _full_scope_ids(db: sqlite3.Connection, root_id: str) -> set[str]:
    """Explicit opt-in legacy/full connected-component traversal."""
    result = {root_id}
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        rows = db.execute(
            "SELECT source_id, target_id FROM edges "
            "WHERE source_id = ? OR target_id = ? ORDER BY source_id, target_id",
            (current, current),
        ).fetchall()
        for row in rows:
            neighbor = row["target_id"] if row["source_id"] == current else row["source_id"]
            if neighbor in result:
                continue
            result.add(neighbor)
            queue.append(neighbor)
    return result

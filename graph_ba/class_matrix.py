"""Executable artifact-class edge policy for project graph views and gates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import sqlite3


@dataclass(frozen=True)
class ClassEdgeRule:
    source_type: str
    relation: str
    target_type: str
    traversal: str = ""
    producer: str = ""

    def matches(self, source_type: str, relation: str, target_type: str) -> bool:
        return (
            self.source_type in {"*", source_type}
            and self.relation in {"*", relation}
            and self.target_type in {"*", target_type}
        )


@dataclass(frozen=True)
class ClassMatrixPolicy:
    enforce: bool
    rules: tuple[ClassEdgeRule, ...]
    sources: tuple[str, ...] = ()
    preferred_directions: tuple[tuple[str, str], ...] = ()
    disabled_rules: tuple[ClassEdgeRule, ...] = ()

    def rule_for(
        self, source_type: str, relation: str, target_type: str
    ) -> ClassEdgeRule | None:
        matches = [
            rule
            for rule in self.rules
            if rule.matches(source_type, relation, target_type)
        ]
        if not matches:
            return None
        return max(
            matches,
            key=lambda rule: sum(
                value != "*"
                for value in (rule.source_type, rule.relation, rule.target_type)
            ),
        )

    def allows(self, source_type: str, relation: str, target_type: str) -> bool:
        return not self.enforce or self.rule_for(source_type, relation, target_type) is not None

    def suppresses_reverse(self, source_type: str, target_type: str) -> bool:
        """Whether project policy deliberately selected the opposite orientation."""
        return (target_type, source_type) in self.preferred_directions


EMPTY_CLASS_MATRIX = ClassMatrixPolicy(False, ())
SYMMETRIC_RELATIONS = {"CONFLICTS_WITH"}


def load_class_matrix_policy(root: Path) -> ClassMatrixPolicy:
    candidates = [
        root / ".graphba" / "artifact-class-matrix.json",
        root / "reports" / "graphba" / "mini-artifact-class-matrix.json",
    ]
    rules: list[ClassEdgeRule] = []
    sources: list[str] = []
    preferred_directions: list[tuple[str, str]] = []
    enforce = False
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
        enforce = enforce or bool(payload.get("enforce") or policy.get("enforce"))
        directions = policy.get("class_directions")
        if isinstance(directions, list):
            for direction in directions:
                if not isinstance(direction, dict):
                    continue
                source_type = str(direction.get("source_type") or "")
                target_type = str(direction.get("target_type") or "")
                if source_type and target_type and source_type != target_type:
                    preferred_directions.append((source_type, target_type))
        entries = payload.get("entries")
        if not isinstance(entries, list):
            continue
        sources.append(str(path.relative_to(root)))
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source_type = str(entry.get("source_type") or "")
            relation = str(entry.get("relation") or "")
            target_type = str(entry.get("target_type") or "")
            if not source_type or not relation or not target_type:
                continue
            rules.append(
                ClassEdgeRule(
                    source_type=source_type,
                    relation=relation,
                    target_type=target_type,
                    traversal=str(entry.get("traversal") or ""),
                    producer=str(entry.get("producer") or ""),
                )
            )
    preferred_by_pair = {
        tuple(sorted(direction)): direction for direction in preferred_directions
    }
    active_rules: list[ClassEdgeRule] = []
    disabled_rules: list[ClassEdgeRule] = []
    for rule in rules:
        if (
            "*" in {rule.source_type, rule.target_type}
            or rule.source_type == rule.target_type
            or rule.relation in SYMMETRIC_RELATIONS
        ):
            active_rules.append(rule)
            continue
        preferred = preferred_by_pair.get(
            tuple(sorted((rule.source_type, rule.target_type)))
        )
        if preferred and (rule.source_type, rule.target_type) != preferred:
            disabled_rules.append(rule)
        else:
            active_rules.append(rule)
    return ClassMatrixPolicy(
        enforce,
        tuple(active_rules),
        tuple(sources),
        tuple(sorted(set(preferred_directions))),
        tuple(disabled_rules),
    )


def class_matrix_summary(policy: ClassMatrixPolicy) -> dict[str, Any]:
    return {
        "enforce": policy.enforce,
        "rules": len(policy.rules),
        "sources": list(policy.sources),
        "direction": "declared source -> target; reverse views query incoming edges",
        "direction_conflicts": class_direction_conflicts(policy),
        "preferred_directions": [
            {"source_type": source, "target_type": target}
            for source, target in policy.preferred_directions
        ],
        "disabled_reverse_rules": len(policy.disabled_rules),
    }


def class_direction_conflicts(
    policy: ClassMatrixPolicy,
) -> list[dict[str, Any]]:
    """Reject opposing orientations for one unordered pair of artifact classes."""
    by_pair: dict[tuple[str, str], dict[tuple[str, str], list[str]]] = {}
    for rule in policy.rules:
        if (
            "*" in {rule.source_type, rule.target_type}
            or rule.source_type == rule.target_type
            or rule.relation in SYMMETRIC_RELATIONS
        ):
            continue
        pair = tuple(sorted((rule.source_type, rule.target_type)))
        orientation = (rule.source_type, rule.target_type)
        by_pair.setdefault(pair, {}).setdefault(orientation, []).append(rule.relation)
    conflicts = []
    for pair, orientations in sorted(by_pair.items()):
        if len(orientations) < 2:
            continue
        conflicts.append(
            {
                "classes": list(pair),
                "orientations": [
                    {
                        "source_type": source,
                        "target_type": target,
                        "relations": sorted(set(relations)),
                    }
                    for (source, target), relations in sorted(orientations.items())
                ],
            }
        )
    return conflicts


def undeclared_class_edges(
    db: sqlite3.Connection,
    artifact_ids: set[str],
    policy: ClassMatrixPolicy,
    *,
    limit: int = 100,
) -> list[dict[str, str]]:
    if not policy.enforce or not artifact_ids:
        return []
    ids = sorted(artifact_ids)
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        "SELECT e.source_id, s.type AS source_type, e.relation_type, "
        "e.target_id, t.type AS target_type FROM edges e "
        "JOIN artifacts s ON s.id = e.source_id "
        "JOIN artifacts t ON t.id = e.target_id "
        f"WHERE e.source_id IN ({placeholders}) "
        "AND e.relation_type NOT IN ('MENTIONS', 'INDEX', 'CANDIDATE_TRACE') "
        "ORDER BY s.type, e.relation_type, t.type, e.source_id, e.target_id",
        tuple(ids),
    ).fetchall()
    result = []
    for row in rows:
        if policy.suppresses_reverse(row["source_type"], row["target_type"]):
            # Adapters may still emit both navigational orientations. The
            # project-selected direction is authoritative for delivery views
            # and gates; the suppressed reverse remains available in `full`.
            continue
        if policy.allows(
            row["source_type"], row["relation_type"], row["target_type"]
        ):
            continue
        result.append(dict(row))
        if len(result) >= limit:
            break
    return result

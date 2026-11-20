"""Query a bounded, typed trace and the latest observed run of each test."""

from __future__ import annotations

import sqlite3
from collections import Counter, deque

from .provider_graph import TEST_STATUSES

DIAGNOSTIC_RELATIONS = {"MENTIONS", "INDEX", "CANDIDATE_TRACE", "TRACE_GAP"}
DIRECTIONS = {
    "outgoing": ("source_id",),
    "incoming": ("target_id",),
    "both": ("source_id", "target_id"),
}
MAX_TRACE_DEPTH = 10


def trace_artifact(
    db: sqlite3.Connection,
    artifact_id: str,
    *,
    direction: str = "both",
    depth: int = 3,
    relations: set[str] | None = None,
    commit: str | None = None,
) -> dict:
    """Follow typed edges, preserving their original direction and provenance."""
    if direction not in DIRECTIONS:
        raise ValueError("direction must be incoming, outgoing or both")
    if not 0 <= depth <= MAX_TRACE_DEPTH:
        raise ValueError(f"depth must be between 0 and {MAX_TRACE_DEPTH}")

    catalog = {row["id"] for row in db.execute("SELECT id FROM relation_types")}
    selected = relations if relations is not None else catalog - DIAGNOSTIC_RELATIONS
    unknown = selected - catalog
    if unknown:
        raise ValueError(f"Unknown relation types: {', '.join(sorted(unknown))}")
    root = _artifact(db, artifact_id)
    if root is None:
        raise ValueError(f"Node '{artifact_id}' not found")

    nodes = {artifact_id: root}
    edges = {}
    queue = deque([(artifact_id, 0)])
    placeholders = ", ".join("?" for _ in selected)
    relation_params = sorted(selected)
    while queue and selected:
        node_id, hops = queue.popleft()
        if hops == depth or not nodes[node_id]["defined"]:
            continue
        for field in DIRECTIONS[direction]:
            rows = db.execute(
                f"SELECT * FROM edges WHERE {field} = ? "
                f"AND relation_type IN ({placeholders})",
                (node_id, *relation_params),
            )
            for edge in rows:
                edges[tuple(edge)] = dict(edge)
                other_id = edge["target_id" if field == "source_id" else "source_id"]
                if other_id not in nodes:
                    other = _artifact(db, other_id)
                    if other is not None:
                        nodes[other_id] = other
                        queue.append((other_id, hops + 1))

    test_targets: dict[str, set[str]] = {
        node["id"]: set() for node in nodes.values()
        if node["type"] == "TEST" and node["defined"]
    }
    for edge in edges.values():
        targets = test_targets.get(edge["source_id"])
        if targets is not None and edge["relation_type"] == "VERIFIES":
            targets.add(edge["target_id"])

    tests = []
    for test_id, targets in sorted(test_targets.items()):
        run = _latest_test_run(db, test_id, commit)
        tests.append({
            "test_id": test_id,
            "level": nodes[test_id]["test_level"] or None,
            "verifies": sorted(targets),
            "status": run["status"] if run else "not_run",
            "latest_run": run,
        })
    counts = Counter(test["status"] for test in tests if test["verifies"])
    return {
        "schema": "graph-ba.trace.v1",
        "root": artifact_id,
        "direction": direction,
        "depth": depth,
        "relations": relation_params,
        "commit": commit,
        "nodes": [nodes[node_id] for node_id in sorted(nodes)],
        "edges": [edge for _, edge in sorted(edges.items())],
        "tests": tests,
        "test_status_counts": {status: counts[status] for status in (*TEST_STATUSES, "not_run")},
    }


def _artifact(db: sqlite3.Connection, node_id: str) -> dict | None:
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (node_id,)).fetchone()
    return {**dict(row), "defined": bool(row["defined"])} if row is not None else None


def _latest_test_run(db: sqlite3.Connection, test_id: str, commit: str | None) -> dict | None:
    where = 'test_id = ? AND "commit" = ?' if commit is not None else "test_id = ?"
    params = (test_id, commit) if commit is not None else (test_id,)
    row = db.execute(
        f"SELECT * FROM test_runs WHERE {where} "
        "ORDER BY finished_at DESC, provider, run_id DESC LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row is not None else None

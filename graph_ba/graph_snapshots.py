"""Git-backed graph views and bounded semantic impact."""
from __future__ import annotations

import io
import hashlib
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from graph_ba.db import do_import, get_db
from graph_ba import __version__
from graph_ba.db import SCHEMA_VERSION


CONTRACT_EXCLUDED_ORIGINS = {"implementation", "evidence"}
CONTRACT_EXCLUDED_PREFIXES = ("CODE:", "FILE:", "TEST:", "UI:")
IMPACT_RELATIONS = {
    "CONTAINS",
    "DEPENDS_ON",
    "IMPLEMENTS",
    "NORMALIZES",
    "RENDERS",
    "TRACES_TO",
    "VERIFIES",
}


class GraphSnapshotError(RuntimeError):
    """Raised when a Git graph snapshot cannot be built."""


@dataclass
class GraphViews:
    base: sqlite3.Connection
    proposed: sqlite3.Connection
    delivery: sqlite3.Connection
    base_root: Path
    proposed_root: Path


@contextmanager
def graph_views(
    root: Path,
    proposed_db: sqlite3.Connection,
    base_commit: str,
) -> Iterator[GraphViews]:
    """Build an accepted base graph and overlay logical proposed/delivery views."""
    with tempfile.TemporaryDirectory(prefix="graph-ba-view-") as temp_dir:
        base_root = Path(temp_dir) / "base"
        base_root.mkdir()
        base_db = get_db(Path(temp_dir) / "base.db")
        try:
            cache_path = _base_graph_cache_path(root, base_commit)
            if cache_path.is_file():
                _restore_database(cache_path, base_db)
            else:
                _materialize_git_tree(root, base_commit, base_root)
                if not (base_root / "graph-ba.toml").is_file():
                    raise GraphSnapshotError(f"graph-ba.toml does not exist at {base_commit}")
                do_import(base_root, base_db, quiet=True, force=True)
                _cache_database(base_db, cache_path)
            yield GraphViews(
                base=base_db,
                proposed=proposed_db,
                delivery=proposed_db,
                base_root=base_root,
                proposed_root=root,
            )
        finally:
            base_db.close()


def _base_graph_cache_path(root: Path, base_commit: str) -> Path:
    """Return a user-local cache path bound to repo, commit and graph schema."""
    cache_home = Path(
        os.environ.get("XDG_CACHE_HOME")
        or (Path.home() / ".cache")
    )
    repo_key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    version_key = f"graph-ba-{__version__}-db-{SCHEMA_VERSION}"
    return cache_home / "graph-ba" / "base-graphs" / repo_key / version_key / f"{base_commit}.db"


def _restore_database(source_path: Path, destination: sqlite3.Connection) -> None:
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        source.backup(destination)
    finally:
        source.close()


def _cache_database(source: sqlite3.Connection, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    finally:
        target.close()
    temporary.replace(target_path)


def graph_delta(
    views: GraphViews,
    semantic_change: dict[str, Any],
) -> dict[str, Any]:
    """Compare accepted and proposed contract nodes and typed edges."""
    base_nodes = _contract_nodes(views.base)
    proposed_nodes = _contract_nodes(views.proposed)
    semantic_by_id = {item["id"]: item for item in semantic_change.get("artifacts", [])}

    added_ids = sorted(set(proposed_nodes) - set(base_nodes))
    removed_ids = sorted(set(base_nodes) - set(proposed_nodes))
    modified_ids = sorted(
        artifact_id
        for artifact_id in set(base_nodes) & set(proposed_nodes)
        if artifact_id in semantic_by_id
        and semantic_by_id[artifact_id].get("operation") == "modify"
    )
    node_changes = [
        _node_change("add", artifact_id, None, proposed_nodes[artifact_id])
        for artifact_id in added_ids
    ]
    node_changes.extend(
        _node_change("modify", artifact_id, base_nodes[artifact_id], proposed_nodes[artifact_id])
        for artifact_id in modified_ids
    )
    node_changes.extend(
        _node_change("remove", artifact_id, base_nodes[artifact_id], None)
        for artifact_id in removed_ids
    )

    base_edges = _contract_edges(views.base, set(base_nodes))
    proposed_edges = _contract_edges(views.proposed, set(proposed_nodes))
    edge_added = sorted(proposed_edges - base_edges)
    edge_removed = sorted(base_edges - proposed_edges)
    return {
        "schema": "graph-ba.graph-delta.v1",
        "base_commit": semantic_change["git"]["base_commit"],
        "proposal_fingerprint": semantic_change["proposal_fingerprint"],
        "summary": {
            "nodes_added": len(added_ids),
            "nodes_modified": len(modified_ids),
            "nodes_removed": len(removed_ids),
            "edges_added": len(edge_added),
            "edges_removed": len(edge_removed),
        },
        "nodes": node_changes,
        "edges": [
            {"operation": "add", "source": edge[0], "relation": edge[1], "target": edge[2]}
            for edge in edge_added
        ] + [
            {"operation": "remove", "source": edge[0], "relation": edge[1], "target": edge[2]}
            for edge in edge_removed
        ],
    }


def impact_paths(
    views: GraphViews,
    graph_change: dict[str, Any],
    *,
    scope_hints: tuple[str, ...] = (),
    max_depth: int = 3,
    max_nodes: int = 200,
    max_neighbors: int = 40,
) -> dict[str, Any]:
    """Traverse a bounded union of base and delivery graphs with path evidence."""
    changed = {item["id"] for item in graph_change.get("nodes", [])}
    for edge in graph_change.get("edges", []):
        if edge.get("operation") != "add":
            continue
        if edge.get("source"):
            changed.add(edge["source"])
        if edge.get("target"):
            changed.add(edge["target"])
    seeds = sorted(changed | set(scope_hints))
    base_nodes = _all_nodes(views.base)
    delivery_nodes = _all_nodes(views.delivery)
    adjacency = _union_adjacency(views.base, views.delivery)
    paths: dict[str, dict[str, Any]] = {}
    queue: list[tuple[str, list[dict[str, str]], int]] = [
        (seed, [], 0) for seed in seeds
    ]
    seen = set(seeds)
    truncated = False

    for seed in seeds:
        node = delivery_nodes.get(seed) or base_nodes.get(seed)
        if node:
            paths[seed] = {**node, "reason": "changed", "path": [], "depth": 0}

    while queue:
        current, path, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        neighbors = adjacency.get(current, [])
        if len(neighbors) > max_neighbors:
            neighbors = neighbors[:max_neighbors]
            truncated = True
        for edge in neighbors:
            target = edge["to"]
            if target in seen:
                continue
            seen.add(target)
            next_path = [*path, edge]
            node = delivery_nodes.get(target) or base_nodes.get(target)
            if not node:
                continue
            paths[target] = {
                **node,
                "reason": "typed_impact_path",
                "path": next_path,
                "depth": depth + 1,
            }
            if len(paths) >= max_nodes:
                truncated = True
                queue.clear()
                break
            if node["origin"] not in {"implementation", "evidence"}:
                queue.append((target, next_path, depth + 1))

    ordered = sorted(paths.values(), key=lambda item: (item["depth"], item["type"], item["id"]))
    return {
        "schema": "graph-ba.impact-paths.v1",
        "proposal_fingerprint": graph_change["proposal_fingerprint"],
        "seeds": seeds,
        "limits": {
            "max_depth": max_depth,
            "max_nodes": max_nodes,
            "max_neighbors": max_neighbors,
        },
        "summary": {
            "nodes": len(ordered),
            "implementation": sum(item["origin"] == "implementation" for item in ordered),
            "evidence": sum(item["origin"] == "evidence" for item in ordered),
            "removed_seeds": sum(
                item.get("operation") == "remove" for item in graph_change.get("nodes", [])
            ),
            "truncated": truncated,
        },
        "nodes": ordered,
    }


def _materialize_git_tree(root: Path, commit: str, target: Path) -> None:
    result = subprocess.run(
        ["git", "archive", "--format=tar", commit],
        cwd=root,
        capture_output=True,
    )
    if result.returncode != 0:
        raise GraphSnapshotError(
            result.stderr.decode("utf-8", errors="replace").strip() or "git archive failed"
        )
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise GraphSnapshotError(f"unsafe path in Git archive: {member.name}")
            destination = target / member_path
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read())


def _contract_nodes(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        row["id"]: dict(row)
        for row in db.execute("SELECT * FROM artifacts ORDER BY id").fetchall()
        if _is_contract_node(
            row["id"], row["type"], row["origin"], bool(row["defined"])
        )
    }


def _all_nodes(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {row["id"]: dict(row) for row in db.execute("SELECT * FROM artifacts").fetchall()}


def _is_contract_node(
    artifact_id: str,
    artifact_type: str,
    origin: str,
    defined: bool,
) -> bool:
    return (
        defined
        and artifact_type not in {"CHG", "UNKNOWN"}
        and
        origin not in CONTRACT_EXCLUDED_ORIGINS
        and not artifact_id.startswith(CONTRACT_EXCLUDED_PREFIXES)
    )


def _contract_edges(
    db: sqlite3.Connection,
    node_ids: set[str],
) -> set[tuple[str, str, str]]:
    return {
        (row["source_id"], row["relation_type"], row["target_id"])
        for row in db.execute(
            "SELECT source_id, target_id, relation_type FROM edges "
            "WHERE relation_type != 'MENTIONS'"
        ).fetchall()
        if row["source_id"] in node_ids and row["target_id"] in node_ids
    }


def _union_adjacency(
    base: sqlite3.Connection,
    delivery: sqlite3.Connection,
) -> dict[str, list[dict[str, str]]]:
    adjacency: dict[str, list[dict[str, str]]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for view, db in (("base", base), ("delivery", delivery)):
        placeholders = ",".join("?" for _ in IMPACT_RELATIONS)
        rows = db.execute(
            "SELECT source_id, target_id, relation_type FROM edges "
            f"WHERE relation_type IN ({placeholders}) ORDER BY source_id, target_id",
            tuple(sorted(IMPACT_RELATIONS)),
        ).fetchall()
        for row in rows:
            for direction, current, target in (
                ("outgoing", row["source_id"], row["target_id"]),
                ("incoming", row["target_id"], row["source_id"]),
            ):
                key = (current, row["relation_type"], target, direction)
                if key in seen:
                    continue
                seen.add(key)
                adjacency.setdefault(current, []).append({
                    "from": current,
                    "relation": row["relation_type"],
                    "to": target,
                    "direction": direction,
                    "view": view,
                })
    return adjacency


def _node_change(
    operation: str,
    artifact_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    current = after or before or {}
    result = {
        "operation": operation,
        "id": artifact_id,
        "type": current.get("type", "?"),
        "origin": current.get("origin", ""),
    }
    if before:
        result["before"] = before
    if after:
        result["after"] = after
    return result

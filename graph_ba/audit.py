"""Global graph audit logic."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List

from graph_ba.db import _load_nx


def _is_meta_node(node_id: str) -> bool:
    """Check if node is a meta-node rather than a BA artifact."""
    return (node_id.startswith("FILE:") or node_id.startswith("CODE:")
            or node_id.startswith("TEST:") or node_id.startswith("UI:"))

def _issue_fingerprints(issue: dict) -> List[str]:
    """Stable fingerprint strings for an audit issue (for baseline ratchet)."""
    t = issue["type"]
    if t == "DANGLING":
        return [f"DANGLING:{issue['id']}"]
    if t == "CYCLE":
        ids = sorted(issue["ids"])
        return [f"CYCLE:{len(ids)}:{','.join(ids[:3])}"]
    if t == "BRIDGE":
        u, v = sorted(issue["ids"])
        return [f"BRIDGE:{u}|{v}"]
    if t == "BOTTLENECK":
        return [f"BOTTLENECK:{issue['id']}"]
    if t == "COVERAGE_GAP":
        return [f"COVERAGE_GAP:{issue['source']}:{issue['target']}:{mid}"
                for mid in issue["missing"]]
    if t == "MISSING_CROSS_LAYER":
        return [f"MISSING_CROSS_LAYER:{issue['id']}:{issue['expected']}"]
    if t == "MISSING_BIDIR":
        return [f"MISSING_BIDIR:{issue['id']}:{issue['target']}"]
    # Unknown issue type: fall back to a canonical JSON dump
    return [f"{t}:{json.dumps(issue, sort_keys=True, ensure_ascii=False, default=str)}"]


def run_coverage(db: sqlite3.Connection, config) -> dict:
    pairs = [(cp.source, cp.target) for cp in config.coverage_pairs]
    results = []
    for src_type, tgt_type in pairs:
        total = db.execute(
            "SELECT count(*) as c FROM artifacts WHERE type = ? AND defined = 1",
            (src_type,)
        ).fetchone()["c"]
        linked = db.execute("""
            SELECT count(DISTINCT a.id) as c
            FROM artifacts a
            WHERE a.type = ? AND a.defined = 1
              AND (EXISTS (
                  SELECT 1 FROM edges e JOIN artifacts a2 ON e.target_id = a2.id
                  WHERE e.source_id = a.id AND a2.type = ?
              ) OR EXISTS (
                  SELECT 1 FROM edges e JOIN artifacts a2 ON e.source_id = a2.id
                  WHERE e.target_id = a.id AND a2.type = ?
              ))
        """, (src_type, tgt_type, tgt_type)).fetchone()["c"]
        pct = (linked / total * 100) if total else 0
        status = "OK" if pct >= 90 else "WARN" if pct >= 50 else "GAP"
        results.append({"source": src_type, "target": tgt_type,
                        "linked": linked, "total": total,
                        "pct": round(pct, 1), "status": status})

    def _meta_coverage(art_types, prefix):
        out = []
        for art_type in art_types:
            total = db.execute(
                "SELECT count(*) as c FROM artifacts WHERE type = ? AND defined = 1",
                (art_type,)
            ).fetchone()["c"]
            linked = db.execute("""
                SELECT count(DISTINCT a.id) as c FROM artifacts a
                WHERE a.type = ? AND a.defined = 1
                AND EXISTS (
                    SELECT 1 FROM edges e
                    WHERE e.target_id = a.id AND e.source_id LIKE ?
                )
            """, (art_type, f"{prefix}%")).fetchone()["c"]
            pct = (linked / total * 100) if total else 0
            status = "OK" if pct >= 90 else "WARN" if pct >= 50 else "GAP"
            out.append({"type": art_type, "linked": linked,
                        "total": total, "pct": round(pct, 1),
                        "status": status})
        return out

    return {
        "pairs": results,
        "code_coverage": _meta_coverage(config.code.coverage_types, "CODE:")
        if config.code and config.code.coverage_types else [],
        "test_coverage": _meta_coverage(config.tests.coverage_types, "TEST:")
        if config.tests and config.tests.coverage_types else [],
        "ui_coverage": _meta_coverage(config.ui.coverage_types, "UI:")
        if config.ui and config.ui.coverage_types else [],
    }


def run_anomalies(db: sqlite3.Connection, min_component: int = 2) -> dict:
    import networkx as nx

    G = _load_nx(db)
    issues = []
    U = G.to_undirected()
    components = list(nx.connected_components(U))
    if len(components) > 1:
        components.sort(key=len, reverse=True)
        main_size = len(components[0])
        islands = [c for c in components[1:] if len(c) >= min_component]
        if islands:
            issues.append(("ISLAND", f"{len(islands)} disconnected component(s) "
                          f"(main: {main_size} nodes)"))
            for i, comp in enumerate(islands[:10], 1):
                nodes = sorted(comp)[:10]
                suffix = f" ... +{len(comp)-10}" if len(comp) > 10 else ""
                issues.append(("ISLAND", f"  Component {i} ({len(comp)} nodes): "
                              f"{', '.join(nodes)}{suffix}"))

    sccs = [c for c in nx.strongly_connected_components(G) if len(c) > 1]
    if sccs:
        issues.append(("CYCLE", f"{len(sccs)} cycle(s) found"))
        for scc in sccs[:5]:
            nodes = sorted(scc)
            issues.append(("CYCLE", f"  Cycle: {' → '.join(nodes[:8])}"
                          f"{' → ...' if len(nodes) > 8 else ''}"))

    sources = [n for n in G.nodes() if G.in_degree(n) == 0
               and not _is_meta_node(n) and G.nodes[n].get("defined")]
    if sources:
        by_type: dict = {}
        for n in sources:
            by_type.setdefault(G.nodes[n].get("type", "?"), []).append(n)
        issues.append(("ROOT", f"{len(sources)} root node(s) (no incoming edges)"))
        for t, ids in sorted(by_type.items()):
            issues.append(("ROOT", f"  [{t}] ({len(ids)}): {', '.join(sorted(ids)[:10])}"))

    sinks = [n for n in G.nodes() if G.out_degree(n) == 0
             and not _is_meta_node(n) and G.nodes[n].get("defined")]
    if sinks:
        by_type = {}
        for n in sinks:
            by_type.setdefault(G.nodes[n].get("type", "?"), []).append(n)
        issues.append(("SINK", f"{len(sinks)} sink node(s) (no outgoing edges)"))
        for t, ids in sorted(by_type.items()):
            issues.append(("SINK", f"  [{t}] ({len(ids)}): {', '.join(sorted(ids)[:10])}"))

    dangling = [n for n in G.nodes() if not G.nodes[n].get("defined", False)
                and not _is_meta_node(n)]
    if dangling:
        issues.append(("DANGLING", f"{len(dangling)} dangling reference(s) (not defined)"))
        for n in sorted(dangling)[:15]:
            preds = sorted(G.predecessors(n))[:3]
            issues.append(("DANGLING", f"  {n} ← referenced by: {', '.join(preds)}"))

    try:
        bridges = list(nx.bridges(U))
        if bridges:
            issues.append(("BRIDGE", f"{len(bridges)} bridge edge(s) (critical connections)"))
            for u, v in bridges[:10]:
                issues.append(("BRIDGE", f"  {u} — {v}"))
    except nx.NetworkXError:
        pass

    threshold = max(10, G.number_of_edges() // G.number_of_nodes() * 3) if G.number_of_nodes() > 0 else 10
    bottlenecks = [(n, G.in_degree(n) + G.out_degree(n)) for n in G.nodes()
                   if G.in_degree(n) + G.out_degree(n) > threshold
                   and not _is_meta_node(n)]
    bottlenecks.sort(key=lambda x: -x[1])
    if bottlenecks:
        issues.append(("BOTTLENECK", f"{len(bottlenecks)} high-degree node(s) (degree > {threshold})"))
        for n, deg in bottlenecks[:10]:
            issues.append(("BOTTLENECK", f"  [{G.nodes[n].get('type', '?')}] {n} degree={deg}"))

    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "issues": [{"type": cat, "message": msg} for cat, msg in issues],
    }


def run_audit(db: sqlite3.Connection, root: Path, config=None, top: int = 30) -> dict:
    import networkx as nx

    G = _load_nx(db)
    issues = []
    candidates = {}

    def flag(aid, reason, priority="medium"):
        if _is_meta_node(aid):
            return
        if aid not in candidates:
            candidates[aid] = {"reasons": set(), "priority": "medium"}
        candidates[aid]["reasons"].add(reason)
        if priority == "high":
            candidates[aid]["priority"] = "high"

    for scc in nx.strongly_connected_components(G):
        if len(scc) == 1:
            n = next(iter(scc))
            if not G.has_edge(n, n):
                continue
        issues.append({"type": "CYCLE", "ids": sorted(scc)})
        for n in scc:
            flag(n, "CYCLE", "high")

    for n in G.nodes():
        if not G.nodes[n].get("defined", False) and not _is_meta_node(n):
            srcs = sorted(p for p in G.predecessors(n))
            issues.append({"type": "DANGLING", "id": n, "referenced_by": srcs})
            flag(n, "DANGLING", "high")
            for s in srcs:
                flag(s, "REFS_DANGLING")

    UG = G.to_undirected()
    try:
        for u, v in nx.bridges(UG):
            if _is_meta_node(u) or _is_meta_node(v):
                continue
            issues.append({"type": "BRIDGE", "ids": [u, v]})
            flag(u, "BRIDGE")
            flag(v, "BRIDGE")
    except Exception:
        pass

    threshold = max(10, G.number_of_nodes() // 10)
    for n in G.nodes():
        if _is_meta_node(n):
            continue
        deg = G.in_degree(n) + G.out_degree(n)
        if deg > threshold:
            issues.append({"type": "BOTTLENECK", "id": n, "degree": deg})
            flag(n, "BOTTLENECK", "high")

    for n in G.nodes():
        if _is_meta_node(n):
            continue
        if G.in_degree(n) == 0:
            flag(n, "ROOT")
        if G.out_degree(n) == 0:
            flag(n, "SINK")

    if config:
        for cp in config.coverage_pairs:
            total = db.execute(
                "SELECT count(*) as c FROM artifacts WHERE type = ? AND defined = 1",
                (cp.source,)).fetchone()["c"]
            if total == 0:
                continue
            missing = [r["id"] for r in db.execute("""
                SELECT a.id FROM artifacts a
                WHERE a.type = ? AND a.defined = 1
                AND NOT EXISTS (
                    SELECT 1 FROM edges e JOIN artifacts a2 ON e.target_id = a2.id
                    WHERE e.source_id = a.id AND a2.type = ?
                ) AND NOT EXISTS (
                    SELECT 1 FROM edges e JOIN artifacts a2 ON e.source_id = a2.id
                    WHERE e.target_id = a.id AND a2.type = ?
                )
            """, (cp.source, cp.target, cp.target)).fetchall()]
            if missing:
                linked = total - len(missing)
                pct = round(linked / total * 100, 1) if total else 0
                issues.append({"type": "COVERAGE_GAP", "source": cp.source,
                               "target": cp.target, "pct": pct, "missing": missing})
                prio = "high" if pct < 50 else "medium"
                for aid in missing:
                    flag(aid, "COVERAGE_GAP", prio)

        for src_type, expected in config.expected_cross_layer.items():
            for tgt_type, label in expected:
                for r in db.execute(
                    "SELECT id FROM artifacts WHERE type = ? AND defined = 1",
                    (src_type,)).fetchall():
                    has_link = db.execute("""
                        SELECT 1 FROM edges e JOIN artifacts a ON e.target_id = a.id
                        WHERE e.source_id = ? AND a.type = ?
                        UNION
                        SELECT 1 FROM edges e JOIN artifacts a ON e.source_id = a.id
                        WHERE e.target_id = ? AND a.type = ?
                    """, (r["id"], tgt_type, r["id"], tgt_type)).fetchone()
                    if not has_link:
                        issues.append({"type": "MISSING_CROSS_LAYER", "id": r["id"],
                                       "expected": tgt_type, "label": label})
                        flag(r["id"], "MISSING_CROSS_LAYER")

        for src_type, tgt_types in config.expected_bidir.items():
            for tgt_type in tgt_types:
                rows = db.execute("""
                    SELECT DISTINCT e.source_id, e.target_id
                    FROM edges e
                    JOIN artifacts a1 ON e.source_id = a1.id
                    JOIN artifacts a2 ON e.target_id = a2.id
                    WHERE a1.type = ? AND a2.type = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM edges e2
                        WHERE e2.source_id = e.target_id AND e2.target_id = e.source_id
                    )
                """, (src_type, tgt_type)).fetchall()
                for r in rows:
                    issues.append({"type": "MISSING_BIDIR", "id": r["source_id"],
                                   "target": r["target_id"]})
                    flag(r["source_id"], "MISSING_BIDIR")

    sorted_list = []
    for aid, info in candidates.items():
        t = G.nodes[aid].get("type", "?") if aid in G else "?"
        sorted_list.append({
            "id": aid, "type": t,
            "reasons": sorted(info["reasons"]),
            "priority": info["priority"],
        })
    sorted_list.sort(key=lambda x: (0 if x["priority"] == "high" else 1,
                                    -len(x["reasons"]), x["id"]))
    sorted_list = sorted_list[:top]
    return {
        "summary": {
            "artifacts": G.number_of_nodes(),
            "edges": G.number_of_edges(),
            "issues": len(issues),
            "candidates": len(sorted_list),
        },
        "issues": issues,
        "candidates": sorted_list,
    }

"""
Traceability Scanner — builds a cross-reference graph
of BA artifacts and reports coverage gaps, orphans, and dangling references.

Config-driven: artifact types, patterns, and scan rules are defined in graph-ba.toml.

Usage:
    trace-ba --root /path/to/project
    trace-ba --root . --json-out reports/graph.json --dot-out reports/graph.dot -v
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import click
import networkx as nx

from graph_ba.config import ProjectConfig, load_config, normalize_id, classify_id


# ── Data model ────────────────────────────────────────────────────

@dataclass
class Artifact:
    id: str
    artifact_type: str  # type ID from config (e.g. "ST", "BR_REQ")
    source_file: Path
    line_number: int
    title: str = ""


@dataclass
class Reference:
    target_id: str
    source_file: Path
    line_number: int
    context: str = ""


@dataclass
class CodeReference:
    """A @trace reference found in a source code file."""
    code_file: Path
    line_number: int
    target_ids: List[str]  # normalized artifact IDs
    context: str = ""      # the raw comment line
    rel_path: str = ""     # relative path from project root


# ── Phase 1: Definition scanning ─────────────────────────────────

def _read_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _register(registry: Dict[str, Artifact], art: Artifact, config: ProjectConfig):
    nid = normalize_id(art.id, config)
    art.id = nid
    if nid not in registry:
        registry[nid] = art


def scan_definitions(root: Path, config: ProjectConfig) -> Dict[str, Artifact]:
    """Scan artifact definitions using rules from config."""
    registry: Dict[str, Artifact] = {}

    for rule in config.definitions:
        file_str = rule.file
        # Support glob patterns in file field
        if '*' in file_str or '?' in file_str:
            # Glob: scan matching files
            matched = sorted(root.glob(file_str))
            for f in matched:
                if rule.mode == "heading":
                    _scan_heading(registry, f, rule.pattern, rule.type_id, config)
                elif rule.mode == "table":
                    _scan_table_first_col(registry, f, rule.pattern, rule.type_id, config)
        else:
            filepath = root / file_str
            if rule.mode == "heading":
                _scan_heading(registry, filepath, rule.pattern, rule.type_id, config)
            elif rule.mode == "table":
                _scan_table_first_col(registry, filepath, rule.pattern, rule.type_id, config)

    return registry


def _scan_heading(registry, filepath, pattern, type_id, config):
    if not filepath.exists():
        return
    for i, line in enumerate(_read_lines(filepath), 1):
        m = pattern.match(line)
        if m:
            raw_id = m.group(1)
            title = m.group(2).strip() if m.lastindex >= 2 else ""
            _register(registry, Artifact(raw_id, type_id, filepath, i, title), config)


def _scan_table_first_col(registry, filepath, pattern, type_id, config):
    if not filepath.exists():
        return
    for i, line in enumerate(_read_lines(filepath), 1):
        m = pattern.match(line)
        if m:
            raw_id = m.group(1)
            cols = [c.strip() for c in line.split("|")]
            title = cols[2] if len(cols) > 2 else ""
            _register(registry, Artifact(raw_id, type_id, filepath, i, title), config)


# ── Phase 2: Reference extraction ────────────────────────────────

def expand_ranges(text: str, config: ProjectConfig) -> List[str]:
    """Expand ranges like BR.12.1–BR.12.6 into individual IDs."""
    results = []
    for m in config.range_pattern.finditer(text):
        prefix, start_s, end_s = m.group(1), m.group(2), m.group(3)
        for i in range(int(start_s), int(end_s) + 1):
            results.append(f"{prefix}{i}")
    return results


def scan_index_cross_refs(
    root: Path, config: ProjectConfig
) -> List[Tuple[str, str, Path, int]]:
    """Parse index tables where first column is the 'source' artifact and other
    columns contain 'target' artifact IDs."""
    results: List[Tuple[str, str, Path, int]] = []

    for rule in config.index_tables:
        filepath = root / rule.file
        _parse_index_table(results, filepath, rule.first_col_pattern, config)

    return results


def _parse_index_table(
    results: List[Tuple[str, str, Path, int]],
    filepath: Path,
    first_col_re: re.Pattern,
    config: ProjectConfig,
):
    if not filepath.exists():
        return
    lines = _read_lines(filepath)
    for line_num, line in enumerate(lines, 1):
        m = first_col_re.match(line)
        if not m:
            continue
        source_id = normalize_id(m.group(1), config)
        rest = line[m.end():]

        # Find all artifact IDs in the rest of the row
        for tid, tdef in config.types.items():
            for rm in tdef.ref_pattern.finditer(rest):
                target_id = normalize_id(rm.group(1), config)
                if target_id != source_id:
                    results.append((source_id, target_id, filepath, line_num))

        # Also expand ranges
        for eid in expand_ranges(rest, config):
            nid = normalize_id(eid, config)
            if nid != source_id:
                results.append((source_id, nid, filepath, line_num))


def scan_references(
    root: Path,
    registry: Dict[str, Artifact],
    config: ProjectConfig,
) -> List[Reference]:
    """Scan all .md files for cross-references to known artifact types."""
    scan_dirs = [root / d for d in config.scan_dirs]
    md_files: List[Path] = []
    for d in scan_dirs:
        if d.exists():
            md_files.extend(sorted(d.rglob("*.md")))

    # Build restriction sets for types with restrict_to
    restrict_files: Dict[str, Set[Path]] = {}
    for tid, tdef in config.types.items():
        if tdef.restrict_to:
            files: Set[Path] = set()
            for pattern in tdef.restrict_to:
                p = root / pattern
                if p.is_file():
                    files.add(p)
                elif p.is_dir():
                    files.update(p.rglob("*.md"))
                else:
                    # Treat as glob
                    files.update(root.glob(pattern))
            restrict_files[tid] = files

    all_refs: List[Reference] = []

    for filepath in md_files:
        lines = _read_lines(filepath)
        in_code_fence = False
        current_section = ""

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                continue
            if in_code_fence:
                continue

            if stripped.startswith("## "):
                current_section = stripped.lstrip("# ").strip()

            # Expand ranges
            expanded = expand_ranges(line, config)
            for eid in expanded:
                nid = normalize_id(eid, config)
                all_refs.append(Reference(nid, filepath, line_num, current_section))

            # Match all type patterns
            for tid, tdef in config.types.items():
                # Check restriction
                if tid in restrict_files and filepath not in restrict_files[tid]:
                    continue

                for m in tdef.ref_pattern.finditer(line):
                    raw_id = m.group(1)
                    nid = normalize_id(raw_id, config)
                    all_refs.append(Reference(nid, filepath, line_num, current_section))

    return all_refs


# ── Phase 2b: Code reference extraction ──────────────────────────

def scan_code_references(
    root: Path,
    config: ProjectConfig,
) -> List[CodeReference]:
    """Scan source code files for @trace comments referencing BA artifacts."""
    if not config.code:
        return []

    code_cfg = config.code
    comment_re = code_cfg.comment_pattern
    if not comment_re:
        return []

    results: List[CodeReference] = []

    code_files: List[Path] = []
    for dir_str in code_cfg.dirs:
        d = root / dir_str
        if not d.exists():
            continue
        for ext in code_cfg.extensions:
            code_files.extend(sorted(d.rglob(f"*.{ext}")))

    for filepath in code_files:
        try:
            lines = filepath.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue

        try:
            rel_path = str(filepath.relative_to(root))
        except ValueError:
            rel_path = str(filepath)

        for line_num, line in enumerate(lines, 1):
            m = comment_re.match(line)
            if not m:
                continue

            raw_ids_str = m.group(1).strip()
            raw_ids = [s.strip() for s in re.split(r'[,\s]+', raw_ids_str) if s.strip()]

            target_ids = []
            for raw_id in raw_ids:
                raw_id = raw_id.strip(".,;:")
                if not raw_id:
                    continue
                nid = normalize_id(raw_id, config)
                if classify_id(nid, config) is not None:
                    target_ids.append(nid)

            if target_ids:
                results.append(CodeReference(
                    code_file=filepath,
                    line_number=line_num,
                    target_ids=target_ids,
                    context=line.strip(),
                    rel_path=rel_path,
                ))

    return results


# ── Phase 3: Graph construction ──────────────────────────────────

def _find_owner(
    file_arts: List[Tuple[int, str]],
    ref_line: int,
) -> Optional[str]:
    if not file_arts:
        return None
    if len(file_arts) == 1:
        return file_arts[0][1]
    best = None
    for def_line, aid in file_arts:
        if def_line <= ref_line:
            best = aid
        else:
            break
    return best


def build_graph(
    registry: Dict[str, Artifact],
    references: List[Reference],
    config: ProjectConfig,
    index_xrefs: Optional[List[Tuple[str, str, Path, int]]] = None,
    code_refs: Optional[List[CodeReference]] = None,
) -> nx.DiGraph:
    G = nx.DiGraph()

    for aid, art in registry.items():
        G.add_node(aid, type=art.artifact_type, title=art.title,
                   source_file=str(art.source_file.name))

    file_arts_map: Dict[Path, List[Tuple[int, str]]] = defaultdict(list)
    for aid, art in registry.items():
        file_arts_map[art.source_file].append((art.line_number, aid))
    for k in file_arts_map:
        file_arts_map[k].sort()

    for ref in references:
        target = ref.target_id
        if target not in G:
            atype = classify_id(target, config)
            G.add_node(target, type=atype or "UNKNOWN",
                       title="", source_file="", defined=False)

        file_arts = file_arts_map.get(ref.source_file)
        owner = _find_owner(file_arts, ref.line_number) if file_arts else None

        if owner and owner != target:
            G.add_edge(owner, target, context=ref.context,
                       source_file=str(ref.source_file.name),
                       line=ref.line_number)
        elif not owner:
            file_node = f"FILE:{ref.source_file.name}"
            if not G.has_node(file_node):
                G.add_node(file_node, type="FILE", title=ref.source_file.name,
                           source_file=str(ref.source_file.name), defined=True)
            if target != file_node:
                G.add_edge(file_node, target, context=ref.context,
                           source_file=str(ref.source_file.name),
                           line=ref.line_number)

    if index_xrefs:
        for src, tgt, fpath, lnum in index_xrefs:
            if tgt not in G:
                atype = classify_id(tgt, config)
                G.add_node(tgt, type=atype or "UNKNOWN",
                           title="", source_file="", defined=False)
            if src not in G:
                continue
            if src != tgt:
                G.add_edge(src, tgt, context="index_table",
                           source_file=str(fpath.name), line=lnum)

    # ── Code references → CODE nodes ──
    if code_refs:
        for cref in code_refs:
            code_node_id = f"CODE:{cref.rel_path}"
            if not G.has_node(code_node_id):
                G.add_node(code_node_id, type="CODE", title=cref.rel_path,
                           source_file=cref.rel_path, defined=True)
            for target_id in cref.target_ids:
                if target_id not in G:
                    atype = classify_id(target_id, config)
                    G.add_node(target_id, type=atype or "UNKNOWN",
                               title="", source_file="", defined=False)
                G.add_edge(code_node_id, target_id,
                           context=cref.context,
                           source_file=cref.rel_path,
                           line=cref.line_number)

    for aid in registry:
        G.nodes[aid]["defined"] = True

    _resolve_dangling_variants(G, registry)

    return G


def _resolve_dangling_variants(G: nx.DiGraph, registry: Dict[str, Artifact]):
    """If an ID is referenced but not defined, link to its variants if they exist.

    E.g. BP-01 → BP-01a, BP-01b.
    """
    dangling = [
        n for n in G.nodes()
        if not G.nodes[n].get("defined", False) and not n.startswith("FILE:")
    ]
    for node_id in dangling:
        variants = [aid for aid in registry if aid.startswith(node_id) and aid != node_id]
        if not variants:
            continue
        preds = list(G.predecessors(node_id))
        for pred in preds:
            edge_data = G.edges[pred, node_id]
            for var in variants:
                if pred != var:
                    G.add_edge(pred, var, **edge_data)
        succs = list(G.successors(node_id))
        for succ in succs:
            edge_data = G.edges[node_id, succ]
            for var in variants:
                if var != succ:
                    G.add_edge(var, succ, **edge_data)
        G.remove_node(node_id)


# ── Phase 4: Verification checks ────────────────────────────────

@dataclass
class TraceReport:
    registry_count: Dict[str, int] = field(default_factory=dict)
    total_edges: int = 0
    orphans: List[str] = field(default_factory=list)
    dangling: List[Tuple[str, str, int]] = field(default_factory=list)
    coverage: Dict[str, dict] = field(default_factory=dict)
    missing_expected: List[Tuple[str, str]] = field(default_factory=list)


def verify(
    G: nx.DiGraph,
    registry: Dict[str, Artifact],
    references: List[Reference],
    config: ProjectConfig,
) -> TraceReport:
    report = TraceReport()

    type_counts: Dict[str, int] = defaultdict(int)
    for art in registry.values():
        type_counts[art.artifact_type] += 1
    report.registry_count = dict(type_counts)
    report.total_edges = G.number_of_edges()

    for aid in registry:
        if G.in_degree(aid) == 0:
            report.orphans.append(aid)

    defined_ids = set(registry.keys())
    dangling_ids: Set[str] = set()
    for ref in references:
        if ref.target_id not in defined_ids:
            if ref.target_id in G:
                continue
            dangling_ids.add(ref.target_id)

    dangling_first: Dict[str, Tuple[str, int]] = {}
    for ref in references:
        if ref.target_id in dangling_ids and ref.target_id not in dangling_first:
            dangling_first[ref.target_id] = (str(ref.source_file.name), ref.line_number)
    for did, (fname, lnum) in sorted(dangling_first.items()):
        report.dangling.append((did, fname, lnum))

    # Coverage matrix from config
    for cp in config.coverage_pairs:
        src_type, tgt_type, label = cp.source, cp.target, cp.label
        src_ids = [aid for aid, art in registry.items() if art.artifact_type == src_type]
        linked = []
        missing = []
        for sid in src_ids:
            if sid in G:
                has_link = any(
                    G.nodes.get(t, {}).get("type") == tgt_type
                    for t in G.successors(sid)
                )
                if has_link:
                    linked.append(sid)
                else:
                    missing.append(sid)
            else:
                missing.append(sid)
        total = len(src_ids)
        pct = (len(linked) / total * 100) if total > 0 else 0
        report.coverage[label] = {
            "total": total,
            "linked": len(linked),
            "pct": round(pct, 1),
            "missing": missing,
        }

    # Missing expected links from config
    for atype, expected in config.expected_cross_layer.items():
        for aid, art in registry.items():
            if art.artifact_type != atype or aid not in G:
                continue
            for target_type, label in expected:
                if not any(G.nodes.get(t, {}).get("type") == target_type
                           for t in G.successors(aid)):
                    report.missing_expected.append((aid, f"{target_type} ({label})"))

    return report


# ── Output: Console report ───────────────────────────────────────

def print_report(report: TraceReport, registry: Dict[str, Artifact],
                 config: ProjectConfig, verbose: bool):
    print("=" * 60)
    print("  Traceability Report")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    print("\n--- Registry Summary ---")
    total = 0
    for tid in config.type_order:
        cnt = report.registry_count.get(tid, 0)
        total += cnt
        label = config.types[tid].label if tid in config.types else tid
        print(f"  {label:40s} {cnt:>4d}")
    # Types not in config (shouldn't happen, but be safe)
    for tid, cnt in report.registry_count.items():
        if tid not in config.type_order:
            print(f"  {tid:40s} {cnt:>4d}")
            total += cnt
    print(f"  {'TOTAL':40s} {total:>4d} artifacts, {report.total_edges} edges")

    print("\n--- Coverage Matrix ---")
    for label, data in report.coverage.items():
        status = "OK" if data["pct"] >= 100 else f"WARN: {len(data['missing'])} missing"
        bar = f"{data['linked']}/{data['total']} ({data['pct']}%)"
        print(f"  {label:25s} {bar:>20s}  [{status}]")
        if verbose and data["missing"]:
            for mid in data["missing"]:
                print(f"       - {mid}")

    if report.dangling:
        print(f"\n--- Dangling References ({len(report.dangling)}) ---")
        for did, fname, lnum in report.dangling:
            print(f"  [ERROR] {did} referenced in {fname}:{lnum} but NOT defined")
    else:
        print("\n--- Dangling References: none ---")

    if report.orphans:
        print(f"\n--- Orphan Artifacts ({len(report.orphans)}) ---")
        orphan_types = defaultdict(list)
        for oid in report.orphans:
            if oid in registry:
                orphan_types[registry[oid].artifact_type].append(oid)
        for tval, ids in sorted(orphan_types.items()):
            print(f"  [{tval}] ({len(ids)}): {', '.join(sorted(ids))}")
    else:
        print("\n--- Orphan Artifacts: none ---")

    if report.missing_expected:
        print(f"\n--- Missing Expected Links ({len(report.missing_expected)}) ---")
        for aid, missing_type in sorted(report.missing_expected):
            print(f"  [WARN] {aid} has no link to {missing_type}")
    else:
        print("\n--- Missing Expected Links: none ---")

    errors = len(report.dangling)
    warnings = len(report.orphans) + len(report.missing_expected)
    ok_pairs = sum(1 for d in report.coverage.values() if d["pct"] >= 100)
    print(f"\n{'=' * 60}")
    print(f"  {errors} errors, {warnings} warnings, "
          f"{ok_pairs}/{len(report.coverage)} coverage pairs at 100%")
    print(f"{'=' * 60}")


# ── Output: JSON export ──────────────────────────────────────────

def export_json(G: nx.DiGraph, registry: Dict[str, Artifact],
                report: TraceReport, path: Path):
    data = {
        "metadata": {
            "generated": datetime.now().isoformat(),
            "artifact_count": len(registry),
            "edge_count": G.number_of_edges(),
        },
        "nodes": [
            {
                "id": n,
                "type": d.get("type", ""),
                "title": d.get("title", ""),
                "source_file": d.get("source_file", ""),
                "defined": d.get("defined", False),
            }
            for n, d in G.nodes(data=True)
        ],
        "edges": [
            {
                "source": u,
                "target": v,
                "context": d.get("context", ""),
                "source_file": d.get("source_file", ""),
                "line": d.get("line", 0),
            }
            for u, v, d in G.edges(data=True)
        ],
        "report": {
            "dangling": [{"id": d, "file": f, "line": l} for d, f, l in report.dangling],
            "orphans": report.orphans,
            "coverage": report.coverage,
            "missing_expected": [{"id": a, "missing": m} for a, m in report.missing_expected],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON exported to {path}")


# ── Output: DOT export ───────────────────────────────────────────

# Default colors for common types; config can extend/override
_DEFAULT_COLORS = {
    "ST":      {"bg": "#FFF3C4", "border": "#D69E2E"},
    "BR_REQ":  {"bg": "#C3DAF9", "border": "#2B6CB0"},
    "BR_RULE": {"bg": "#FCCFCF", "border": "#C53030"},
    "BP":      {"bg": "#C6F6D5", "border": "#276749"},
    "BD":      {"bg": "#FEEBC8", "border": "#C05621"},
    "BF":      {"bg": "#E9D8FD", "border": "#6B46C1"},
    "F":       {"bg": "#B2F5EA", "border": "#285E61"},
    "VAD":     {"bg": "#FED7E2", "border": "#B83280"},
    "M":       {"bg": "#E2E8F0", "border": "#4A5568"},
    "DM":      {"bg": "#D6BCFA", "border": "#553C9A"},
    "SM":      {"bg": "#FBD38D", "border": "#B7791F"},
    "EN":      {"bg": "#BEE3F8", "border": "#2A69AC"},
    "RL":      {"bg": "#C6F6D5", "border": "#22543D"},
    "FILE":    {"bg": "#F7FAFC", "border": "#A0AEC0"},
    "CODE":    {"bg": "#E6FFFA", "border": "#319795"},
    "UNKNOWN": {"bg": "#FED7D7", "border": "#E53E3E"},
}

_DEFAULT_SHAPES = {
    "ST": "house", "BR_REQ": "box", "BR_RULE": "octagon",
    "BP": "cds", "BD": "diamond", "BF": "component",
    "F": "note", "VAD": "hexagon", "M": "box3d",
    "DM": "box", "SM": "box", "EN": "box", "RL": "box",
    "FILE": "folder", "UNKNOWN": "plaintext",
}

_HTML_SHAPES = {
    "ST": "triangle", "BR_REQ": "box", "BR_RULE": "diamond",
    "BP": "ellipse", "BD": "hexagon", "BF": "box",
    "F": "star", "VAD": "hexagon", "M": "database",
    "DM": "box", "SM": "box", "EN": "box", "RL": "box",
    "FILE": "text", "UNKNOWN": "dot",
}

_HTML_SIZES = {
    "ST": 25, "BR_REQ": 18, "BR_RULE": 16, "BP": 22, "BD": 20,
    "BF": 14, "F": 24, "VAD": 22, "M": 26,
    "DM": 16, "SM": 16, "EN": 14, "RL": 16,
    "FILE": 10, "UNKNOWN": 10,
}


def _get_colors(atype: str) -> dict:
    return _DEFAULT_COLORS.get(atype, _DEFAULT_COLORS["UNKNOWN"])


def export_dot(G: nx.DiGraph, config: ProjectConfig, path: Path):
    """Export graph as a richly styled DOT file with clusters."""
    type_groups: Dict[str, List[str]] = defaultdict(list)
    for n, d in G.nodes(data=True):
        atype = d.get("type", "UNKNOWN")
        type_groups[atype].append(n)

    lines: List[str] = []
    lines.append('digraph traceability {')
    lines.append('  rankdir=TB; newrank=true; concentrate=true; compound=true;')
    lines.append('  splines=ortho; nodesep=0.4; ranksep=0.8;')
    lines.append('  fontname="Helvetica"; bgcolor="#FAFAFA"; pad=0.5;')
    lines.append('  node [style="filled,rounded", fontname="Helvetica", fontsize=10, '
                 'penwidth=1.5, margin="0.15,0.08"];')
    lines.append('  edge [fontname="Helvetica", fontsize=8, penwidth=0.8, '
                 'arrowsize=0.7, color="#718096"];')
    lines.append('')

    # Clustered subgraphs by type order from config
    all_types = list(config.type_order) + [t for t in type_groups if t not in config.type_order]
    for atype in all_types:
        nodes = type_groups.get(atype, [])
        if not nodes:
            continue
        label = config.types[atype].label if atype in config.types else atype
        colors = _get_colors(atype)
        shape = _DEFAULT_SHAPES.get(atype, "box")
        nodes.sort()

        is_meta = atype in ("FILE", "UNKNOWN")
        style = 'rounded,dashed' if is_meta else 'rounded,filled'

        lines.append(f'  subgraph cluster_{atype} {{')
        lines.append(f'    label="{label}"; style="{style}";')
        lines.append(f'    fillcolor="{colors["bg"]}20"; color="{colors["border"]}";')
        lines.append(f'    penwidth=1.5; fontname="Helvetica"; fontsize=12;')
        for n in nodes:
            d = G.nodes[n]
            lbl = _dot_node_label(n, d)
            defined = d.get("defined", True)
            ns = "filled,rounded,dashed" if not defined else "filled,rounded"
            lines.append(
                f'    "{_esc(n)}" [label="{lbl}", shape={shape}, '
                f'fillcolor="{colors["bg"]}", color="{colors["border"]}", style="{ns}"];'
            )
        lines.append('  }')
        lines.append('')

    # Edges
    lines.append('  // Edges')
    for u, v, d in G.edges(data=True):
        src_type = G.nodes[u].get("type", "UNKNOWN") if u in G.nodes else "UNKNOWN"
        edge_color = _get_colors(src_type)["border"]
        ctx = d.get("context", "").replace('"', '\\"')[:60]
        lines.append(
            f'  "{_esc(u)}" -> "{_esc(v)}" '
            f'[color="{edge_color}80", tooltip="{ctx}"];'
        )
    lines.append('}')

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"DOT exported to {path}")


def _esc(s: str) -> str:
    return s.replace('"', '\\"')


def _dot_node_label(node_id: str, data: dict) -> str:
    eid = node_id.replace('"', '\\"')
    title = data.get("title", "")
    if not title:
        return eid
    short = title[:35].replace('"', '\\"')
    if len(title) > 35:
        short += "…"
    return f"{eid}\\n{short}"


# ── Output: ARTIFACT_INDEX.md ────────────────────────────────────

def export_index(G: nx.DiGraph, registry: Dict[str, Artifact],
                 config: ProjectConfig, root: Path, path: Path):
    """Generate compact ARTIFACT_INDEX.md for agent navigation."""
    lines_out = [
        "# Artifact Index",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d')}. "
        "Rebuild: `graph-ba import`",
        "",
        "## Semantic Map",
        "",
    ]

    for topic, ids in config.clusters.items():
        existing = [i for i in ids if normalize_id(i, config) in registry]
        if existing:
            lines_out.append(f"**{topic}:** {', '.join(existing)}")

    lines_out.append("")
    lines_out.append("---")
    lines_out.append("")

    for tid in config.type_order:
        arts = sorted(
            [(aid, art) for aid, art in registry.items() if art.artifact_type == tid],
            key=lambda x: x[0],
        )
        if not arts:
            continue

        label = config.types[tid].label if tid in config.types else tid
        lines_out.append(f"## {label} ({len(arts)})")
        lines_out.append("")

        dir_groups: Dict[str, List[Tuple[str, Artifact]]] = defaultdict(list)
        for aid, art in arts:
            try:
                d = str(art.source_file.parent.relative_to(root))
            except ValueError:
                d = str(art.source_file.parent)
            dir_groups[d].append((aid, art))

        for dirpath, group in dir_groups.items():
            if len(dir_groups) > 1:
                lines_out.append(f"_{dirpath}/_")
            for aid, art in group:
                fname = art.source_file.name
                title = art.title[:55] if art.title else ""
                if title:
                    lines_out.append(f"- `{aid}` {fname}:{art.line_number} — {title}")
                else:
                    lines_out.append(f"- `{aid}` {fname}:{art.line_number}")

        lines_out.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines_out), encoding="utf-8")
    print(f"Index exported to {path}")


# ── Output: Interactive HTML (vis-network) ───────────────────────

def export_html(G: nx.DiGraph, config: ProjectConfig, path: Path):
    """Export a standalone interactive HTML visualization."""
    nodes_js: List[str] = []
    for n, d in G.nodes(data=True):
        atype = d.get("type", "UNKNOWN")
        title_text = d.get("title", "").replace("'", "\\'").replace("\n", " ")
        source_file = d.get("source_file", "")
        defined = d.get("defined", True)

        colors = _get_colors(atype)
        shape = _HTML_SHAPES.get(atype, "dot")
        size = _HTML_SIZES.get(atype, 15)
        group_label = config.types[atype].label if atype in config.types else atype

        in_deg = G.in_degree(n)
        out_deg = G.out_degree(n)
        tip_parts = [f"<b>{n}</b>"]
        if title_text:
            tip_parts.append(title_text)
        tip_parts.append(f"<i>Type:</i> {group_label}")
        if source_file:
            tip_parts.append(f"<i>File:</i> {source_file}")
        tip_parts.append(f"<i>In:</i> {in_deg} | <i>Out:</i> {out_deg}")
        if not defined:
            tip_parts.append("<b style='color:red'>Not defined</b>")
        tooltip = "<br>".join(tip_parts).replace("'", "\\'")

        bg = colors["bg"] if defined else "#FED7D7"
        border = colors["border"] if defined else "#E53E3E"
        esc_n = n.replace("'", "\\'")

        nodes_js.append(
            f"  {{id:'{esc_n}',label:'{esc_n}',title:'{tooltip}',"
            f"shape:'{shape}',size:{size},group:'{atype}',"
            f"color:{{background:'{bg}',border:'{border}',"
            f"highlight:{{background:'{bg}',border:'{border}'}}}}}}"
        )

    edges_js: List[str] = []
    for u, v, d in G.edges(data=True):
        ctx = d.get("context", "").replace("'", "\\'")[:60]
        src_file = d.get("source_file", "").replace("'", "\\'")
        tip = f"{u} → {v}"
        if ctx:
            tip += f"\\n{ctx}"
        if src_file:
            tip += f"\\n({src_file})"
        tip = tip.replace("'", "\\'")
        eu = u.replace("'", "\\'")
        ev = v.replace("'", "\\'")
        edges_js.append(f"  {{from:'{eu}',to:'{ev}',title:'{tip}'}}")

    # Legend items from config types
    legend_items_html = []
    for tid in config.type_order:
        if tid not in config.types:
            continue
        c = _get_colors(tid)
        lbl = config.types[tid].label
        legend_items_html.append(
            f'<label style="display:flex;align-items:center;gap:6px;margin:2px 0;cursor:pointer">'
            f'<input type="checkbox" checked data-group="{tid}" '
            f'onchange="toggleGroup(this)">'
            f'<span style="display:inline-block;width:14px;height:14px;'
            f'background:{c["bg"]};border:2px solid {c["border"]};border-radius:3px"></span>'
            f'<span style="font-size:11px;color:#4A5568">{lbl}</span></label>'
        )

    html = _HTML_TEMPLATE.format(
        nodes_data=",\n".join(nodes_js),
        edges_data=",\n".join(edges_js),
        legend_items="\n".join(legend_items_html),
        node_count=G.number_of_nodes(),
        edge_count=G.number_of_edges(),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    print(f"HTML exported to {path}")


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Graph BA — Artifact Traceability</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/dist/vis-network.min.css" crossorigin="anonymous"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.js" crossorigin="anonymous"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: Helvetica, Arial, sans-serif; background: #FAFAFA; overflow: hidden; }}
  #graph {{ width:100vw; height:100vh; }}
  #legend {{
    position:fixed; top:12px; left:12px; z-index:9999;
    background:white; border:1px solid #CBD5E0; border-radius:8px;
    padding:10px 14px; box-shadow:0 2px 8px rgba(0,0,0,0.12);
    max-width:220px; max-height:90vh; overflow-y:auto;
  }}
  #legend h3 {{ font-size:13px; color:#2D3748; margin-bottom:6px; }}
  #stats {{
    position:fixed; bottom:12px; left:12px; z-index:9999;
    background:white; border:1px solid #CBD5E0; border-radius:8px;
    padding:8px 12px; box-shadow:0 2px 8px rgba(0,0,0,0.08);
    font-size:11px; color:#4A5568;
  }}
  #search {{
    position:fixed; top:12px; right:12px; z-index:9999;
    background:white; border:1px solid #CBD5E0; border-radius:8px;
    padding:8px 12px; box-shadow:0 2px 8px rgba(0,0,0,0.08);
  }}
  #search input {{
    border:1px solid #CBD5E0; border-radius:4px; padding:5px 8px;
    font-size:12px; width:200px; outline:none;
  }}
  #search input:focus {{ border-color:#4299E1; }}
  #stabilize-msg {{
    position:fixed; top:50%; left:50%; transform:translate(-50%,-50%);
    background:rgba(45,55,72,0.85); color:white; padding:16px 28px;
    border-radius:10px; font-size:14px; z-index:99999;
    transition: opacity 0.5s;
  }}
</style>
</head>
<body>
<div id="graph"></div>
<div id="legend">
  <h3>Legend (click to filter)</h3>
  {legend_items}
</div>
<div id="search">
  <input type="text" id="searchBox" placeholder="Search by ID..." oninput="onSearch(this.value)">
</div>
<div id="stats">Nodes: {node_count} | Edges: {edge_count}</div>
<div id="stabilize-msg">Stabilizing graph…</div>
<script>
var allNodes = new vis.DataSet([
{nodes_data}
]);
var allEdges = new vis.DataSet([
{edges_data}
]);
var container = document.getElementById('graph');
var data = {{ nodes: allNodes, edges: allEdges }};
var options = {{
  physics: {{
    enabled: true,
    barnesHut: {{
      gravitationalConstant: -6000, centralGravity: 0.25,
      springLength: 140, springConstant: 0.04, damping: 0.09, avoidOverlap: 0.4
    }},
    stabilization: {{ enabled: true, iterations: 500, updateInterval: 25 }}
  }},
  interaction: {{
    hover: true, tooltipDelay: 100,
    navigationButtons: true, keyboard: {{ enabled: true }}
  }},
  edges: {{
    arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }},
    smooth: {{ type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.4 }},
    color: {{ inherit: 'from', opacity: 0.35 }}, width: 0.7, hoverWidth: 2
  }},
  nodes: {{
    font: {{ size: 11, face: 'Helvetica, Arial, sans-serif' }},
    borderWidth: 2, borderWidthSelected: 3
  }}
}};
var network = new vis.Network(container, data, options);
network.on('stabilizationIterationsDone', function() {{
  network.setOptions({{ physics: {{ enabled: false }} }});
  document.getElementById('stabilize-msg').style.opacity = '0';
  setTimeout(function() {{
    document.getElementById('stabilize-msg').style.display = 'none';
  }}, 600);
}});
function onSearch(q) {{
  q = q.trim().toUpperCase();
  if (!q) {{
    allNodes.forEach(function(n) {{ allNodes.update({{id:n.id, opacity:1, font:{{size:11}}}}); }});
    return;
  }}
  allNodes.forEach(function(n) {{
    var match = n.id.toUpperCase().indexOf(q) >= 0;
    allNodes.update({{id:n.id, opacity: match ? 1 : 0.15, font:{{size: match ? 14 : 8}}}});
  }});
}}
var hiddenGroups = {{}};
function toggleGroup(cb) {{
  var g = cb.dataset.group;
  if (cb.checked) {{ delete hiddenGroups[g]; }} else {{ hiddenGroups[g] = true; }}
  allNodes.forEach(function(n) {{
    var hidden = !!hiddenGroups[n.group];
    allNodes.update({{id:n.id, hidden:hidden}});
  }});
}}
</script>
</body>
</html>
"""


def _filter_graph(G: nx.DiGraph, no_file_nodes: bool, no_transitive: bool,
                  verbose: bool) -> nx.DiGraph:
    """Return a filtered copy of G for visualization."""
    if not no_file_nodes and not no_transitive:
        return G

    H = G.copy()
    removed_nodes = 0
    removed_edges = 0

    if no_file_nodes:
        file_nodes = [n for n, d in H.nodes(data=True) if d.get("type") == "FILE"]
        H.remove_nodes_from(file_nodes)
        removed_nodes = len(file_nodes)

    if no_transitive:
        to_remove = []
        for u, v in list(H.edges()):
            H.remove_edge(u, v)
            if nx.has_path(H, u, v):
                to_remove.append((u, v))
            else:
                H.add_edge(u, v, **G.edges[u, v])
        for u, v in to_remove:
            if H.has_edge(u, v):
                H.remove_edge(u, v)
        removed_edges = len(to_remove)

    if verbose or (removed_nodes or removed_edges):
        orig_n, orig_e = G.number_of_nodes(), G.number_of_edges()
        new_n, new_e = H.number_of_nodes(), H.number_of_edges()
        print(f"\n[filter] {orig_n} → {new_n} nodes, {orig_e} → {new_e} edges"
              f" (removed {removed_nodes} FILE nodes, {removed_edges} transitive edges)")

    return H


# ── CLI ───────────────────────────────────────────────────────────

@click.command()
@click.option("--root", type=click.Path(exists=True, path_type=Path),
              default=".", help="Project root directory")
@click.option("--json-out", type=click.Path(path_type=Path), default=None,
              help="Path for JSON graph export")
@click.option("--dot-out", type=click.Path(path_type=Path), default=None,
              help="Path for DOT file export")
@click.option("--html-out", type=click.Path(path_type=Path), default=None,
              help="Path for interactive HTML export")
@click.option("--no-file-nodes", is_flag=True,
              help="Exclude FILE nodes from visual exports")
@click.option("--no-transitive", is_flag=True,
              help="Remove transitive edges")
@click.option("--index", "index_out", type=click.Path(path_type=Path), default=None,
              help="Path for ARTIFACT_INDEX.md")
@click.option("--index-auto", is_flag=True,
              help="Generate index at default path")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed info")
def main(root: Path, json_out: Optional[Path], dot_out: Optional[Path],
         html_out: Optional[Path],
         no_file_nodes: bool, no_transitive: bool,
         index_out: Optional[Path], index_auto: bool, verbose: bool):
    """Traceability Scanner — parse BA artifacts and verify cross-references."""
    root = root.resolve()
    config = load_config(root)

    registry = scan_definitions(root, config)
    if verbose:
        print(f"[scan] {len(registry)} artifact definitions found")

    references = scan_references(root, registry, config)
    index_xrefs = scan_index_cross_refs(root, config)
    code_refs = scan_code_references(root, config)
    if verbose:
        print(f"[scan] {len(references)} references found, {len(index_xrefs)} index cross-refs, "
              f"{len(code_refs)} code trace refs")

    G = build_graph(registry, references, config, index_xrefs, code_refs)
    report = verify(G, registry, references, config)
    print_report(report, registry, config, verbose)

    if json_out:
        export_json(G, registry, report, json_out)

    G_vis = _filter_graph(G, no_file_nodes, no_transitive, verbose)

    if dot_out:
        export_dot(G_vis, config, dot_out)
    if html_out:
        export_html(G_vis, config, html_out)

    # Index generation
    idx_path = index_out
    if not idx_path and index_auto:
        # Find first scan dir that exists
        for d in config.scan_dirs:
            p = root / d
            if p.exists():
                idx_path = p / "ARTIFACT_INDEX.md"
                break
    if idx_path:
        export_index(G, registry, config, root, idx_path)


if __name__ == "__main__":
    main()

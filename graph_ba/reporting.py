"""Traceability verification and JSON, DOT, Markdown and HTML exports."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import networkx as nx

from graph_ba.config import ProjectConfig, normalize_id

from .models import Artifact, Reference, TraceReport


def verify(
    G: nx.DiGraph,
    registry: dict[str, Artifact],
    references: list[Reference],
    config: ProjectConfig,
) -> TraceReport:
    report = TraceReport()

    type_counts: dict[str, int] = defaultdict(int)
    for art in registry.values():
        type_counts[art.artifact_type] += 1
    report.registry_count = dict(type_counts)
    report.total_edges = G.number_of_edges()

    for aid in registry:
        if G.in_degree(aid) == 0:
            report.orphans.append(aid)

    defined_ids = set(registry.keys())
    dangling_ids: set[str] = set()
    for ref in references:
        if ref.target_id not in defined_ids:
            if ref.target_id in G:
                continue
            dangling_ids.add(ref.target_id)

    dangling_first: dict[str, tuple[str, int]] = {}
    for ref in references:
        if ref.target_id in dangling_ids and ref.target_id not in dangling_first:
            dangling_first[ref.target_id] = (str(ref.source_file.name), ref.line_number)
    for did, (fname, lnum) in sorted(dangling_first.items()):
        report.dangling.append((did, fname, lnum))

    # Coverage matrix from config
    for cp in config.coverage_pairs:
        src_type, tgt_type, label = cp.source, cp.target, cp.label
        src_ids = [
            aid for aid, art in registry.items() if art.artifact_type == src_type
        ]
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
                if not any(
                    G.nodes.get(t, {}).get("type") == target_type
                    for t in G.successors(aid)
                ):
                    report.missing_expected.append((aid, f"{target_type} ({label})"))

    return report


def print_report(
    report: TraceReport,
    registry: dict[str, Artifact],
    config: ProjectConfig,
    verbose: bool,
):
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
    print(
        f"  {errors} errors, {warnings} warnings, "
        f"{ok_pairs}/{len(report.coverage)} coverage pairs at 100%"
    )
    print(f"{'=' * 60}")


def export_json(
    G: nx.DiGraph, registry: dict[str, Artifact], report: TraceReport, path: Path
):
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
                "origin": d.get("origin", ""),
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
                "relation_type": d.get("relation_type", "MENTIONS"),
                "context": d.get("context", ""),
                "source_file": d.get("source_file", ""),
                "line": d.get("line", 0),
            }
            for u, v, d in G.edges(data=True)
        ],
        "report": {
            "dangling": [
                {"id": d, "file": f, "line": l} for d, f, l in report.dangling
            ],
            "orphans": report.orphans,
            "coverage": report.coverage,
            "missing_expected": [
                {"id": a, "missing": m} for a, m in report.missing_expected
            ],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON exported to {path}")


_DEFAULT_COLORS = {
    "ST": {"bg": "#FFF3C4", "border": "#D69E2E"},
    "BR": {"bg": "#C3DAF9", "border": "#2B6CB0"},
    "BR_REQ": {"bg": "#C3DAF9", "border": "#2B6CB0"},
    "BRL": {"bg": "#FCCFCF", "border": "#C53030"},
    "BR_RULE": {"bg": "#FCCFCF", "border": "#C53030"},
    "BP": {"bg": "#C6F6D5", "border": "#276749"},
    "BD": {"bg": "#FEEBC8", "border": "#C05621"},
    "BF": {"bg": "#E9D8FD", "border": "#6B46C1"},
    "F": {"bg": "#B2F5EA", "border": "#285E61"},
    "VAD": {"bg": "#FED7E2", "border": "#B83280"},
    "M": {"bg": "#E2E8F0", "border": "#4A5568"},
    "DM": {"bg": "#D6BCFA", "border": "#553C9A"},
    "SM": {"bg": "#FBD38D", "border": "#B7791F"},
    "EN": {"bg": "#BEE3F8", "border": "#2A69AC"},
    "RL": {"bg": "#C6F6D5", "border": "#22543D"},
    "FILE": {"bg": "#F7FAFC", "border": "#A0AEC0"},
    "CODE": {"bg": "#E6FFFA", "border": "#319795"},
    "TEST": {"bg": "#FAF5FF", "border": "#805AD5"},
    "UNKNOWN": {"bg": "#FED7D7", "border": "#E53E3E"},
}

_DEFAULT_SHAPES = {
    "ST": "house",
    "BR": "box",
    "BR_REQ": "box",
    "BRL": "octagon",
    "BR_RULE": "octagon",
    "BP": "cds",
    "BD": "diamond",
    "BF": "component",
    "F": "note",
    "VAD": "hexagon",
    "M": "box3d",
    "DM": "box",
    "SM": "box",
    "EN": "box",
    "RL": "box",
    "FILE": "folder",
    "UNKNOWN": "plaintext",
}

_HTML_SHAPES = {
    "ST": "triangle",
    "BR": "box",
    "BR_REQ": "box",
    "BRL": "diamond",
    "BR_RULE": "diamond",
    "BP": "ellipse",
    "BD": "hexagon",
    "BF": "box",
    "F": "star",
    "VAD": "hexagon",
    "M": "database",
    "DM": "box",
    "SM": "box",
    "EN": "box",
    "RL": "box",
    "FILE": "text",
    "UNKNOWN": "dot",
}

_HTML_SIZES = {
    "ST": 25,
    "BR": 18,
    "BR_REQ": 18,
    "BRL": 16,
    "BR_RULE": 16,
    "BP": 22,
    "BD": 20,
    "BF": 14,
    "F": 24,
    "VAD": 22,
    "M": 26,
    "DM": 16,
    "SM": 16,
    "EN": 14,
    "RL": 16,
    "FILE": 10,
    "UNKNOWN": 10,
}


def _get_colors(atype: str) -> dict:
    return _DEFAULT_COLORS.get(atype, _DEFAULT_COLORS["UNKNOWN"])


def export_dot(G: nx.DiGraph, config: ProjectConfig, path: Path):
    """Export graph as a richly styled DOT file with clusters."""
    type_groups: dict[str, list[str]] = defaultdict(list)
    for n, d in G.nodes(data=True):
        atype = d.get("type", "UNKNOWN")
        type_groups[atype].append(n)

    lines: list[str] = []
    lines.append("digraph traceability {")
    lines.append("  rankdir=TB; newrank=true; concentrate=true; compound=true;")
    lines.append("  splines=ortho; nodesep=0.4; ranksep=0.8;")
    lines.append('  fontname="Helvetica"; bgcolor="#FAFAFA"; pad=0.5;')
    lines.append(
        '  node [style="filled,rounded", fontname="Helvetica", fontsize=10, '
        'penwidth=1.5, margin="0.15,0.08"];'
    )
    lines.append(
        '  edge [fontname="Helvetica", fontsize=8, penwidth=0.8, arrowsize=0.7, color="#718096"];'
    )
    lines.append("")

    # Clustered subgraphs by type order from config
    all_types = list(config.type_order) + [
        t for t in type_groups if t not in config.type_order
    ]
    for atype in all_types:
        nodes = type_groups.get(atype, [])
        if not nodes:
            continue
        label = config.types[atype].label if atype in config.types else atype
        colors = _get_colors(atype)
        shape = _DEFAULT_SHAPES.get(atype, "box")
        nodes.sort()

        is_meta = atype in ("FILE", "UNKNOWN")
        style = "rounded,dashed" if is_meta else "rounded,filled"

        lines.append(f"  subgraph cluster_{atype} {{")
        lines.append(f'    label="{label}"; style="{style}";')
        lines.append(f'    fillcolor="{colors["bg"]}20"; color="{colors["border"]}";')
        lines.append('    penwidth=1.5; fontname="Helvetica"; fontsize=12;')
        for n in nodes:
            d = G.nodes[n]
            lbl = _dot_node_label(n, d)
            defined = d.get("defined", True)
            ns = "filled,rounded,dashed" if not defined else "filled,rounded"
            lines.append(
                f'    "{_esc(n)}" [label="{lbl}", shape={shape}, '
                f'fillcolor="{colors["bg"]}", color="{colors["border"]}", style="{ns}"];'
            )
        lines.append("  }")
        lines.append("")

    # Edges
    lines.append("  // Edges")
    for u, v, d in G.edges(data=True):
        src_type = G.nodes[u].get("type", "UNKNOWN") if u in G.nodes else "UNKNOWN"
        edge_color = _get_colors(src_type)["border"]
        rel = d.get("relation_type", "MENTIONS")
        ctx = d.get("context", "").replace('"', '\\"')[:60]
        tooltip = f"{rel}: {ctx}" if ctx else rel
        lines.append(
            f'  "{_esc(u)}" -> "{_esc(v)}" '
            f'[color="{edge_color}80", label="{rel if rel != "MENTIONS" else ""}", '
            f'tooltip="{tooltip}"];'
        )
    lines.append("}")

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


def export_index(
    G: nx.DiGraph,
    registry: dict[str, Artifact],
    config: ProjectConfig,
    root: Path,
    path: Path,
):
    """Generate compact ARTIFACT_INDEX.md for agent navigation."""
    lines_out = [
        "# Artifact Index",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d')}. Rebuild: `graph-ba import`",
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

        dir_groups: dict[str, list[tuple[str, Artifact]]] = defaultdict(list)
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


def export_html(G: nx.DiGraph, config: ProjectConfig, path: Path):
    """Export a standalone interactive HTML visualization."""
    nodes_js: list[str] = []
    for n, d in G.nodes(data=True):
        atype = d.get("type", "UNKNOWN")
        title_text = d.get("title", "").replace("'", "\\'").replace("\n", " ")
        source_file = d.get("source_file", "")
        origin = d.get("origin", "")
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
        if origin:
            tip_parts.append(f"<i>Origin:</i> {origin}")
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

    edges_js: list[str] = []
    for u, v, d in G.edges(data=True):
        ctx = d.get("context", "").replace("'", "\\'")[:60]
        rel = d.get("relation_type", "MENTIONS")
        src_file = d.get("source_file", "").replace("'", "\\'")
        tip = f"{u} → {v}\\n{rel}"
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
<title>graph-ba — Artifact Traceability</title>
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
var origNodes = {{}};
allNodes.forEach(function(n) {{ origNodes[n.id] = JSON.parse(JSON.stringify(n)); }});
var hiddenGroups = {{}};
function toggleGroup(cb) {{
  var g = cb.dataset.group;
  if (cb.checked) {{ delete hiddenGroups[g]; }} else {{ hiddenGroups[g] = true; }}
  var updates = [];
  allNodes.forEach(function(n) {{
    if (n.group === g) {{
      if (cb.checked) {{
        var o = origNodes[n.id];
        updates.push({{id:n.id, hidden:false, color:o.color, shape:o.shape, size:o.size}});
      }} else {{
        updates.push({{id:n.id, hidden:true}});
      }}
    }}
  }});
  allNodes.update(updates);
}}
</script>
</body>
</html>
"""


def _filter_graph(
    G: nx.DiGraph, no_file_nodes: bool, no_transitive: bool, verbose: bool
) -> nx.DiGraph:
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
        print(
            f"\n[filter] {orig_n} → {new_n} nodes, {orig_e} → {new_e} edges"
            f" (removed {removed_nodes} FILE nodes, {removed_edges} transitive edges)"
        )

    return H

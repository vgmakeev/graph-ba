"""
Traceability Scanner — builds a cross-reference graph
of BA artifacts and reports coverage gaps, orphans, and dangling references.

Config-driven: artifact types, patterns, and scan rules are defined in graph-ba.toml.

Usage:
    trace-ba --root /path/to/project
    trace-ba --root . --json-out reports/graph.json --dot-out reports/graph.dot -v
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import networkx as nx

from graph_ba.config import ProjectConfig, classify_id

from .models import (
    Artifact,
    CodeReference,
    GraphNativeArtifactTrace,
    GraphNativeChangeTrace,
    MiniAdminComponentTrace,
    Reference,
)
from .provider_scanning import MiniAdminSourceTrace, MiniRegistryTrace, ReactUiElement


def _find_owner(
    file_arts: list[tuple[int, str]],
    ref_line: int,
) -> str | None:
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
    registry: dict[str, Artifact],
    references: list[Reference],
    config: ProjectConfig,
    index_xrefs: list[tuple[str, str, Path, int]] | None = None,
    code_refs: list[CodeReference] | None = None,
    test_refs: list[CodeReference] | None = None,
    ui_refs: list[CodeReference] | None = None,
    mini_registry_traces: list[MiniRegistryTrace] | None = None,
    react_ui_elements: list[ReactUiElement] | None = None,
    mini_admin_source_traces: list[MiniAdminSourceTrace] | None = None,
    mini_admin_component_traces: list[MiniAdminComponentTrace] | None = None,
    graph_native_change_traces: list[GraphNativeChangeTrace] | None = None,
    graph_native_artifact_traces: list[GraphNativeArtifactTrace] | None = None,
) -> nx.DiGraph:
    G = nx.DiGraph()

    for aid, art in registry.items():
        origin = (
            config.types.get(art.artifact_type).origin
            if art.artifact_type in config.types
            else ""
        )
        G.add_node(
            aid,
            type=art.artifact_type,
            title=art.title,
            source_file=str(art.source_file.name),
            origin=origin,
        )

    file_arts_map: dict[Path, list[tuple[int, str]]] = defaultdict(list)
    for aid, art in registry.items():
        file_arts_map[art.source_file].append((art.line_number, aid))
    for k in file_arts_map:
        file_arts_map[k].sort()

    for ref in references:
        target = ref.target_id
        if target not in G:
            atype = classify_id(target, config)
            origin = config.types.get(atype).origin if atype in config.types else ""
            G.add_node(
                target,
                type=atype or "UNKNOWN",
                title="",
                source_file="",
                defined=False,
                origin=origin,
            )

        file_arts = file_arts_map.get(ref.source_file)
        owner = _find_owner(file_arts, ref.line_number) if file_arts else None

        if owner and owner != target:
            G.add_edge(
                owner,
                target,
                context=ref.context,
                source_file=str(ref.source_file.name),
                line=ref.line_number,
                relation_type="MENTIONS",
            )
        elif not owner:
            file_node = f"FILE:{ref.source_file.name}"
            if not G.has_node(file_node):
                G.add_node(
                    file_node,
                    type="FILE",
                    title=ref.source_file.name,
                    source_file=str(ref.source_file.name),
                    defined=True,
                    origin="container",
                )
            if target != file_node:
                G.add_edge(
                    file_node,
                    target,
                    context=ref.context,
                    source_file=str(ref.source_file.name),
                    line=ref.line_number,
                    relation_type="MENTIONS",
                )

    if index_xrefs:
        for src, tgt, fpath, lnum in index_xrefs:
            if tgt not in G:
                atype = classify_id(tgt, config)
                origin = config.types.get(atype).origin if atype in config.types else ""
                G.add_node(
                    tgt,
                    type=atype or "UNKNOWN",
                    title="",
                    source_file="",
                    defined=False,
                    origin=origin,
                )
            if src not in G:
                continue
            if src != tgt:
                G.add_edge(
                    src,
                    tgt,
                    context="index_table",
                    source_file=str(fpath.name),
                    line=lnum,
                    relation_type="INDEX",
                )

    # ── Code references → CODE nodes, test references → TEST nodes,
    #    UI trace references → UI nodes ──
    _add_source_ref_nodes(
        G,
        code_refs,
        "CODE:",
        "CODE",
        config,
        relation_type="CODE_TRACE",
        origin="implementation",
    )
    _add_source_ref_nodes(
        G,
        test_refs,
        "TEST:",
        "TEST",
        config,
        relation_type="TEST_EVIDENCE",
        origin="evidence",
    )
    _add_source_ref_nodes(
        G, ui_refs, "UI:", "UI", config, relation_type="UI_TRACE", origin="evidence"
    )
    _add_mini_registry_nodes(G, mini_registry_traces, config)
    _add_react_ui_nodes(G, react_ui_elements, config)
    _add_mini_admin_source_nodes(G, mini_admin_source_traces, config)
    _add_mini_admin_component_trace_nodes(G, mini_admin_component_traces, config)
    _add_graph_native_change_nodes(G, graph_native_change_traces, config)
    _add_graph_native_artifact_nodes(G, graph_native_artifact_traces, config)

    for aid in registry:
        G.nodes[aid]["defined"] = True

    _resolve_dangling_variants(G, registry)

    return G


def _add_graph_native_change_nodes(
    G: nx.DiGraph,
    traces: list[GraphNativeChangeTrace] | None,
    config: ProjectConfig,
):
    if not traces:
        return
    origin = config.graph_native.change_origin if config.graph_native else "derived"
    change_type = config.graph_native.change_type if config.graph_native else "CHG"
    for trace in traces:
        if not G.has_node(trace.source_id):
            G.add_node(
                trace.source_id,
                type=change_type,
                title=trace.source_id,
                source_file=trace.rel_path,
                defined=True,
                origin=origin,
            )
        if not G.has_node(trace.target_id):
            atype = classify_id(trace.target_id, config)
            target_origin = (
                config.types.get(atype).origin if atype in config.types else ""
            )
            G.add_node(
                trace.target_id,
                type=atype or "UNKNOWN",
                title="",
                source_file="",
                defined=False,
                origin=target_origin,
            )
        G.add_edge(
            trace.source_id,
            trace.target_id,
            context=trace.context,
            source_file=trace.rel_path,
            line=trace.line_number,
            relation_type=trace.relation_type,
        )


def _add_graph_native_artifact_nodes(
    G: nx.DiGraph,
    traces: list[GraphNativeArtifactTrace] | None,
    config: ProjectConfig,
):
    if not traces:
        return
    for trace in traces:
        if not G.has_node(trace.source_id):
            atype = classify_id(trace.source_id, config)
            source_origin = (
                config.types.get(atype).origin if atype in config.types else ""
            )
            G.add_node(
                trace.source_id,
                type=atype or "UNKNOWN",
                title=trace.source_id,
                source_file=trace.rel_path,
                defined=False,
                origin=source_origin,
            )
        if not G.has_node(trace.target_id):
            atype = classify_id(trace.target_id, config)
            target_origin = (
                config.types.get(atype).origin if atype in config.types else ""
            )
            G.add_node(
                trace.target_id,
                type=atype or "UNKNOWN",
                title="",
                source_file="",
                defined=False,
                origin=target_origin,
            )
        G.add_edge(
            trace.source_id,
            trace.target_id,
            context=trace.context,
            source_file=trace.rel_path,
            line=trace.line_number,
            relation_type=trace.relation_type,
        )


def _add_react_ui_nodes(
    G: nx.DiGraph,
    elements: list[ReactUiElement] | None,
    config: ProjectConfig,
):
    if not elements:
        return
    by_source: dict[str, list[ReactUiElement]] = defaultdict(list)
    for element in elements:
        by_source[element.source_id].append(element)
        origin = config.react_ui.origin if config.react_ui else "implementation"
        if not G.has_node(element.source_id):
            G.add_node(
                element.source_id,
                type=element.source_type,
                title=element.rel_path,
                source_file=element.rel_path,
                defined=True,
                origin=origin,
            )

        if element.target_id not in G:
            target_origin = (
                config.types.get(element.target_type).origin
                if element.target_type in config.types
                else origin
            )
            G.add_node(
                element.target_id,
                type=element.target_type,
                title=element.selector,
                source_file=element.rel_path,
                defined=True,
                origin=target_origin,
            )
        else:
            G.nodes[element.target_id]["type"] = element.target_type
            G.nodes[element.target_id]["title"] = element.selector
            G.nodes[element.target_id]["source_file"] = element.rel_path
            G.nodes[element.target_id]["defined"] = True
            if (
                "origin" not in G.nodes[element.target_id]
                or not G.nodes[element.target_id]["origin"]
            ):
                G.nodes[element.target_id]["origin"] = (
                    config.types.get(element.target_type).origin
                    if element.target_type in config.types
                    else origin
                )

        G.add_edge(
            element.source_id,
            element.target_id,
            context=element.context,
            source_file=element.rel_path,
            line=element.line_number,
            relation_type=element.relation_type,
        )

    for source_elements in by_source.values():
        _add_react_ui_ownership_edges(G, source_elements, config)


def _add_react_ui_ownership_edges(
    G: nx.DiGraph,
    elements: list[ReactUiElement],
    config: ProjectConfig,
):
    families = [e for e in elements if e.role == "screen_family"]
    screens = [e for e in elements if e.role == "screen"]
    components = [e for e in elements if e.role == "component"]
    if not config.react_ui:
        return

    for family in families:
        for screen in screens:
            if family.target_id == screen.target_id:
                continue
            G.add_edge(
                family.target_id,
                screen.target_id,
                context="same React source",
                source_file=screen.rel_path,
                line=screen.line_number,
                relation_type=config.react_ui.screen_family_relation_type,
            )

    owners = screens if screens else families
    for owner in owners:
        for component in components:
            if owner.target_id == component.target_id:
                continue
            G.add_edge(
                owner.target_id,
                component.target_id,
                context="same React source",
                source_file=component.rel_path,
                line=component.line_number,
                relation_type=config.react_ui.screen_component_relation_type,
            )


def _add_mini_registry_nodes(
    G: nx.DiGraph,
    traces: list[MiniRegistryTrace] | None,
    config: ProjectConfig,
):
    if not traces:
        return
    for trace in traces:
        if not G.has_node(trace.source_id):
            origin = (
                config.mini_registry.origin
                if config.mini_registry
                else "implementation"
            )
            G.add_node(
                trace.source_id,
                type=trace.source_type,
                title=trace.title,
                source_file=trace.rel_path,
                defined=True,
                origin=origin,
            )
        if not trace.target_id:
            continue
        if trace.target_id not in G:
            atype = classify_id(trace.target_id, config)
            target_origin = (
                config.types.get(atype).origin if atype in config.types else ""
            )
            G.add_node(
                trace.target_id,
                type=atype or "UNKNOWN",
                title="",
                source_file="",
                defined=False,
                origin=target_origin,
            )
        G.add_edge(
            trace.source_id,
            trace.target_id,
            context=trace.context,
            source_file=trace.rel_path,
            line=trace.line_number,
            relation_type=trace.relation_type,
        )


def _add_mini_admin_source_nodes(
    G: nx.DiGraph,
    traces: list[MiniAdminSourceTrace] | None,
    config: ProjectConfig,
):
    if not traces:
        return
    for trace in traces:
        origin = (
            config.mini_admin_sources.origin
            if config.mini_admin_sources
            else "implementation"
        )
        if not G.has_node(trace.source_id):
            G.add_node(
                trace.source_id,
                type=trace.source_type,
                title=trace.title,
                source_file=trace.rel_path,
                defined=True,
                origin=origin,
            )
        if not G.has_node(trace.target_id):
            G.add_node(
                trace.target_id,
                type=trace.target_type,
                title=trace.target_id,
                source_file=trace.rel_path,
                defined=True,
                origin=origin,
            )
        G.add_edge(
            trace.source_id,
            trace.target_id,
            context=trace.context,
            source_file=trace.rel_path,
            line=trace.line_number,
            relation_type=trace.relation_type,
        )


def _add_mini_admin_component_trace_nodes(
    G: nx.DiGraph,
    traces: list[MiniAdminComponentTrace] | None,
    config: ProjectConfig,
):
    if not traces:
        return
    origin = (
        config.mini_admin_sources.origin
        if config.mini_admin_sources
        else "implementation"
    )
    component_type = (
        config.mini_admin_sources.component_type if config.mini_admin_sources else "UIC"
    )
    component_relation_type = (
        config.mini_admin_sources.component_relation_type
        if config.mini_admin_sources
        else "CONTAINS"
    )
    seen_containment: set[tuple[str, str]] = set()
    for trace in traces:
        if not G.has_node(trace.screen_id):
            screen_type = classify_id(trace.screen_id, config) or "SCREEN_FAMILY"
            G.add_node(
                trace.screen_id,
                type=screen_type,
                title=trace.screen_id,
                source_file=trace.rel_path,
                defined=True,
                origin=origin,
            )
        if not G.has_node(trace.component_id):
            G.add_node(
                trace.component_id,
                type=component_type,
                title=trace.component_selector,
                source_file=trace.rel_path,
                defined=True,
                origin=origin,
            )
        containment_key = (trace.screen_id, trace.component_id)
        if containment_key not in seen_containment:
            G.add_edge(
                trace.screen_id,
                trace.component_id,
                context="mini_admin_component_trace",
                source_file=trace.rel_path,
                line=trace.line_number,
                relation_type=component_relation_type,
            )
            seen_containment.add(containment_key)
        if not G.has_node(trace.target_id):
            G.add_node(
                trace.target_id,
                type=trace.target_type,
                title=trace.target_id,
                source_file=trace.rel_path,
                defined=True,
                origin=origin,
            )
        G.add_edge(
            trace.component_id,
            trace.target_id,
            context=trace.context,
            source_file=trace.rel_path,
            line=trace.line_number,
            relation_type=trace.relation_type,
        )


def _add_source_ref_nodes(
    G: nx.DiGraph,
    refs: list[CodeReference] | None,
    prefix: str,
    node_type: str,
    config: ProjectConfig,
    relation_type: str,
    origin: str,
):
    """Add meta-nodes (CODE:/TEST:) with edges to referenced artifacts."""
    if not refs:
        return
    for cref in refs:
        node_id = f"{prefix}{cref.rel_path}"
        if not G.has_node(node_id):
            G.add_node(
                node_id,
                type=node_type,
                title=cref.rel_path,
                source_file=cref.rel_path,
                defined=True,
                origin=origin,
            )
        for target_id in cref.target_ids:
            if target_id not in G:
                atype = classify_id(target_id, config)
                target_origin = (
                    config.types.get(atype).origin if atype in config.types else ""
                )
                G.add_node(
                    target_id,
                    type=atype or "UNKNOWN",
                    title="",
                    source_file="",
                    defined=False,
                    origin=target_origin,
                )
            G.add_edge(
                node_id,
                target_id,
                context=cref.context,
                source_file=cref.rel_path,
                line=cref.line_number,
                relation_type=relation_type,
            )


def _resolve_dangling_variants(G: nx.DiGraph, registry: dict[str, Artifact]):
    """If an ID is referenced but not defined, link to its variants if they exist.

    E.g. BP-01 → BP-01a, BP-01b.
    """
    dangling = [
        n
        for n in G.nodes()
        if not G.nodes[n].get("defined", False) and not n.startswith("FILE:")
    ]
    for node_id in dangling:
        variants = [
            aid for aid in registry if aid.startswith(node_id) and aid != node_id
        ]
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

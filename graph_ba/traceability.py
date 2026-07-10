"""
Traceability Scanner — builds a cross-reference graph
of BA artifacts and reports coverage gaps, orphans, and dangling references.

Config-driven: artifact types, patterns, and scan rules are defined in graph-ba.toml.

Usage:
    trace-ba --root /path/to/project
    trace-ba --root . --json-out reports/graph.json --dot-out reports/graph.dot -v
"""

from __future__ import annotations

from pathlib import Path

import click

from graph_ba.config import load_config

from .graph_builder import _find_owner, build_graph
from .models import (
    Artifact,
    CodeReference,
    GraphNativeArtifactTrace,
    GraphNativeChangeTrace,
    MiniAdminComponentTrace,
    MiniAdminComponentTraceEntry,
    Reference,
    TraceReport,
)
from .provider_scanning import (
    MiniAdminSourceTrace,
    MiniRegistryTrace,
    ReactUiElement,
    build_mini_admin_component_trace_entries,
    export_mini_admin_component_trace_map,
    scan_mini_admin_component_traces,
    scan_mini_admin_source_traces,
    scan_mini_registry_traces,
    scan_react_ui_elements,
)
from .reporting import (
    _filter_graph,
    export_dot,
    export_html,
    export_index,
    export_json,
    print_report,
    verify,
)
from .scanning import (
    _graph_native_artifact_files,
    _graph_native_change_files,
    _parse_graph_native_attrs,
    _read_graph_native_change,
    expand_ranges,
    scan_code_references,
    scan_definitions,
    scan_graph_native_artifact_traces,
    scan_graph_native_change_traces,
    scan_index_cross_refs,
    scan_references,
    scan_test_references,
    scan_ui_references,
)

# ── Data model ────────────────────────────────────────────────────


# ── Phase 1: Definition scanning ─────────────────────────────────


# ── Phase 2: Reference extraction ────────────────────────────────


# ── Phase 2b: Code / test reference extraction ───────────────────


# ── Phase 3: Graph construction ──────────────────────────────────


# ── Phase 4: Verification checks ────────────────────────────────


# ── Output: Console report ───────────────────────────────────────


# ── Output: JSON export ──────────────────────────────────────────


# ── Output: DOT export ───────────────────────────────────────────

# Default colors for common types; config can extend/override


# ── Output: ARTIFACT_INDEX.md ────────────────────────────────────


# ── Output: Interactive HTML (vis-network) ───────────────────────


# ── CLI ───────────────────────────────────────────────────────────


@click.command()
@click.option(
    "--root",
    type=click.Path(exists=True, path_type=Path),
    default=".",
    help="Project root directory",
)
@click.option(
    "--json-out",
    type=click.Path(path_type=Path),
    default=None,
    help="Path for JSON graph export",
)
@click.option(
    "--dot-out",
    type=click.Path(path_type=Path),
    default=None,
    help="Path for DOT file export",
)
@click.option(
    "--html-out",
    type=click.Path(path_type=Path),
    default=None,
    help="Path for interactive HTML export",
)
@click.option(
    "--no-file-nodes", is_flag=True, help="Exclude FILE nodes from visual exports"
)
@click.option("--no-transitive", is_flag=True, help="Remove transitive edges")
@click.option(
    "--index",
    "index_out",
    type=click.Path(path_type=Path),
    default=None,
    help="Path for ARTIFACT_INDEX.md",
)
@click.option("--index-auto", is_flag=True, help="Generate index at default path")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed info")
def main(
    root: Path,
    json_out: Path | None,
    dot_out: Path | None,
    html_out: Path | None,
    no_file_nodes: bool,
    no_transitive: bool,
    index_out: Path | None,
    index_auto: bool,
    verbose: bool,
):
    """Traceability Scanner — parse BA artifacts and verify cross-references."""
    root = root.resolve()
    config = load_config(root)

    registry = scan_definitions(root, config)
    if verbose:
        print(f"[scan] {len(registry)} artifact definitions found")

    references = scan_references(root, registry, config)
    index_xrefs = scan_index_cross_refs(root, config)
    code_refs = scan_code_references(root, config)
    test_refs = scan_test_references(root, config)
    graph_native_change_traces = scan_graph_native_change_traces(root, config)
    graph_native_artifact_traces = scan_graph_native_artifact_traces(root, config)
    if verbose:
        print(
            f"[scan] {len(references)} references found, {len(index_xrefs)} index cross-refs, "
            f"{len(code_refs)} code trace refs, {len(test_refs)} test refs, "
            f"{len(graph_native_change_traces)} graph-native change refs, "
            f"{len(graph_native_artifact_traces)} graph-native artifact refs"
        )

    G = build_graph(
        registry,
        references,
        config,
        index_xrefs,
        code_refs,
        test_refs,
        graph_native_change_traces=graph_native_change_traces,
        graph_native_artifact_traces=graph_native_artifact_traces,
    )
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

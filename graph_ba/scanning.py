"""
Traceability Scanner — builds a cross-reference graph
of BA artifacts and reports coverage gaps, orphans, and dangling references.

Config-driven: artifact types, patterns, and scan rules are defined in graph-ba.toml.

Usage:
    trace-ba --root /path/to/project
    trace-ba --root . --json-out reports/graph.json --dot-out reports/graph.dot -v
"""

from __future__ import annotations

import re
from pathlib import Path

from graph_ba.config import ProjectConfig, classify_id, normalize_id

from .models import (
    Artifact,
    CodeReference,
    GraphNativeArtifactTrace,
    GraphNativeChangeTrace,
    Reference,
)


GRAPH_NATIVE_LINK_ATTRS = {
    "implements": "IMPLEMENTS",
    "depends_on": "DEPENDS_ON",
    "verifies": "VERIFIES",
    "renders": "RENDERS",
    "contains": "CONTAINS",
    "traces_to": "TRACES_TO",
    "normalizes": "NORMALIZES",
    "supersedes": "SUPERSEDES",
    "conflicts_with": "CONFLICTS_WITH",
}


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _register(registry: dict[str, Artifact], art: Artifact, config: ProjectConfig):
    nid = normalize_id(art.id, config)
    art.id = nid
    if nid not in registry:
        registry[nid] = art


def scan_definitions(root: Path, config: ProjectConfig) -> dict[str, Artifact]:
    """Scan artifact definitions using rules from config."""
    registry: dict[str, Artifact] = {}

    for rule in config.definitions:
        file_str = rule.file
        # Support glob patterns in file field
        if "*" in file_str or "?" in file_str:
            # Glob: scan matching files
            matched = sorted(root.glob(file_str))
            for f in matched:
                if rule.mode == "heading":
                    _scan_heading(registry, f, rule.pattern, rule.type_id, config)
                elif rule.mode == "table":
                    _scan_table_first_col(
                        registry, f, rule.pattern, rule.type_id, config
                    )
        else:
            filepath = root / file_str
            if rule.mode == "heading":
                _scan_heading(registry, filepath, rule.pattern, rule.type_id, config)
            elif rule.mode == "table":
                _scan_table_first_col(
                    registry, filepath, rule.pattern, rule.type_id, config
                )

    _scan_graph_native_definitions(registry, root, config)
    return registry


def scan_definition_occurrences(
    root: Path,
    config: ProjectConfig,
) -> dict[str, list[Artifact]]:
    """Return every definition occurrence before stable-ID owner selection."""
    occurrences: dict[str, list[Artifact]] = {}

    def add(artifact: Artifact) -> None:
        artifact.id = normalize_id(artifact.id, config)
        occurrences.setdefault(artifact.id, []).append(artifact)

    for rule in config.definitions:
        paths = (
            sorted(root.glob(rule.file))
            if "*" in rule.file or "?" in rule.file
            else [root / rule.file]
        )
        for filepath in paths:
            if not filepath.exists():
                continue
            for line_number, line in enumerate(_read_lines(filepath), 1):
                match = rule.pattern.match(line)
                if not match or match.group(1) is None:
                    continue
                title = ""
                if rule.mode == "heading" and match.lastindex and match.lastindex >= 2:
                    title = match.group(2).strip()
                elif rule.mode == "table":
                    columns = [column.strip() for column in line.split("|")]
                    title = columns[2] if len(columns) > 2 else ""
                add(Artifact(match.group(1), rule.type_id, filepath, line_number, title))

    if config.graph_native:
        for filepath in _graph_native_artifact_files(root, config):
            for line_number, line in enumerate(_read_lines(filepath), 1):
                marker = re.match(r"^\s*:::artifact\s+(.+?)\s*$", line)
                if not marker:
                    continue
                attrs = _parse_graph_native_attrs(marker.group(1))
                if attrs.get("id") and attrs.get("type"):
                    add(
                        Artifact(
                            attrs["id"],
                            attrs["type"],
                            filepath,
                            line_number,
                            attrs.get("title", ""),
                            attrs.get("origin", ""),
                        )
                    )
    return occurrences


def canonical_ownership_findings(
    root: Path,
    config: ProjectConfig,
    artifact_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Report ambiguous canonical definitions or explicit migration shadows."""
    findings: list[dict[str, object]] = []
    occurrences = scan_definition_occurrences(root, config)
    for artifact_id, items in sorted(occurrences.items()):
        if artifact_ids is not None and artifact_id not in artifact_ids:
            continue
        canonical = [
            item
            for item in items
            if (item.origin or getattr(config.types.get(item.artifact_type), "origin", ""))
            == "canonical"
        ]
        if len(canonical) < 2:
            continue
        files = {item.source_file.resolve() for item in canonical}
        if len(files) == 1:
            path = next(iter(files))
            relative = str(path.relative_to(root))
            findings.append({
                "severity": "ERR",
                "category": "DUPLICATE_CANONICAL_OWNER",
                "artifact_id": artifact_id,
                "file": relative,
                "line": 0,
                "message": f"duplicate canonical definitions in one file: {relative}",
            })
            continue
        owners = [
            path
            for path in files
            if "<!-- graph-ba: canonical-owner -->" in path.read_text(encoding="utf-8")
        ]
        relative_files = sorted(str(path.relative_to(root)) for path in files)
        if len(owners) == 1:
            owner = str(owners[0].relative_to(root))
            findings.append({
                "severity": "INFO",
                "category": "CANONICAL_MIGRATION_SHADOW",
                "artifact_id": artifact_id,
                "file": owner,
                "line": 0,
                "message": f"canonical owner {owner}; shadowed definitions: "
                + ", ".join(path for path in relative_files if path != owner),
            })
        else:
            findings.append({
                "severity": "ERR",
                "category": "DUPLICATE_CANONICAL_OWNER",
                "artifact_id": artifact_id,
                "file": "",
                "line": 0,
                "message": "expected exactly one canonical-owner marker across: "
                + ", ".join(relative_files),
            })
    return findings


def _scan_heading(registry, filepath, pattern, type_id, config):
    if not filepath.exists():
        return
    for i, line in enumerate(_read_lines(filepath), 1):
        m = pattern.match(line)
        if m:
            raw_id = m.group(1)
            if raw_id is None:
                continue
            title = m.group(2).strip() if m.lastindex >= 2 else ""
            _register(registry, Artifact(raw_id, type_id, filepath, i, title), config)


def _scan_table_first_col(registry, filepath, pattern, type_id, config):
    if not filepath.exists():
        return
    for i, line in enumerate(_read_lines(filepath), 1):
        m = pattern.match(line)
        if m:
            raw_id = m.group(1)
            if raw_id is None:
                continue
            cols = [c.strip() for c in line.split("|")]
            title = cols[2] if len(cols) > 2 else ""
            _register(registry, Artifact(raw_id, type_id, filepath, i, title), config)


def _scan_graph_native_definitions(
    registry: dict[str, Artifact],
    root: Path,
    config: ProjectConfig,
):
    if not config.graph_native:
        return
    for filepath in _graph_native_artifact_files(root, config):
        _scan_graph_native_artifact_file(registry, filepath, config)
    for filepath in _graph_native_change_files(root, config):
        change = _read_graph_native_change(filepath)
        change_id = change.get("id")
        if not change_id:
            continue
        title = change.get("title", "")
        _register(
            registry,
            Artifact(
                change_id,
                config.graph_native.change_type,
                filepath,
                int(change.get("_line", 1)),
                title,
            ),
            config,
        )


def _scan_graph_native_artifact_file(
    registry: dict[str, Artifact],
    filepath: Path,
    config: ProjectConfig,
):
    if not filepath.exists():
        return
    for line_number, line in enumerate(_read_lines(filepath), 1):
        marker = re.match(r"^\s*:::artifact\s+(.+?)\s*$", line)
        if not marker:
            continue
        attrs = _parse_graph_native_attrs(marker.group(1))
        artifact_id = attrs.get("id")
        type_id = attrs.get("type")
        if not artifact_id or not type_id:
            continue
        title = attrs.get("title", "")
        _register(
            registry,
            Artifact(
                artifact_id,
                type_id,
                filepath,
                line_number,
                title,
                attrs.get("origin", ""),
            ),
            config,
        )


def _parse_graph_native_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, double_quoted, single_quoted, bare in re.findall(
        r"([A-Za-z_][\w-]*)=(?:\"([^\"]*)\"|'([^']*)'|([^\s]+))",
        raw,
    ):
        attrs[key] = double_quoted or single_quoted or bare
    return attrs


def _graph_native_artifact_files(root: Path, config: ProjectConfig) -> list[Path]:
    if not config.graph_native:
        return []
    files: set[Path] = set()
    for dir_str in config.graph_native.dirs:
        base = root / dir_str
        if not base.exists():
            continue
        for ext in config.graph_native.artifact_extensions:
            files.update(base.rglob(f"*.{ext.lstrip('.')}"))
    for pattern in config.graph_native.files:
        files.update(root.glob(pattern))
    return sorted(path for path in files if path.is_file())


def _graph_native_change_files(root: Path, config: ProjectConfig) -> list[Path]:
    if not config.graph_native:
        return []
    files: set[Path] = set()
    for pattern in config.graph_native.change_files:
        files.update(root.glob(pattern))
    # The single-file Git-native layout is a core convention. Keep importing
    # it when an older project config only lists the legacy directory layout.
    files.update(root.glob(".graphba/changes/*.yaml"))
    return sorted(path for path in files if path.is_file())


def _read_graph_native_change(filepath: Path) -> dict[str, object]:
    """Read the small supported subset of change.yaml without extra deps."""
    data: dict[str, object] = {}
    lists: dict[str, list[str]] = {"scope": [], "sources": []}
    active_list = ""
    for line_number, raw_line in enumerate(_read_lines(filepath), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") and active_list:
            value = line[1:].strip().strip("\"'")
            if value:
                lists[active_list].append(value)
            continue
        active_list = ""
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in lists:
            active_list = key
            if value.startswith("[") and value.endswith("]"):
                lists[key].extend(
                    item.strip().strip("\"'")
                    for item in value[1:-1].split(",")
                    if item.strip()
                )
            continue
        if key in {
            "id",
            "title",
            "intent",
            "base_ref",
            "target_ref",
            "state",
            "mode",
        }:
            data[key] = value.strip("\"'")
            data.setdefault("_line", line_number)
    for key, values in lists.items():
        if values:
            data[key] = values
    return data


def expand_ranges(text: str, config: ProjectConfig) -> list[str]:
    """Expand ranges like BR.12.1–BR.12.6 into individual IDs."""
    results = []
    for m in config.range_pattern.finditer(text):
        prefix, start_s, end_s = m.group(1), m.group(2), m.group(3)
        for i in range(int(start_s), int(end_s) + 1):
            results.append(f"{prefix}{i}")
    return results


def scan_index_cross_refs(
    root: Path, config: ProjectConfig
) -> list[tuple[str, str, Path, int]]:
    """Parse index tables where first column is the 'source' artifact and other
    columns contain 'target' artifact IDs."""
    results: list[tuple[str, str, Path, int]] = []

    for rule in config.index_tables:
        filepath = root / rule.file
        _parse_index_table(results, filepath, rule.first_col_pattern, config)

    return results


def scan_graph_native_change_traces(
    root: Path,
    config: ProjectConfig,
) -> list[GraphNativeChangeTrace]:
    """Read Change scope lists and expose them as graph edges."""
    if not config.graph_native:
        return []
    traces: list[GraphNativeChangeTrace] = []
    for filepath in _graph_native_change_files(root, config):
        change = _read_graph_native_change(filepath)
        change_id = change.get("id")
        if not change_id:
            continue
        try:
            rel_path = str(filepath.relative_to(root))
        except ValueError:
            rel_path = str(filepath)
        line_number = int(change.get("_line", 1))
        for target in change.get("scope", []):
            if not isinstance(target, str) or not target:
                continue
            traces.append(
                GraphNativeChangeTrace(
                    source_id=normalize_id(str(change_id), config),
                    source_file=filepath,
                    line_number=line_number,
                    target_id=normalize_id(target, config),
                    relation_type=config.graph_native.scope_relation_type,
                    context="graph_native_change_scope",
                    rel_path=rel_path,
                )
            )
    return traces


def scan_graph_native_artifact_traces(
    root: Path,
    config: ProjectConfig,
) -> list[GraphNativeArtifactTrace]:
    """Read typed link attributes from graph-native artifact blocks."""
    if not config.graph_native:
        return []
    traces: list[GraphNativeArtifactTrace] = []
    for filepath in _graph_native_artifact_files(root, config):
        try:
            rel_path = str(filepath.relative_to(root))
        except ValueError:
            rel_path = str(filepath)
        for line_number, line in enumerate(_read_lines(filepath), 1):
            marker = re.match(r"^\s*:::artifact\s+(.+?)\s*$", line)
            if not marker:
                continue
            attrs = _parse_graph_native_attrs(marker.group(1))
            source_id = attrs.get("id")
            if not source_id:
                continue
            for attr_name, relation_type in GRAPH_NATIVE_LINK_ATTRS.items():
                raw_targets = attrs.get(attr_name, "")
                if not raw_targets:
                    continue
                for raw_target in _split_graph_native_targets(raw_targets):
                    traces.append(
                        GraphNativeArtifactTrace(
                            source_id=normalize_id(source_id, config),
                            source_file=filepath,
                            line_number=line_number,
                            target_id=normalize_id(raw_target, config),
                            relation_type=relation_type,
                            context=f"graph_native:{attr_name}",
                            rel_path=rel_path,
                        )
                    )
    return traces


def _split_graph_native_targets(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_index_table(
    results: list[tuple[str, str, Path, int]],
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
        rest = line[m.end() :]

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
    registry: dict[str, Artifact],
    config: ProjectConfig,
) -> list[Reference]:
    """Scan all .md files for cross-references to known artifact types."""
    scan_dirs = [root / d for d in config.scan_dirs]
    md_files: list[Path] = []
    for d in scan_dirs:
        if d.exists():
            md_files.extend(sorted(d.rglob("*.md")))

    # Build restriction sets for types with restrict_to
    restrict_files: dict[str, set[Path]] = {}
    for tid, tdef in config.types.items():
        if tdef.restrict_to:
            files: set[Path] = set()
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

    all_refs: list[Reference] = []

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


def _scan_source_references(
    root: Path,
    dirs: list[str],
    extensions: list[str],
    extract_raw_ids,
    config: ProjectConfig,
) -> list[CodeReference]:
    """Generic per-line scanner over source files.

    `extract_raw_ids(line) -> List[str]` returns raw artifact ID candidates
    for a line; they are normalized and kept only if classifiable.
    """
    files: list[Path] = []
    for dir_str in dirs:
        d = root / dir_str
        if not d.exists():
            continue
        for ext in extensions:
            files.extend(sorted(d.rglob(f"*.{ext}")))

    return _scan_file_references(root, files, extract_raw_ids, config)


def _scan_file_references(
    root: Path,
    files: list[Path],
    extract_raw_ids,
    config: ProjectConfig,
) -> list[CodeReference]:
    results: list[CodeReference] = []

    for filepath in files:
        try:
            lines = filepath.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue

        try:
            rel_path = str(filepath.relative_to(root))
        except ValueError:
            rel_path = str(filepath)

        for line_num, line in enumerate(lines, 1):
            target_ids = []
            for raw_id in extract_raw_ids(line):
                raw_id = raw_id.strip(".,;:")
                if not raw_id:
                    continue
                nid = normalize_id(raw_id, config)
                if classify_id(nid, config) is not None:
                    target_ids.append(nid)

            if target_ids:
                results.append(
                    CodeReference(
                        code_file=filepath,
                        line_number=line_num,
                        target_ids=target_ids,
                        context=line.strip(),
                        rel_path=rel_path,
                    )
                )

    return results


def scan_code_references(
    root: Path,
    config: ProjectConfig,
    *,
    warn_provider: bool = True,
) -> list[CodeReference]:
    """Scan source code files for @trace comments referencing BA artifacts."""
    if not config.code or not config.code.comment_pattern:
        return []

    comment_re = config.code.comment_pattern

    def extract(line: str) -> list[str]:
        m = comment_re.match(line)
        if not m:
            return []
        raw_ids_str = m.group(1).strip()
        return [s.strip() for s in re.split(r"[,\s]+", raw_ids_str) if s.strip()]

    refs = _scan_source_references(
        root, config.code.dirs, config.code.extensions, extract, config
    )
    if config.codegraph:
        from .codegraph_provider import enrich_code_references

        enrich_code_references(
            root,
            refs,
            config.codegraph.database,
            warn=warn_provider,
        )
    return refs


def scan_test_references(
    root: Path,
    config: ProjectConfig,
) -> list[CodeReference]:
    """Scan test files for artifact ID references (TEST: nodes).

    Unlike code scanning, no @trace marker is required: any artifact ID
    matching a configured type ref pattern counts as test evidence.

    Files come from `dirs` (recursive, filtered by `extensions`) plus
    `files` glob patterns — the latter for colocated tests (e.g.
    `src/**/*.test.ts`) where scanning the whole dir would count ordinary
    source mentions as evidence.
    """
    if not config.tests:
        return []

    type_patterns = [tdef.ref_pattern for tdef in config.types.values()]

    def extract(line: str) -> list[str]:
        ids: list[str] = []
        for pattern in type_patterns:
            for m in pattern.finditer(line):
                ids.append(m.group(1))
        return ids

    refs = _scan_source_references(
        root, config.tests.dirs, config.tests.extensions, extract, config
    )

    if config.tests.files:
        glob_files: list[Path] = []
        for pattern in config.tests.files:
            glob_files.extend(sorted(root.glob(pattern)))
        seen = {r.code_file for r in refs}
        glob_files = [f for f in glob_files if f not in seen]
        refs.extend(_scan_file_references(root, glob_files, extract, config))

    return refs


def scan_ui_references(
    root: Path,
    config: ProjectConfig,
) -> list[CodeReference]:
    """Scan UI trace sidecar files for artifact ID references (UI: nodes).

    Files come from root-relative glob patterns in [ui].files. Like test
    scanning, no marker is required: any artifact ID matching a configured
    type ref pattern counts as a UI-to-artifact link.
    """
    if not config.ui:
        return []

    files: list[Path] = []
    for pattern in config.ui.files:
        files.extend(sorted(root.glob(pattern)))

    type_patterns = [tdef.ref_pattern for tdef in config.types.values()]

    def extract(line: str) -> list[str]:
        ids: list[str] = []
        for pattern in type_patterns:
            for m in pattern.finditer(line):
                ids.append(m.group(1))
        return ids

    return _scan_file_references(root, files, extract, config)

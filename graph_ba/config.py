"""Configuration loader for graph-ba projects.

Reads graph-ba.toml from the project root and provides structured config
for the traceability scanner.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib


CONFIG_FILENAME = "graph-ba.toml"

DEFAULT_ORIGINS = {
    "human": {
        "label": "Human primary source",
        "description": "Client, stakeholder, refined meeting or human dictation input.",
    },
    "derived": {
        "label": "Derived analysis artifact",
        "description": "Agent or analyst normalization derived from source artifacts.",
    },
    "canonical": {
        "label": "Canonical contract",
        "description": "Accepted/current product or requirement contract.",
    },
    "evidence": {
        "label": "Evidence",
        "description": "Test, UI trace, review or runtime evidence artifact.",
    },
    "implementation": {
        "label": "Implementation",
        "description": "Code or generated implementation artifact.",
    },
    "container": {
        "label": "Container",
        "description": "Synthetic file/container node created by graph-ba.",
    },
    "unknown": {
        "label": "Unknown or mixed",
        "description": "Origin is not classified by the project config.",
    },
}

DEFAULT_RELATIONS = {
    "MENTIONS": {
        "label": "Mentions",
        "description": "Weak textual mention; useful for navigation, not acceptance.",
        "direction": "source_mentions_target",
    },
    "INDEX": {
        "label": "Index",
        "description": "Configured index-table link.",
        "direction": "source_index_row_to_target",
    },
    "CODE_TRACE": {
        "label": "Code trace",
        "description": "Code marker references an artifact.",
        "direction": "code_to_artifact",
    },
    "TEST_EVIDENCE": {
        "label": "Test evidence",
        "description": "Test file references an artifact as evidence.",
        "direction": "test_to_artifact",
    },
    "UI_TRACE": {
        "label": "UI trace",
        "description": "UI trace artifact references a rendered artifact.",
        "direction": "ui_to_artifact",
    },
    "DERIVES_FROM": {
        "label": "Derives from",
        "description": "Source material used to derive this artifact.",
        "direction": "derived_to_source",
    },
    "NORMALIZES": {
        "label": "Normalizes",
        "description": "Canonical artifact refines raw/source material.",
        "direction": "canonical_to_source",
    },
    "IMPLEMENTS": {
        "label": "Implements",
        "description": "Implementation realizes a contract or semantic artifact.",
        "direction": "implementation_to_contract",
    },
    "DEPENDS_ON": {
        "label": "Depends on",
        "description": "Artifact needs another artifact for behavior, data or context.",
        "direction": "source_to_dependency",
    },
    "CONTAINS": {
        "label": "Contains",
        "description": "Container scopes a child artifact.",
        "direction": "container_to_child",
    },
    "TRACES_TO": {
        "label": "Traces to",
        "description": "Explicit trace without stronger semantics.",
        "direction": "source_to_target",
    },
    "VERIFIES": {
        "label": "Verifies",
        "description": "Evidence proves the target artifact.",
        "direction": "evidence_to_contract",
    },
    "RENDERS": {
        "label": "Renders",
        "description": "UI/source artifact renders a visible target.",
        "direction": "ui_to_contract",
    },
    "CONFLICTS_WITH": {
        "label": "Conflicts with",
        "description": "Artifacts cannot both be true as written.",
        "direction": "symmetric",
    },
    "SUPERSEDES": {
        "label": "Supersedes",
        "description": "New artifact replaces an older artifact.",
        "direction": "new_to_old",
    },
    "TRACE_GAP": {
        "label": "Trace gap",
        "description": "Expected edge is deliberately missing.",
        "direction": "source_to_gap",
    },
}


@dataclass
class EnumDef:
    """Project-configurable enum value for origins and relation types."""
    id: str
    label: str = ""
    description: str = ""
    direction: str = ""


@dataclass
class TypeDef:
    """Definition of an artifact type."""
    id: str
    label: str
    ref_pattern: re.Pattern  # compiled regex for finding references
    classify_pattern: Optional[re.Pattern] = None  # for classifying an ID string
    restrict_to: Optional[List[str]] = None  # only match in these files/dirs
    origin: str = ""  # optional provenance class, e.g. human / derived / evidence


@dataclass
class DefinitionRule:
    """Rule for scanning artifact definitions in files."""
    type_id: str
    file: str  # relative path or glob (e.g. "02_Discovery/06_Business_Rules/BR-*.md")
    mode: str  # "heading" or "table"
    pattern: re.Pattern  # compiled regex


@dataclass
class IndexTableRule:
    """Index table for extracting cross-references from table rows."""
    file: str
    first_col_pattern: re.Pattern


@dataclass
class CoveragePair:
    source: str
    target: str
    label: str


@dataclass
class NormalizeRule:
    """Normalization rule for artifact IDs."""
    char_map: Dict[str, str] = field(default_factory=dict)
    zero_pad: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class CodeConfig:
    """Configuration for scanning source code files for @trace references."""
    dirs: List[str]
    extensions: List[str]
    marker: str = "@trace"
    comment_pattern: Optional[re.Pattern] = None
    coverage_types: List[str] = field(default_factory=list)


@dataclass
class TestsConfig:
    """Configuration for scanning test files for artifact ID references."""
    dirs: List[str]
    extensions: List[str] = field(default_factory=lambda: [
        "py", "ts", "tsx", "js", "dart",
    ])
    # Root-relative glob patterns for colocated tests (e.g. "src/**/*.test.ts")
    files: List[str] = field(default_factory=list)
    coverage_types: List[str] = field(default_factory=list)


@dataclass
class UiConfig:
    """Configuration for scanning UI trace sidecar files (UI: nodes).

    `files` are root-relative glob patterns pointing at machine-readable
    trace files (e.g. feature-level trace.json mapping data-testid -> AC IDs).
    Any artifact ID matching a configured type ref pattern counts as a
    UI-to-artifact link.
    """
    files: List[str]
    coverage_types: List[str] = field(default_factory=list)


@dataclass
class GraphNativeConfig:
    """Configuration for graph-native Artifact/Change source files.

    Artifact blocks are lightweight markdown blocks:
    `:::artifact type="AC" id="AC-..." state="draft" title="..."`
    The body remains free-form markdown and is scanned for normal refs.

    Change files are deliberately small YAML-like files. graph-ba reads only
    top-level `id`, `title`, `state`, `mode` and a `scope` list so projects do
    not need a YAML dependency for the core workflow.
    """
    dirs: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    artifact_extensions: List[str] = field(default_factory=lambda: ["md"])
    change_files: List[str] = field(default_factory=lambda: [".graphba/changes/*/change.yaml"])
    change_type: str = "CHG"
    change_origin: str = "derived"
    scope_relation_type: str = "DEPENDS_ON"


@dataclass
class LintConfig:
    """Configuration for the lint command."""
    glossary_file: Optional[str] = None
    meetings_dir: str = "00_Inputs/meetings_refined"
    stale_threshold_days: int = 30
    todo_patterns: List[str] = field(default_factory=lambda: [
        "TODO", "TBD", "FIXME", "???",
    ])


@dataclass
class ProjectConfig:
    """Full project configuration."""
    scan_dirs: List[str]
    types: Dict[str, TypeDef]
    type_order: List[str]  # ordered list of type IDs (for display)
    definitions: List[DefinitionRule]
    index_tables: List[IndexTableRule]
    coverage_pairs: List[CoveragePair]
    clusters: Dict[str, List[str]]
    normalize: NormalizeRule
    range_pattern: re.Pattern
    origins: Dict[str, EnumDef]
    relation_types: Dict[str, EnumDef]
    # Review validation
    required_sections: Dict[str, List[str]]
    expected_bidir: Dict[str, List[str]]
    expected_cross_layer: Dict[str, List[Tuple[str, str]]]  # type -> [(target_type, label)]
    # Code traceability
    code: Optional[CodeConfig] = None
    # Test traceability
    tests: Optional[TestsConfig] = None
    # UI traceability
    ui: Optional[UiConfig] = None
    # graph-native Artifact/Change workflow
    graph_native: Optional[GraphNativeConfig] = None
    # Lint
    lint: Optional[LintConfig] = None


def load_config(root: Path) -> ProjectConfig:
    """Load config from graph-ba.toml in the project root."""
    config_path = root / CONFIG_FILENAME
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found: {config_path}\n"
            f"Run 'graph-ba init' in the project root to create one."
        )

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    # ── Scan dirs ──
    scan_dirs = data.get("scan", {}).get("dirs", [])

    # ── Types ──
    types: Dict[str, TypeDef] = {}
    type_order: List[str] = []
    for tid, tdata in data.get("types", {}).items():
        restrict = tdata.get("restrict_to")
        types[tid] = TypeDef(
            id=tid,
            label=tdata.get("label", tid),
            ref_pattern=re.compile(tdata["ref"]),
            classify_pattern=re.compile(tdata["classify"]) if "classify" in tdata else None,
            restrict_to=restrict,
            origin=tdata.get("origin", ""),
        )
        type_order.append(tid)

    # ── Definitions ──
    definitions: List[DefinitionRule] = []
    for d in data.get("definitions", []):
        definitions.append(DefinitionRule(
            type_id=d["type"],
            file=d["file"],
            mode=d["mode"],
            pattern=re.compile(d["pattern"]),
        ))

    # ── Index tables ──
    index_tables: List[IndexTableRule] = []
    for it in data.get("index_tables", []):
        index_tables.append(IndexTableRule(
            file=it["file"],
            first_col_pattern=re.compile(it["first_col"]),
        ))

    # ── Coverage ──
    coverage_pairs: List[CoveragePair] = []
    for c in data.get("coverage", []):
        coverage_pairs.append(CoveragePair(
            source=c["source"],
            target=c["target"],
            label=c.get("label", f"{c['source']} ↔ {c['target']}"),
        ))

    # ── Clusters ──
    clusters = data.get("clusters", {})

    # ── Normalize ──
    norm_data = data.get("normalize", {})
    normalize = NormalizeRule(
        char_map=norm_data.get("char_map", {}),
        zero_pad=norm_data.get("zero_pad", []),
    )

    # ── Range pattern ──
    rp = data.get("range_pattern",
                   r'((?:BR|BF)\.\d+\.)(\d+)\s*[–\-]\s*(?:(?:BR|BF)\.\d+\.)(\d+)')
    range_pat = re.compile(rp)

    # ── Artifact origins / edge relation types ──
    origins = _load_enum_defs(DEFAULT_ORIGINS, data.get("origins", {}))
    relation_types = _load_enum_defs(DEFAULT_RELATIONS, data.get("relations", {}))
    for tdef in types.values():
        if tdef.origin and tdef.origin not in origins:
            origins[tdef.origin] = EnumDef(id=tdef.origin, label=tdef.origin)

    # ── Review config ──
    review = data.get("review", {})
    required_sections = review.get("required_sections", {})
    expected_bidir = review.get("expected_bidir", {})
    expected_cross_layer_raw = review.get("expected_cross_layer", {})
    expected_cross_layer: Dict[str, List[Tuple[str, str]]] = {}
    for atype, pairs in expected_cross_layer_raw.items():
        expected_cross_layer[atype] = [(p["type"], p["label"]) for p in pairs]

    # ── Code scan config ──
    code_data = data.get("code")
    code_config = None
    if code_data:
        marker = code_data.get("marker", "@trace")
        escaped_marker = re.escape(marker)
        comment_re = re.compile(
            rf'^\s*(?://+|/?\*+|#+|--)\s*{escaped_marker}:\s*(.+)'
        )
        code_config = CodeConfig(
            dirs=code_data.get("dirs", []),
            extensions=code_data.get("extensions", [
                "ts", "tsx", "js", "jsx", "mjs", "cjs",
                "py", "pyw",
                "go",
                "rs",
                "java", "kt", "kts", "scala",
                "cs",
                "c", "h", "cpp", "hpp", "cc", "cxx",
                "swift",
                "rb",
                "php",
                "lua",
                "sh", "bash", "zsh",
                "sql",
                "dart",
                "ex", "exs",
                "zig",
                "vue", "svelte",
            ]),
            marker=marker,
            comment_pattern=comment_re,
            coverage_types=code_data.get("coverage_types", []),
        )

    # ── Tests scan config ──
    tests_data = data.get("tests")
    tests_config = None
    if tests_data:
        tests_config = TestsConfig(
            dirs=tests_data.get("dirs", []),
            extensions=tests_data.get("extensions", TestsConfig(dirs=[]).extensions),
            files=tests_data.get("files", []),
            coverage_types=tests_data.get("coverage_types", []),
        )

    # ── UI scan config ──
    ui_data = data.get("ui")
    ui_config = None
    if ui_data:
        ui_config = UiConfig(
            files=ui_data.get("files", []),
            coverage_types=ui_data.get("coverage_types", []),
        )

    # ── graph-native Artifact/Change workflow ──
    graph_native_data = data.get("graph_native")
    graph_native_config = None
    if graph_native_data:
        graph_native_defaults = GraphNativeConfig()
        graph_native_config = GraphNativeConfig(
            dirs=graph_native_data.get("dirs", []),
            files=graph_native_data.get("files", []),
            artifact_extensions=graph_native_data.get(
                "artifact_extensions",
                graph_native_defaults.artifact_extensions,
            ),
            change_files=graph_native_data.get(
                "change_files",
                graph_native_defaults.change_files,
            ),
            change_type=graph_native_data.get("change_type", graph_native_defaults.change_type),
            change_origin=graph_native_data.get(
                "change_origin",
                graph_native_defaults.change_origin,
            ),
            scope_relation_type=graph_native_data.get(
                "scope_relation_type",
                graph_native_defaults.scope_relation_type,
            ),
        )
        if graph_native_config.change_origin not in origins:
            origins[graph_native_config.change_origin] = EnumDef(
                id=graph_native_config.change_origin,
                label=graph_native_config.change_origin,
            )

    # ── Lint config ──
    lint_data = data.get("lint")
    lint_config = None
    if lint_data:
        lint_config = LintConfig(
            glossary_file=lint_data.get("glossary_file"),
            meetings_dir=lint_data.get("meetings_dir", "00_Inputs/meetings_refined"),
            stale_threshold_days=lint_data.get("stale_threshold_days", 30),
            todo_patterns=lint_data.get("todo_patterns", LintConfig().todo_patterns),
        )

    return ProjectConfig(
        scan_dirs=scan_dirs,
        types=types,
        type_order=type_order,
        definitions=definitions,
        index_tables=index_tables,
        coverage_pairs=coverage_pairs,
        clusters=clusters,
        normalize=normalize,
        range_pattern=range_pat,
        origins=origins,
        relation_types=relation_types,
        required_sections=required_sections,
        expected_bidir=expected_bidir,
        expected_cross_layer=expected_cross_layer,
        code=code_config,
        tests=tests_config,
        ui=ui_config,
        graph_native=graph_native_config,
        lint=lint_config,
    )


def _load_enum_defs(defaults: Dict[str, Dict[str, str]],
                    overrides: Dict[str, Dict[str, str]]) -> Dict[str, EnumDef]:
    """Merge built-in enum suggestions with project-specific definitions."""
    values: Dict[str, EnumDef] = {}
    for key, raw in defaults.items():
        values[key] = EnumDef(
            id=key,
            label=raw.get("label", key),
            description=raw.get("description", ""),
            direction=raw.get("direction", ""),
        )
    for key, raw in overrides.items():
        base = values.get(key, EnumDef(id=key, label=key))
        values[key] = EnumDef(
            id=key,
            label=raw.get("label", base.label or key),
            description=raw.get("description", base.description),
            direction=raw.get("direction", base.direction),
        )
    return values


def normalize_id(raw: str, config: ProjectConfig) -> str:
    """Canonical form of artifact ID using config rules."""
    s = raw
    for src, dst in config.normalize.char_map.items():
        s = s.replace(src, dst)
    for rule in config.normalize.zero_pad:
        m = re.fullmatch(rule["pattern"], s)
        if m:
            s = rule["format"].format(int(m.group(1)))
            break
    return s


def classify_id(raw: str, config: ProjectConfig) -> Optional[str]:
    """Classify an artifact ID string into its type using config patterns."""
    nid = normalize_id(raw, config)
    for tid, tdef in config.types.items():
        if tdef.classify_pattern and tdef.classify_pattern.fullmatch(nid):
            return tid
    return None

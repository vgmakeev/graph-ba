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


@dataclass
class TypeDef:
    """Definition of an artifact type."""
    id: str
    label: str
    ref_pattern: re.Pattern  # compiled regex for finding references
    classify_pattern: Optional[re.Pattern] = None  # for classifying an ID string
    restrict_to: Optional[List[str]] = None  # only match in these files/dirs


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
    # Review validation
    required_sections: Dict[str, List[str]]
    expected_bidir: Dict[str, List[str]]
    expected_cross_layer: Dict[str, List[Tuple[str, str]]]  # type -> [(target_type, label)]
    # Code traceability
    code: Optional[CodeConfig] = None
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
        required_sections=required_sections,
        expected_bidir=expected_bidir,
        expected_cross_layer=expected_cross_layer,
        code=code_config,
        lint=lint_config,
    )


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

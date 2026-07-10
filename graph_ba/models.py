"""Shared traceability data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Artifact:
    id: str
    artifact_type: str  # type ID from config (e.g. "ST", "BR_REQ")
    source_file: Path
    line_number: int
    title: str = ""
    origin: str = ""


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
    target_ids: list[str]  # normalized artifact IDs
    context: str = ""  # the raw comment line
    rel_path: str = ""  # relative path from project root
    provider_id: str = ""  # external symbol ID, when resolved by a provider
    provider_kind: str = ""
    provider_title: str = ""


@dataclass
class GraphNativeChangeTrace:
    """A Change scope edge declared by graph-native change.yaml."""

    source_id: str
    source_file: Path
    line_number: int
    target_id: str
    relation_type: str
    context: str = ""
    rel_path: str = ""


@dataclass
class GraphNativeArtifactTrace:
    """A typed edge declared directly on a graph-native artifact block."""

    source_id: str
    source_file: Path
    line_number: int
    target_id: str
    relation_type: str
    context: str = ""
    rel_path: str = ""


@dataclass
class TraceReport:
    registry_count: dict[str, int] = field(default_factory=dict)
    total_edges: int = 0
    orphans: list[str] = field(default_factory=list)
    dangling: list[tuple[str, str, int]] = field(default_factory=list)
    coverage: dict[str, dict] = field(default_factory=dict)
    missing_expected: list[tuple[str, str]] = field(default_factory=list)

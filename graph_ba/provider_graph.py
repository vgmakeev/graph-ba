"""Import the versioned graph format emitted by external providers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

from .config import ProjectConfig
from .graph_builder import add_trace_edge

PROVIDER_SCHEMA = "graph-ba.provider.v1"
TEST_LEVELS = {"unit", "headless", "ui", "other"}
TEST_STATUSES = ("passed", "failed", "skipped", "error")


class ProviderGraphError(ValueError):
    """A provider export is invalid or conflicts with another graph input."""


@dataclass
class ProviderGraph:
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[tuple[str, str, dict]] = field(default_factory=list)
    test_runs: dict[tuple[str, str, str], dict] = field(default_factory=dict)

    def add_to(self, graph: nx.MultiDiGraph) -> None:
        for node_id, node in self.nodes.items():
            if graph.nodes.get(node_id, {}).get("defined"):
                raise ProviderGraphError(
                    f"{node['provider']}: node {node_id!r} already has a local definition"
                )
            graph.add_node(node_id, **node)
        for source, target, edge in self.edges:
            for node_id in (source, target):
                if node_id not in graph:
                    graph.add_node(node_id, type="UNKNOWN", defined=False)
            add_trace_edge(graph, source, target, **edge)
        for run in self.test_runs.values():
            test_id = run["test_id"]
            test = graph.nodes.get(test_id, {})
            if not test.get("defined") or test.get("type") != "TEST":
                raise ProviderGraphError(
                    f"{run['provider']}: run {run['run_id']!r} references "
                    f"an undefined TEST node {test_id!r}"
                )


def provider_graph_files(root: Path, config: ProjectConfig) -> list[Path]:
    return sorted({
        path for pattern in config.provider_graph_files
        for path in root.glob(pattern) if path.is_file()
    })


def load_provider_graph(paths: list[Path], root: Path, config: ProjectConfig) -> ProviderGraph:
    graph = ProviderGraph()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("schema") != PROVIDER_SCHEMA:
                raise ValueError(f"expected schema {PROVIDER_SCHEMA!r}")
            provider = _text(document, "provider")
            for node in _records(document, "nodes"):
                node_id = _text(node, "id")
                entry = _node(node, provider, relative, config)
                previous = graph.nodes.setdefault(node_id, entry)
                if previous != entry:
                    raise ValueError(f"conflicting definitions of node {node_id!r}")
            for edge in _records(document, "edges"):
                source, target = _text(edge, "source_id"), _text(edge, "target_id")
                relation = _text(edge, "relation_type")
                if relation not in config.relation_types:
                    raise ValueError(f"unknown relation_type {relation!r}")
                graph.edges.append((source, target, {
                    "relation_type": relation,
                    "context": _text(edge, "context", default=""),
                    "source_file": _source_file(edge, relative),
                    "line": _line(edge),
                    "provider": provider,
                }))
            for run in _records(document, "test_runs"):
                entry = _test_run(run, provider)
                key = (provider, entry["test_id"], entry["run_id"])
                previous = graph.test_runs.setdefault(key, entry)
                if previous != entry:
                    raise ValueError(f"conflicting results for test {key[1]!r} in run {key[2]!r}")
        except (OSError, ValueError, OverflowError) as exc:
            raise ProviderGraphError(f"{relative}: {exc}") from exc
    return graph


def _records(document: dict, key: str) -> list[dict]:
    items = document.get(key, [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError(f"{key} must be an array of objects")
    return items


def _text(record: dict, key: str, *, default: str | None = None) -> str:
    value = record.get(key, default)
    if not isinstance(value, str) or (default is None and not value.strip()):
        raise ValueError(f"{key} must be {'a non-empty string' if default is None else 'a string'}")
    return value


def _line(record: dict) -> int:
    value = record.get("line_number", 0)
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise ValueError("line_number must be a non-negative SQLite integer")
    return value


def _source_file(record: dict, fallback: str) -> str:
    value = _text(record, "source_file", default=fallback)
    path = Path(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise ValueError("source_file must be a project-relative path")
    return path.as_posix()


def _node(record: dict, provider: str, relative: str, config: ProjectConfig) -> dict:
    node_type = _text(record, "type")
    origin = _text(record, "origin")
    if origin not in config.origins:
        raise ValueError(f"unknown origin {origin!r}")
    level = _text(record, "test_level", default="")
    if level and (node_type != "TEST" or level not in TEST_LEVELS):
        raise ValueError("test_level is only valid on TEST nodes and must be unit, headless, ui or other")
    return {
        "type": node_type,
        "origin": origin,
        "title": _text(record, "title", default=""),
        "source_file": _source_file(record, relative),
        "line_number": _line(record),
        "provider": provider,
        "test_level": level,
        "defined": True,
    }


def _test_run(record: dict, provider: str) -> dict:
    status = _text(record, "status")
    if status not in TEST_STATUSES:
        raise ValueError(f"invalid test status {status!r}")
    timestamp = datetime.fromisoformat(_text(record, "finished_at"))
    if timestamp.utcoffset() is None:
        raise ValueError("finished_at must include a timezone")
    return {
        "provider": provider,
        "test_id": _text(record, "test_id"),
        "run_id": _text(record, "run_id"),
        "commit": _text(record, "commit"),
        "status": status,
        "finished_at": timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "evidence": _text(record, "evidence", default=""),
    }

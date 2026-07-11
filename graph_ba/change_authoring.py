"""Safe graph-native artifact and link authoring for CLI/MCP agents."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Iterable

from graph_ba.config import classify_id, load_config
from graph_ba.scanning import (
    GRAPH_NATIVE_LINK_ATTRS,
    _parse_graph_native_attrs,
    scan_definition_occurrences,
)


class ChangeAuthoringError(RuntimeError):
    """Raised when a requested graph-native edit is ambiguous or unsafe."""


def add_artifact(
    root: Path,
    change_id: str,
    artifact_type: str,
    artifact_id: str,
    *,
    title: str,
    body: str = "",
    target_file: str | None = None,
    state: str = "active",
    links: Iterable[str] = (),
    migrate: bool = False,
) -> dict[str, object]:
    root = root.resolve()
    _require_change(root, change_id)
    config = load_config(root)
    if artifact_type not in config.types:
        raise ChangeAuthoringError(f"unknown artifact type: {artifact_type}")
    classified = classify_id(artifact_id, config)
    if classified != artifact_type:
        raise ChangeAuthoringError(
            f"artifact ID {artifact_id} classifies as {classified or 'unknown'}, not {artifact_type}"
        )
    relative = target_file or f".graphba/contract/{change_id.removeprefix('CHG-').lower()}.md"
    path = _safe_contract_path(root, relative)
    existing = scan_definition_occurrences(root, config).get(artifact_id, [])
    if existing and not migrate:
        locations = ", ".join(
            f"{item.source_file.relative_to(root)}:{item.line_number}" for item in existing
        )
        raise ChangeAuthoringError(
            f"artifact already has definition(s): {locations}; use --migrate to select a new owner"
        )
    if migrate and any(item.source_file.resolve() == path for item in existing):
        raise ChangeAuthoringError(
            f"artifact already exists in migration target: {path.relative_to(root)}"
        )

    parsed_links = _parse_links(links, config)
    attrs: dict[str, str] = {
        "type": artifact_type,
        "id": artifact_id,
        "state": state,
        "title": title,
        **parsed_links,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.is_file():
        prefix = path.read_text(encoding="utf-8")
        if migrate and "<!-- graph-ba: canonical-owner -->" not in prefix:
            prefix = "<!-- graph-ba: canonical-owner -->\n\n" + prefix
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
    elif migrate:
        prefix = "<!-- graph-ba: canonical-owner -->\n\n"
    block = _render_block(attrs, body)
    path.write_text(prefix + ("\n" if prefix and not prefix.endswith("\n\n") else "") + block, encoding="utf-8")
    return {
        "change": change_id,
        "artifact": artifact_id,
        "type": artifact_type,
        "file": str(path.relative_to(root)),
        "migrate": migrate,
        "links": parsed_links,
    }


def add_link(
    root: Path,
    change_id: str,
    source_id: str,
    relation: str,
    target_id: str,
) -> dict[str, str]:
    root = root.resolve()
    _require_change(root, change_id)
    config = load_config(root)
    if classify_id(target_id, config) is None:
        raise ChangeAuthoringError(f"target ID is not recognized by project config: {target_id}")
    attr_name = _relation_attr(relation)
    occurrences = scan_definition_occurrences(root, config).get(source_id, [])
    graph_native = [
        item for item in occurrences
        if item.source_file.suffix.lower() == ".md"
        and _line_is_graph_native(item.source_file, item.line_number)
    ]
    if not graph_native:
        if not occurrences:
            raise ChangeAuthoringError(f"source artifact not found: {source_id}")
        return _add_link_overlay(
            root,
            change_id,
            source_id,
            GRAPH_NATIVE_LINK_ATTRS[attr_name],
            target_id,
        )
    selected = _select_owner(root, graph_native)
    lines = selected.source_file.read_text(encoding="utf-8").splitlines()
    index = selected.line_number - 1
    marker = re.match(r"^\s*:::artifact\s+(.+?)\s*$", lines[index])
    if not marker:
        raise ChangeAuthoringError(f"artifact opening marker not found: {source_id}")
    attrs = _parse_graph_native_attrs(marker.group(1))
    targets = [item.strip() for item in attrs.get(attr_name, "").split(",") if item.strip()]
    if target_id not in targets:
        targets.append(target_id)
    attrs[attr_name] = ",".join(targets)
    lines[index] = _render_opening(attrs)
    selected.source_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "change": change_id,
        "source": source_id,
        "relation": GRAPH_NATIVE_LINK_ATTRS[attr_name],
        "target": target_id,
        "file": str(selected.source_file.relative_to(root)),
    }


def _add_link_overlay(
    root: Path,
    change_id: str,
    source_id: str,
    relation: str,
    target_id: str,
) -> dict[str, str]:
    """Declare a strong link without rewriting its brownfield source document."""
    digest = hashlib.sha256(
        f"{source_id}\0{relation}\0{target_id}".encode("utf-8")
    ).hexdigest()[:16].upper()
    link_id = f"LNK-{digest}"
    path = root / ".graphba" / "contract" / f"{change_id}-links.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    block = (
        f':::link id="{link_id}" source={json.dumps(source_id, ensure_ascii=False)} '
        f'relation="{relation}" target={json.dumps(target_id, ensure_ascii=False)} '
        f'change="{change_id}"\n:::\n'
    )
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if f'id="{link_id}"' not in existing:
        prefix = existing.rstrip()
        if not prefix:
            prefix = (
                "# Graph-native link assertions\n\n"
                "<!-- graph-ba: canonical-owner -->"
            )
        path.write_text(prefix + "\n\n" + block, encoding="utf-8")
    return {
        "change": change_id,
        "source": source_id,
        "relation": relation,
        "target": target_id,
        "assertion": link_id,
        "file": str(path.relative_to(root)),
        "overlay": "true",
    }


def _require_change(root: Path, change_id: str) -> None:
    single = root / ".graphba" / "changes" / f"{change_id}.yaml"
    legacy = root / ".graphba" / "changes" / change_id / "change.yaml"
    if not single.is_file() and not legacy.is_file():
        raise ChangeAuthoringError(f"change not found: {change_id}")


def _safe_contract_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix.lower() != ".md":
        raise ChangeAuthoringError("target file must be a root-relative Markdown path")
    path = (root / candidate).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ChangeAuthoringError("target file escapes project root") from exc
    return path


def _parse_links(links: Iterable[str], config) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for raw in links:
        if "=" not in raw:
            raise ChangeAuthoringError(f"link must use RELATION=TARGET syntax: {raw}")
        relation, target = (item.strip() for item in raw.split("=", 1))
        attr = _relation_attr(relation)
        if classify_id(target, config) is None:
            raise ChangeAuthoringError(f"link target is not recognized: {target}")
        grouped.setdefault(attr, []).append(target)
    return {key: ",".join(dict.fromkeys(values)) for key, values in grouped.items()}


def _relation_attr(relation: str) -> str:
    normalized = relation.strip()
    if normalized in GRAPH_NATIVE_LINK_ATTRS:
        return normalized
    upper = normalized.upper()
    for attr, relation_type in GRAPH_NATIVE_LINK_ATTRS.items():
        if relation_type == upper:
            return attr
    raise ChangeAuthoringError(f"unsupported graph-native relation: {relation}")


def _select_owner(root: Path, occurrences):
    marked = [
        item
        for item in occurrences
        if "<!-- graph-ba: canonical-owner -->"
        in item.source_file.read_text(encoding="utf-8")
    ]
    if len(marked) == 1:
        return marked[0]
    if len(occurrences) == 1:
        return occurrences[0]
    locations = ", ".join(str(item.source_file.relative_to(root)) for item in occurrences)
    raise ChangeAuthoringError(f"ambiguous source owner; add canonical-owner marker: {locations}")


def _line_is_graph_native(path: Path, line_number: int) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    return 0 < line_number <= len(lines) and lines[line_number - 1].lstrip().startswith(
        ":::artifact"
    )


def _render_block(attrs: dict[str, str], body: str) -> str:
    content = body.rstrip()
    return _render_opening(attrs) + "\n" + (content + "\n" if content else "") + ":::\n"


def _render_opening(attrs: dict[str, str]) -> str:
    preferred = ["type", "id", "state", "origin", "title"]
    keys = [key for key in preferred if key in attrs]
    keys.extend(sorted(key for key in attrs if key not in preferred))
    rendered = " ".join(
        f"{key}={json.dumps(str(attrs[key]), ensure_ascii=False)}" for key in keys
    )
    return f":::artifact {rendered}"

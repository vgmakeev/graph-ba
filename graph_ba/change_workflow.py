"""Git-native semantic change workflow.

Git owns versions and review.  This module only explains which graph artifacts
changed and prepares a bounded context for an agent or reviewer.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from graph_ba.config import ProjectConfig, load_config
from graph_ba.models import Artifact
from graph_ba.traceability import scan_definitions


class ChangeWorkflowError(RuntimeError):
    """Raised when a Git-native change cannot be compiled."""


CONTEXT_RELATIONS = {
    "CODE_TRACE",
    "CONTAINS",
    "DEPENDS_ON",
    "IMPLEMENTS",
    "NORMALIZES",
    "RENDERS",
    "TEST_EVIDENCE",
    "TRACES_TO",
    "UI_TRACE",
    "VERIFIES",
}


def find_change_manifest(root: Path, change_id: str) -> Path | None:
    """Resolve the Git-native manifest, falling back to the legacy layout."""
    root = root.resolve()
    single_file = root / ".graphba" / "changes" / f"{change_id}.yaml"
    if single_file.is_file():
        return single_file
    legacy = root / ".graphba" / "changes" / change_id / "change.yaml"
    return legacy if legacy.is_file() else None


def read_change_manifest(path: Path) -> dict[str, Any]:
    from graph_ba.traceability import _read_graph_native_change

    return _read_graph_native_change(path)


def semantic_diff(root: Path, *, base_ref: str | None = None) -> dict[str, Any]:
    """Compare stable-ID artifact sections between a Git base and the worktree."""
    root = root.resolve()
    config = load_config(root)
    git = _git_context(root, base_ref)
    changes = _changed_paths(root, git["base_commit"])

    with tempfile.TemporaryDirectory(prefix="graph-ba-base-") as base_tmp, tempfile.TemporaryDirectory(
        prefix="graph-ba-head-"
    ) as head_tmp:
        base_root = Path(base_tmp)
        head_root = Path(head_tmp)
        for change in changes:
            old_path = change.get("old_path")
            new_path = change.get("path")
            if old_path:
                content = _git_text(root, git["base_commit"], old_path)
                if content is not None:
                    _write_snapshot_file(base_root, old_path, content)
            if new_path:
                source = root / new_path
                if source.is_file():
                    try:
                        _write_snapshot_file(head_root, new_path, source.read_text(encoding="utf-8"))
                    except UnicodeDecodeError:
                        pass

        base_artifacts = _artifact_snapshot(base_root, config)
        head_artifacts = _artifact_snapshot(head_root, config)

    artifact_changes = _compare_artifacts(base_artifacts, head_artifacts)
    contract_changes = [item for item in artifact_changes if item["origin"] == "canonical"]
    fingerprint_input = [
        {
            "operation": item["operation"],
            "id": item["id"],
            "type": item["type"],
            "before": item.get("before", {}).get("fingerprint", ""),
            "after": item.get("after", {}).get("fingerprint", ""),
        }
        for item in contract_changes
    ]
    proposal_fingerprint = _sha256_json(
        {"base_commit": git["base_commit"], "contract": fingerprint_input}
    )
    return {
        "schema": "graph-ba.semantic-change.v1",
        "git": git,
        "proposal_fingerprint": proposal_fingerprint,
        "summary": {
            "files": len(changes),
            "artifacts": len(artifact_changes),
            "contract": len(contract_changes),
            "added": sum(item["operation"] == "add" for item in artifact_changes),
            "modified": sum(item["operation"] == "modify" for item in artifact_changes),
            "removed": sum(item["operation"] == "remove" for item in artifact_changes),
        },
        "files": changes,
        "artifacts": artifact_changes,
        "contract": contract_changes,
    }


def proposal_check(change: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate that a proposed change is reviewable without judging delivery."""
    findings: list[dict[str, str]] = []
    if not str(manifest.get("intent") or "").strip():
        findings.append({
            "code": "missing_intent",
            "message": "change manifest must state intent",
        })
    if not change["contract"] and not manifest.get("scope"):
        findings.append({
            "code": "empty_contract_delta",
            "message": "no canonical artifact delta or explicit scope hint was found",
        })
    verdict = "PASS" if not findings else "FAIL"
    return {
        "schema": "graph-ba.change-check.v1",
        "stage": "proposal",
        "verdict": verdict,
        "pass": not findings,
        "proposal_fingerprint": change["proposal_fingerprint"],
        "base_commit": change["git"]["base_commit"],
        "summary": {
            "contract": len(change["contract"]),
            "findings": len(findings),
        },
        "findings": findings,
    }


def change_context(
    db,
    change: dict[str, Any],
    *,
    scope_hints: Iterable[str] = (),
    limit: int = 200,
) -> dict[str, Any]:
    """Return one-hop typed impact around changed canonical artifacts."""
    seeds = sorted({item["id"] for item in change["contract"]} | set(scope_hints))
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    unresolved: list[str] = []
    placeholders = ",".join("?" for _ in CONTEXT_RELATIONS)

    for seed in seeds:
        row = db.execute("SELECT * FROM artifacts WHERE id = ?", (seed,)).fetchone()
        if not row:
            unresolved.append(seed)
            continue
        nodes[seed] = _context_node(row, "changed")
        rows = db.execute(
            "SELECT e.source_id, e.target_id, e.relation_type, "
            "s.type AS source_type, s.origin AS source_origin, s.title AS source_title, "
            "t.type AS target_type, t.origin AS target_origin, t.title AS target_title "
            "FROM edges e "
            "LEFT JOIN artifacts s ON s.id = e.source_id "
            "LEFT JOIN artifacts t ON t.id = e.target_id "
            f"WHERE (e.source_id = ? OR e.target_id = ?) AND e.relation_type IN ({placeholders}) "
            "ORDER BY e.relation_type, e.source_id, e.target_id LIMIT ?",
            (seed, seed, *sorted(CONTEXT_RELATIONS), limit),
        ).fetchall()
        for edge in rows:
            edges.append({
                "source": edge["source_id"],
                "relation": edge["relation_type"],
                "target": edge["target_id"],
            })
            for side in ("source", "target"):
                node_id = edge[f"{side}_id"]
                if node_id not in nodes:
                    nodes[node_id] = {
                        "id": node_id,
                        "type": edge[f"{side}_type"] or "?",
                        "origin": edge[f"{side}_origin"] or "",
                        "title": edge[f"{side}_title"] or "",
                        "reason": "direct_typed_neighbor",
                    }

    ordered_nodes = sorted(nodes.values(), key=lambda item: (item["origin"], item["type"], item["id"]))
    return {
        "schema": "graph-ba.change-context.v1",
        "proposal_fingerprint": change["proposal_fingerprint"],
        "seeds": seeds,
        "summary": {
            "nodes": len(ordered_nodes),
            "edges": len(edges),
            "implementation": sum(item["origin"] == "implementation" for item in ordered_nodes),
            "evidence": sum(item["origin"] == "evidence" for item in ordered_nodes),
            "unresolved": len(unresolved),
        },
        "nodes": ordered_nodes,
        "edges": edges,
        "unresolved": unresolved,
    }


def _git_context(root: Path, base_ref: str | None) -> dict[str, Any]:
    _git(root, "rev-parse", "--is-inside-work-tree")
    branch = _git(root, "branch", "--show-current", allow_empty=True) or "DETACHED"
    selected_ref = base_ref or _default_base_ref(root)
    try:
        base_commit = _git(root, "merge-base", "HEAD", selected_ref)
    except ChangeWorkflowError:
        base_commit = _git(root, "rev-parse", selected_ref)
    return {
        "branch": branch,
        "base_ref": selected_ref,
        "base_commit": base_commit,
        "head_commit": _git(root, "rev-parse", "HEAD"),
        "dirty": bool(_git(root, "status", "--porcelain", allow_empty=True)),
    }


def _default_base_ref(root: Path) -> str:
    symbolic = _git(
        root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        check=False,
        allow_empty=True,
    )
    candidates = [symbolic, "origin/main", "main", "HEAD"]
    for candidate in candidates:
        if candidate and _git(root, "rev-parse", "--verify", candidate, check=False, allow_empty=True):
            return candidate
    raise ChangeWorkflowError("cannot determine a Git base ref")


def _changed_paths(root: Path, base_commit: str) -> list[dict[str, str]]:
    raw = _git_bytes(root, "diff", "--name-status", "-z", "--find-renames", base_commit, "--")
    parts = raw.decode("utf-8", errors="surrogateescape").split("\0")
    result: list[dict[str, str]] = []
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index]
        index += 1
        if status.startswith(("R", "C")):
            old_path, new_path = parts[index], parts[index + 1]
            index += 2
            result.append({"status": status[0], "old_path": old_path, "path": new_path})
        else:
            path = parts[index]
            index += 1
            result.append({
                "status": status[0],
                "old_path": path if status[0] != "A" else "",
                "path": "" if status[0] == "D" else path,
            })

    known = {item["path"] for item in result if item.get("path")}
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z", allow_empty=True)
    for path in untracked.split("\0"):
        if path and path not in known:
            result.append({"status": "A", "old_path": "", "path": path})
    return sorted(result, key=lambda item: (item.get("path") or item.get("old_path") or ""))


def _artifact_snapshot(root: Path, config: ProjectConfig) -> dict[str, dict[str, Any]]:
    registry = scan_definitions(root, config)
    grouped: dict[Path, list[Artifact]] = defaultdict(list)
    for artifact in registry.values():
        grouped[artifact.source_file].append(artifact)

    snapshot: dict[str, dict[str, Any]] = {}
    for path, artifacts in grouped.items():
        lines = path.read_text(encoding="utf-8").splitlines()
        ordered = sorted(artifacts, key=lambda item: item.line_number)
        for index, artifact in enumerate(ordered):
            end = ordered[index + 1].line_number - 1 if index + 1 < len(ordered) else len(lines)
            section = "\n".join(line.rstrip() for line in lines[artifact.line_number - 1:end]).strip()
            type_def = config.types.get(artifact.artifact_type)
            snapshot[artifact.id] = {
                "id": artifact.id,
                "type": artifact.artifact_type,
                "origin": type_def.origin if type_def else "",
                "title": artifact.title,
                "source_file": str(path.relative_to(root)),
                "line_number": artifact.line_number,
                "fingerprint": hashlib.sha256(section.encode("utf-8")).hexdigest(),
            }
    return snapshot


def _compare_artifacts(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for artifact_id in sorted(set(before) | set(after)):
        old = before.get(artifact_id)
        new = after.get(artifact_id)
        if old is None:
            operation = "add"
        elif new is None:
            operation = "remove"
        elif all(old.get(key) == new.get(key) for key in ("type", "title", "source_file", "fingerprint")):
            continue
        else:
            operation = "modify"
        current = new or old or {}
        item: dict[str, Any] = {
            "operation": operation,
            "id": artifact_id,
            "type": current.get("type", "?"),
            "origin": current.get("origin", ""),
        }
        if old:
            item["before"] = old
        if new:
            item["after"] = new
        result.append(item)
    return result


def _context_node(row, reason: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "origin": row["origin"],
        "title": row["title"],
        "reason": reason,
    }


def _write_snapshot_file(root: Path, relative: str, content: str) -> None:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        return
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _git_text(root: Path, commit: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=root,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _git(
    root: Path,
    *args: str,
    check: bool = True,
    allow_empty: bool = False,
) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    value = result.stdout.strip()
    if check and result.returncode != 0:
        message = result.stderr.strip() or "git command failed"
        raise ChangeWorkflowError(message)
    if result.returncode != 0:
        return ""
    if not value and not allow_empty and check:
        raise ChangeWorkflowError(f"git {' '.join(args)} returned no value")
    return value


def _git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if result.returncode != 0:
        raise ChangeWorkflowError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

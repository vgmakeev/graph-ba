"""Git-native semantic change workflow.

Git owns versions and review.  This module only explains which graph artifacts
changed and prepares a bounded context for an agent or reviewer.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from graph_ba.config import ProjectConfig, load_config
from graph_ba.db import _fts_query
from graph_ba.graph_snapshots import (
    GraphSnapshotError,
    graph_delta,
    graph_views,
    impact_paths,
)
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


class ChangeWorkflowService:
    """Shared Git-native workflow used by CLI and MCP adapters."""

    def __init__(self, root: Path, db=None):
        self.root = root.resolve()
        self.db = db

    def manifest_path(self, change_id: str) -> Path:
        path = find_change_manifest(self.root, change_id)
        if not path:
            raise ChangeWorkflowError(f"Change not found: {change_id}")
        return path

    def manifest(self, change_id: str) -> dict[str, Any]:
        return read_change_manifest(self.manifest_path(change_id))

    def diff(self, change_id: str) -> dict[str, Any]:
        manifest = self.manifest(change_id)
        return semantic_diff(
            self.root,
            base_ref=str(manifest.get("base_ref") or "") or None,
        )

    def discover(
        self,
        query: str,
        *,
        limit: int = 20,
        seed_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        if self.db is None:
            raise ChangeWorkflowError("discover requires an imported graph")
        return discover_candidates(
            self.db,
            query,
            root=self.root,
            limit=limit,
            seed_ids=seed_ids,
        )

    def compile(self, change_id: str) -> dict[str, Any]:
        if self.db is None:
            raise ChangeWorkflowError("compile requires an imported graph")
        manifest = self.manifest(change_id)
        semantic = self.diff(change_id)
        try:
            with graph_views(self.root, self.db, semantic["git"]["base_commit"]) as views:
                delta = graph_delta(views, semantic)
                impact = impact_paths(
                    views,
                    delta,
                    scope_hints=tuple(manifest.get("scope", [])),
                )
        except GraphSnapshotError as exc:
            raise ChangeWorkflowError(str(exc)) from exc
        return {
            "schema": "graph-ba.compiled-change.v1",
            "change": change_id,
            "semantic": semantic,
            "graph_delta": delta,
            "impact": impact,
        }

    def context(self, change_id: str) -> dict[str, Any]:
        return self.compile(change_id)["impact"]

    def proposal_check(self, change_id: str) -> dict[str, Any]:
        return proposal_check(self.diff(change_id), self.manifest(change_id))

    def approval(self, change_id: str) -> dict[str, Any]:
        return approval_status(self.root, change_id, self.diff(change_id))

    def approve(self, change_id: str, reviewer: str) -> dict[str, Any]:
        if not reviewer.strip():
            raise ChangeWorkflowError("reviewer is required")
        change = self.diff(change_id)
        record = {
            "schema": "graph-ba.approval.v1",
            "change": change_id,
            "reviewer": reviewer.strip(),
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "base_commit": change["git"]["base_commit"],
            "proposal_fingerprint": change["proposal_fingerprint"],
        }
        path = approval_path(self.root, change_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {**record, "path": str(path)}

    def status(self, change_id: str) -> dict[str, Any]:
        manifest_path = self.manifest_path(change_id)
        change = self.diff(change_id)
        approval = approval_status(self.root, change_id, change)
        tracked = bool(
            _git(
                self.root,
                "ls-files",
                "--error-unmatch",
                str(manifest_path.relative_to(self.root)),
                check=False,
                allow_empty=True,
            )
        )
        lifecycle = "draft"
        if tracked and change["git"]["branch"] not in {"main", "master", "dev"}:
            lifecycle = "proposed"
        if approval["valid"]:
            lifecycle = "approved"
        if tracked and approval["valid"] and change["git"]["branch"] in {"main", "master", "dev"}:
            lifecycle = "accepted"
        return {
            "schema": "graph-ba.change-status.v1",
            "change": change_id,
            "lifecycle": lifecycle,
            "git": change["git"],
            "manifest": str(manifest_path.relative_to(self.root)),
            "proposal_fingerprint": change["proposal_fingerprint"],
            "contract_changes": len(change["contract"]),
            "approval": approval,
        }


def find_change_manifest(root: Path, change_id: str) -> Path | None:
    """Resolve the Git-native manifest, falling back to the legacy layout."""
    root = root.resolve()
    single_file = root / ".graphba" / "changes" / f"{change_id}.yaml"
    if single_file.is_file():
        return single_file
    legacy = root / ".graphba" / "changes" / change_id / "change.yaml"
    return legacy if legacy.is_file() else None


def create_change_branch(
    root: Path,
    change_id: str,
    *,
    base_ref: str | None = None,
) -> dict[str, str]:
    """Create a clean Git branch for a change and return its binding."""
    root = root.resolve()
    if _git(root, "status", "--porcelain", allow_empty=True):
        raise ChangeWorkflowError(
            "working tree must be clean before change init; use --no-branch to keep the current branch"
        )
    current = _git(root, "branch", "--show-current", allow_empty=True)
    selected_base = base_ref or current or _default_base_ref(root)
    branch = f"change/{change_id.lower()}"
    if current == branch:
        return {"branch": branch, "base_ref": selected_base}
    if _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False, allow_empty=True):
        raise ChangeWorkflowError(f"branch already exists: {branch}")
    _git(root, "switch", "-c", branch, allow_empty=True)
    return {"branch": branch, "base_ref": selected_base}


def init_change(
    root: Path,
    change_id: str,
    *,
    title: str = "",
    intent: str = "",
    sources: Iterable[str] = (),
    scope: Iterable[str] = (),
    base_ref: str | None = None,
    create_branch: bool = False,
) -> Path:
    """Create the single Git-native manifest used by both CLI and MCP."""
    root = root.resolve()
    if not re.fullmatch(r"CHG-[A-Za-z0-9][A-Za-z0-9-]*", change_id):
        raise ChangeWorkflowError("Change ID must match CHG-<name>")
    path = root / ".graphba" / "changes" / f"{change_id}.yaml"
    legacy = root / ".graphba" / "changes" / change_id
    if path.exists() or legacy.exists():
        raise ChangeWorkflowError(f"Change already exists: {change_id}")
    if create_branch:
        binding = create_change_branch(root, change_id, base_ref=base_ref)
        base_ref = binding["base_ref"]
    lines = [
        f"id: {change_id}",
        f"title: {json.dumps(title or change_id, ensure_ascii=False)}",
        f"intent: {json.dumps(intent, ensure_ascii=False)}",
    ]
    if base_ref:
        lines.append(f"base_ref: {json.dumps(base_ref, ensure_ascii=False)}")
    lines.append("sources:")
    lines.extend(f"  - {item}" for item in sources)
    lines.append("scope:")
    lines.extend(f"  - {item}" for item in scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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
    file_groups = _classify_changed_files(changes, artifact_changes)
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
            "contract_files": len(file_groups["contract_files"]),
            "supporting_files": len(file_groups["supporting_files"]),
            "delivery_files": len(file_groups["delivery_files"]),
            "artifacts": len(artifact_changes),
            "contract": len(contract_changes),
            "added": sum(item["operation"] == "add" for item in artifact_changes),
            "modified": sum(item["operation"] == "modify" for item in artifact_changes),
            "removed": sum(item["operation"] == "remove" for item in artifact_changes),
        },
        "files": changes,
        **file_groups,
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


def discover_candidates(
    db,
    query: str,
    *,
    root: Path | None = None,
    limit: int = 20,
    seed_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Find likely contract/source artifacts for a change intent."""
    ordered_seeds = list(dict.fromkeys(item for item in seed_ids if item))
    sql = (
        "SELECT a.id, a.type, a.origin, a.title, a.source_file, a.line_number, "
        "fp.full_path AS full_source_file "
        "FROM artifacts_fts f JOIN artifacts a ON f.rowid = a.rowid "
        "LEFT JOIN file_paths fp ON fp.filename = a.source_file "
        "WHERE artifacts_fts MATCH ? AND a.defined = 1 "
        "ORDER BY CASE a.origin "
        "WHEN 'canonical' THEN 0 WHEN 'human' THEN 1 WHEN 'derived' THEN 2 ELSE 3 END, "
        "rank LIMIT ?"
    )
    rows = []
    if ordered_seeds:
        placeholders = ",".join("?" for _ in ordered_seeds)
        exact = db.execute(
            "SELECT a.id, a.type, a.origin, a.title, a.source_file, a.line_number, "
            "fp.full_path AS full_source_file FROM artifacts a "
            "LEFT JOIN file_paths fp ON fp.filename = a.source_file "
            f"WHERE a.id IN ({placeholders}) AND a.defined = 1",
            tuple(ordered_seeds),
        ).fetchall()
        by_id = {row["id"]: row for row in exact}
        rows.extend(by_id[item] for item in ordered_seeds if item in by_id)

    strategy = "seeded" if rows else "all_terms"
    remaining = max(0, limit - len(rows))
    search_rows = []
    if query.strip() and remaining:
        fq = _fts_query(query)
        search_rows = db.execute(sql, (fq, remaining)).fetchall()
        if search_rows:
            strategy = "seeded+all_terms" if rows else "all_terms"
    if not search_rows and not rows and query.strip():
        tokens = list(dict.fromkeys(re.findall(r"[\w-]{3,}", query, re.UNICODE)))
        fallback = " OR ".join(f'"{token.replace(chr(34), "")}"*' for token in tokens)
        if fallback:
            search_rows = db.execute(sql, (fallback, remaining or limit)).fetchall()
            strategy = "any_term"
    seen_ids = {row["id"] for row in rows}
    rows.extend(row for row in search_rows if row["id"] not in seen_ids)
    candidates = []
    for row in rows:
        neighbors = db.execute(
            "SELECT source_id, target_id, relation_type FROM edges "
            "WHERE (source_id = ? OR target_id = ?) AND relation_type != 'MENTIONS' "
            "ORDER BY relation_type LIMIT 8",
            (row["id"], row["id"]),
        ).fetchall()
        item = dict(row)
        full_source = item.pop("full_source_file", "") or ""
        if root and full_source:
            try:
                item["source_file"] = str(Path(full_source).resolve().relative_to(root.resolve()))
            except ValueError:
                item["source_file"] = full_source
        candidates.append({
            **item,
            "typed_neighbors": [dict(item) for item in neighbors],
        })
    return {
        "schema": "graph-ba.change-discovery.v1",
        "query": query,
        "strategy": strategy,
        "seed_ids": ordered_seeds,
        "candidates": candidates,
    }


def _classify_changed_files(
    changes: list[dict[str, str]],
    artifact_changes: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """Separate semantic contract files from supporting and delivery edits."""
    contract_paths: set[str] = set()
    supporting_paths: set[str] = set()
    for item in artifact_changes:
        destinations = contract_paths if item.get("origin") == "canonical" else supporting_paths
        for side in ("before", "after"):
            source_file = str(item.get(side, {}).get("source_file") or "")
            if source_file:
                destinations.add(source_file)
    supporting_paths -= contract_paths

    for change in changes:
        for path in (change.get("old_path"), change.get("path")):
            if not path:
                continue
            if path.startswith(".graphba/contract/"):
                contract_paths.add(path)
            elif path.startswith(".graphba/"):
                supporting_paths.add(path)
    supporting_paths -= contract_paths

    def paths(change: dict[str, str]) -> set[str]:
        return {path for path in (change.get("old_path"), change.get("path")) if path}

    contract_files = [item for item in changes if paths(item) & contract_paths]
    supporting_files = [item for item in changes if paths(item) & supporting_paths]
    classified = {id(item) for item in contract_files + supporting_files}
    delivery_files = [item for item in changes if id(item) not in classified]
    return {
        "contract_files": contract_files,
        "supporting_files": supporting_files,
        "delivery_files": delivery_files,
    }


def approval_path(root: Path, change_id: str) -> Path:
    return root / ".graphba" / "approvals" / f"{change_id}.json"


def approval_status(
    root: Path,
    change_id: str,
    change: dict[str, Any],
) -> dict[str, Any]:
    path = approval_path(root, change_id)
    if not path.is_file():
        return {
            "present": False,
            "valid": False,
            "path": str(path),
            "reason": "missing_approval",
        }
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "present": True,
            "valid": False,
            "path": str(path),
            "reason": f"invalid_approval: {exc}",
        }
    expected = change["proposal_fingerprint"]
    valid = (
        record.get("change") == change_id
        and record.get("base_commit") == change["git"]["base_commit"]
        and record.get("proposal_fingerprint") == expected
        and bool(record.get("reviewer"))
    )
    return {
        "present": True,
        "valid": valid,
        "path": str(path),
        "reason": "matched" if valid else "fingerprint_or_base_mismatch",
        "record": record,
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
                "origin": artifact.origin or (type_def.origin if type_def else ""),
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

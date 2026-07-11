"""Git-native semantic change workflow.

Git owns versions and review.  This module only explains which graph artifacts
changed and prepares a bounded context for an agent or reviewer.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
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
from graph_ba.scanning import canonical_ownership_findings
from graph_ba.traceability import scan_definitions


class ChangeWorkflowError(RuntimeError):
    """Raised when a Git-native change cannot be compiled."""


CONTEXT_RELATIONS = {
    "CONTAINS",
    "DEPENDS_ON",
    "IMPLEMENTS",
    "NORMALIZES",
    "RENDERS",
    "TRACES_TO",
    "VERIFIES",
}

PROPOSAL_POLICY_FILES = (
    "graph-ba.toml",
    ".graphba/project.yaml",
    ".graphba/artifact-class-matrix.json",
    ".graphba/evidence-policy.json",
)


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
        change = self.diff(change_id)
        manifest = self.manifest(change_id)
        result = proposal_check(change, manifest)
        ownership = canonical_ownership_findings(
            self.root,
            load_config(self.root),
            {item["id"] for item in change.get("contract", [])},
        )
        for finding in ownership:
            if finding["severity"] != "ERR":
                continue
            result["findings"].append({
                "code": "duplicate_canonical_owner",
                "message": finding["message"],
                "artifact": finding["artifact_id"],
            })
        rebase = semantic_rebase_status(self.root, manifest, change)
        result["rebase"] = rebase
        if rebase["status"] == "conflict":
            result["findings"].append({
                "code": "semantic_rebase_conflict",
                "message": "target ref changed overlapping contract IDs or proposal policy",
                "conflicts": rebase["conflicts"],
            })
            result["pass"] = False
            result["verdict"] = "FAIL"
            result["summary"]["findings"] = len(result["findings"])
        if result["findings"] and any(
            finding.get("code") == "duplicate_canonical_owner"
            for finding in result["findings"]
        ):
            result["pass"] = False
            result["verdict"] = "FAIL"
            result["summary"]["findings"] = len(result["findings"])
        return result

    def approval(self, change_id: str) -> dict[str, Any]:
        return approval_status(self.root, change_id, self.diff(change_id))

    def approve(
        self,
        change_id: str,
        reviewer: str,
        evidence: str,
    ) -> dict[str, Any]:
        if not reviewer.strip():
            raise ChangeWorkflowError("reviewer is required")
        if not evidence.strip():
            raise ChangeWorkflowError("review evidence is required (for example a protected PR URL)")
        change = self.diff(change_id)
        proposal_paths = [
            item.get("path") or item.get("old_path")
            for group in ("contract_files", "supporting_files")
            for item in change.get(group, [])
        ]
        dirty_paths = [
            path
            for path in proposal_paths
            if path
            and _git(
                self.root,
                "status",
                "--porcelain",
                "--",
                path,
                allow_empty=True,
            )
        ]
        if dirty_paths:
            raise ChangeWorkflowError(
                "commit proposal contract/supporting files before approval: "
                + ", ".join(sorted(dirty_paths))
            )
        review_commit = _git(self.root, "rev-parse", "HEAD")
        record = {
            "schema": "graph-ba.approval.v1",
            "change": change_id,
            "reviewer": reviewer.strip(),
            "review_evidence": evidence.strip(),
            "review_commit": review_commit,
            "trust": "asserted_external_review",
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
        rebase = semantic_rebase_status(self.root, self.manifest(change_id), change)
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
            "rebase": rebase,
        }

    def rebase_status(self, change_id: str) -> dict[str, Any]:
        change = self.diff(change_id)
        return semantic_rebase_status(self.root, self.manifest(change_id), change)


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
    target_ref: str | None = None,
) -> dict[str, str]:
    """Create a clean Git branch for a change and return its binding."""
    root = root.resolve()
    if _git(root, "status", "--porcelain", allow_empty=True):
        raise ChangeWorkflowError(
            "working tree must be clean before change init; use --no-branch to keep the current branch"
        )
    current = _git(root, "branch", "--show-current", allow_empty=True)
    selected_target = target_ref or base_ref or current or _default_base_ref(root)
    selected_base = base_ref or selected_target
    base_commit = _git(root, "rev-parse", selected_base)
    branch = f"change/{change_id.lower()}"
    if current == branch:
        return {"branch": branch, "base_ref": base_commit, "target_ref": selected_target}
    if _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False, allow_empty=True):
        raise ChangeWorkflowError(f"branch already exists: {branch}")
    _git(root, "switch", "-c", branch, base_commit, allow_empty=True)
    return {"branch": branch, "base_ref": base_commit, "target_ref": selected_target}


def create_change_worktree(
    root: Path,
    change_id: str,
    worktree_path: Path,
    *,
    base_ref: str | None = None,
    target_ref: str | None = None,
) -> dict[str, str]:
    """Create an isolated change branch/worktree without touching current edits."""
    root = root.resolve()
    selected_target = target_ref or base_ref or _git(
        root, "branch", "--show-current", allow_empty=True
    ) or _default_base_ref(root)
    selected_base = base_ref or selected_target
    base_commit = _git(root, "rev-parse", selected_base)
    branch = f"change/{change_id.lower()}"
    destination = worktree_path.expanduser().resolve()
    if destination.exists():
        raise ChangeWorkflowError(f"worktree path already exists: {destination}")
    if _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False, allow_empty=True):
        raise ChangeWorkflowError(f"branch already exists: {branch}")
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(destination), base_commit],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ChangeWorkflowError(result.stderr.strip() or "git worktree add failed")
    bootstrapped, bootstrap_warnings = _bootstrap_local_graph_inputs(root, destination)
    return {
        "root": str(destination),
        "branch": branch,
        "base_ref": base_commit,
        "target_ref": selected_target,
        "bootstrapped": bootstrapped,
        "bootstrap_warnings": bootstrap_warnings,
    }


def _bootstrap_local_graph_inputs(
    source_root: Path,
    destination_root: Path,
) -> tuple[list[str], list[str]]:
    """Copy ignored provider inputs required to reproduce the source graph."""
    try:
        config = load_config(source_root)
    except Exception as exc:
        return [], [f"cannot load graph-ba config for worktree bootstrap: {exc}"]

    candidates: list[Path] = []
    if config.codegraph:
        candidates.append(Path(config.codegraph.database))
    candidates.extend(Path(item) for item in config.graph_native.dirs)

    copied: list[str] = []
    warnings: list[str] = []
    for relative in candidates:
        if relative.is_absolute() or ".." in relative.parts:
            continue
        source = source_root / relative
        destination = destination_root / relative
        if not source.exists() or destination.exists():
            continue
        if not _git_path_is_ignored(source_root, relative):
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
            copied.append(relative.as_posix())
        except OSError as exc:
            warnings.append(f"cannot bootstrap {relative.as_posix()}: {exc}")
    return copied, warnings


def _git_path_is_ignored(root: Path, relative: Path) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", relative.as_posix()],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def init_change(
    root: Path,
    change_id: str,
    *,
    title: str = "",
    intent: str = "",
    sources: Iterable[str] = (),
    scope: Iterable[str] = (),
    base_ref: str | None = None,
    target_ref: str | None = None,
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
        binding = create_change_branch(
            root,
            change_id,
            base_ref=base_ref,
            target_ref=target_ref,
        )
        base_ref = binding["base_ref"]
        target_ref = binding["target_ref"]
    elif base_ref:
        base_ref = _git(root, "rev-parse", base_ref)
    else:
        # In no-branch mode the current commit is the immutable boundary.
        # Falling back later to origin/main can turn an ordinary dirty feature
        # checkout into a repository-wide semantic delta.
        base_ref = _git(
            root,
            "rev-parse",
            "HEAD",
            check=False,
            allow_empty=True,
        ) or None
    lines = [
        f"id: {change_id}",
        f"title: {json.dumps(title or change_id, ensure_ascii=False)}",
        f"intent: {json.dumps(intent, ensure_ascii=False)}",
    ]
    if base_ref:
        lines.append(f"base_ref: {json.dumps(base_ref, ensure_ascii=False)}")
    if target_ref:
        lines.append(f"target_ref: {json.dumps(target_ref, ensure_ascii=False)}")
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
    policy = _proposal_policy_state(root, git["base_commit"])
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
        {
            "base_commit": git["base_commit"],
            "contract": fingerprint_input,
            "policy": policy["after"],
        }
    )
    return {
        "schema": "graph-ba.semantic-change.v1",
        "git": git,
        "proposal_fingerprint": proposal_fingerprint,
        "policy": policy,
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
            "policy_changed": len(policy["changed"]),
        },
        "files": changes,
        **file_groups,
        "artifacts": artifact_changes,
        "contract": contract_changes,
    }


def _proposal_policy_state(root: Path, base_commit: str) -> dict[str, Any]:
    before = _policy_hashes_at_ref(root, base_commit)
    after: dict[str, str] = {}
    for relative in PROPOSAL_POLICY_FILES:
        path = root / relative
        if path.is_file():
            after[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    changed = sorted(
        relative
        for relative in set(before) | set(after)
        if before.get(relative) != after.get(relative)
    )
    return {"before": before, "after": after, "changed": changed}


def _policy_hashes_at_ref(root: Path, ref: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in PROPOSAL_POLICY_FILES:
        content = _git_text(root, ref, relative)
        if content is not None:
            result[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return result


def semantic_rebase_status(
    root: Path,
    manifest: dict[str, Any],
    change: dict[str, Any],
) -> dict[str, Any]:
    """Detect target-branch movement that overlaps the proposed semantic delta."""
    target_ref = str(manifest.get("target_ref") or "").strip()
    base_commit = change["git"]["base_commit"]
    if not target_ref:
        return {
            "status": "not_configured",
            "target_ref": "",
            "base_commit": base_commit,
            "target_commit": "",
            "behind_commits": 0,
            "conflicts": [],
        }
    target_commit = _git(
        root, "rev-parse", "--verify", target_ref, check=False, allow_empty=True
    )
    if not target_commit:
        return {
            "status": "unavailable",
            "target_ref": target_ref,
            "base_commit": base_commit,
            "target_commit": "",
            "behind_commits": 0,
            "conflicts": [{"kind": "missing_target_ref", "value": target_ref}],
        }
    if target_commit == base_commit:
        return {
            "status": "current",
            "target_ref": target_ref,
            "base_commit": base_commit,
            "target_commit": target_commit,
            "behind_commits": 0,
            "conflicts": [],
        }

    upstream = _semantic_diff_between_refs(root, base_commit, target_commit)
    proposed_ids = {item["id"] for item in change.get("contract", [])}
    upstream_ids = {item["id"] for item in upstream["contract"]}
    conflicts = [
        {"kind": "artifact", "id": artifact_id}
        for artifact_id in sorted(proposed_ids & upstream_ids)
    ]
    if upstream["policy"]["changed"]:
        conflicts.append({
            "kind": "proposal_policy",
            "files": upstream["policy"]["changed"],
        })
    behind = int(
        _git(
            root,
            "rev-list",
            "--count",
            f"{base_commit}..{target_commit}",
            allow_empty=True,
        )
        or 0
    )
    return {
        "status": "conflict" if conflicts else "behind",
        "target_ref": target_ref,
        "base_commit": base_commit,
        "target_commit": target_commit,
        "behind_commits": behind,
        "upstream_contract_changes": len(upstream["contract"]),
        "conflicts": conflicts,
    }


def _semantic_diff_between_refs(
    root: Path,
    before_ref: str,
    after_ref: str,
) -> dict[str, Any]:
    config = load_config(root)
    changes = _changed_paths_between(root, before_ref, after_ref)
    with tempfile.TemporaryDirectory(prefix="graph-ba-before-") as before_tmp, tempfile.TemporaryDirectory(
        prefix="graph-ba-after-"
    ) as after_tmp:
        before_root = Path(before_tmp)
        after_root = Path(after_tmp)
        for change in changes:
            old_path = change.get("old_path")
            new_path = change.get("path")
            if old_path:
                content = _git_text(root, before_ref, old_path)
                if content is not None:
                    _write_snapshot_file(before_root, old_path, content)
            if new_path:
                content = _git_text(root, after_ref, new_path)
                if content is not None:
                    _write_snapshot_file(after_root, new_path, content)
        before = _artifact_snapshot(before_root, config)
        after = _artifact_snapshot(after_root, config)
    artifacts = _compare_artifacts(before, after)
    contract = [item for item in artifacts if item["origin"] == "canonical"]
    before_policy = _policy_hashes_at_ref(root, before_ref)
    after_policy = _policy_hashes_at_ref(root, after_ref)
    policy_changed = sorted(
        relative
        for relative in set(before_policy) | set(after_policy)
        if before_policy.get(relative) != after_policy.get(relative)
    )
    return {
        "artifacts": artifacts,
        "contract": contract,
        "policy": {
            "before": before_policy,
            "after": after_policy,
            "changed": policy_changed,
        },
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
    review_commit = str(record.get("review_commit") or "")
    commit_is_ancestor = bool(
        review_commit
        and subprocess.run(
            ["git", "merge-base", "--is-ancestor", review_commit, "HEAD"],
            cwd=root,
            capture_output=True,
        ).returncode
        == 0
    )
    relative_path = str(path.relative_to(root))
    tracked = bool(
        _git(
            root,
            "ls-files",
            "--error-unmatch",
            relative_path,
            check=False,
            allow_empty=True,
        )
    )
    clean = not bool(
        _git(
            root,
            "status",
            "--porcelain",
            "--",
            relative_path,
            allow_empty=True,
        )
    )
    matched = (
        record.get("change") == change_id
        and record.get("base_commit") == change["git"]["base_commit"]
        and record.get("proposal_fingerprint") == expected
        and bool(record.get("reviewer"))
        and bool(record.get("review_evidence"))
        and commit_is_ancestor
    )
    valid = matched and tracked and clean
    if valid:
        reason = "matched"
    elif matched and (not tracked or not clean):
        reason = "approval_not_committed"
    elif review_commit and not commit_is_ancestor:
        reason = "review_commit_not_ancestor"
    else:
        reason = "fingerprint_base_or_evidence_mismatch"
    return {
        "present": True,
        "valid": valid,
        "tracked": tracked,
        "clean": clean,
        "review_commit_is_ancestor": commit_is_ancestor,
        "path": str(path),
        "reason": reason,
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
    result = _parse_changed_paths(raw)

    known = {item["path"] for item in result if item.get("path")}
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z", allow_empty=True)
    for path in untracked.split("\0"):
        if path and path not in known:
            result.append({"status": "A", "old_path": "", "path": path})
    return sorted(result, key=lambda item: (item.get("path") or item.get("old_path") or ""))


def _changed_paths_between(
    root: Path,
    before_ref: str,
    after_ref: str,
) -> list[dict[str, str]]:
    raw = _git_bytes(
        root,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        before_ref,
        after_ref,
        "--",
    )
    return sorted(
        _parse_changed_paths(raw),
        key=lambda item: (item.get("path") or item.get("old_path") or ""),
    )


def _parse_changed_paths(raw: bytes) -> list[dict[str, str]]:
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
    return result


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

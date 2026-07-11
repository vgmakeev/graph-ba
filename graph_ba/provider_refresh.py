"""Refresh project-owned observed graph projections before compilation."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .config import ProjectConfig, ProviderRefreshConfig
from .codegraph_provider import resolve_codegraph_database


def provider_refresh_plan(
    root: Path, config: ProjectConfig
) -> list[ProviderRefreshConfig]:
    """Return explicit refreshers plus the conventional Make target fallback."""
    if config.provider_refresh:
        return list(config.provider_refresh)
    makefile = root / "Makefile"
    if makefile.is_file():
        content = makefile.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"(?m)^graphba-observed\s*:", content):
            return [
                ProviderRefreshConfig(
                    name="graphba-observed",
                    command=["make", "graphba-observed"],
                    inputs=[
                        "mini-upsushi/admin/src",
                        "mini-upsushi/mini_app",
                        "mini-upsushi/tests",
                    ],
                    outputs=["reports/graphba/observed"],
                )
            ]
    return []


def refresh_provider_inputs(
    root: Path,
    config: ProjectConfig,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Run missing/configured provider refreshers without invoking a shell."""
    root = root.resolve()
    providers: list[dict[str, Any]] = []
    for item in provider_refresh_plan(root, config):
        output_paths = [root / relative for relative in item.outputs]
        missing = [
            relative
            for relative, path in zip(item.outputs, output_paths)
            if not _materialized(path)
        ]
        stale = False if missing else _inputs_are_newer(root, item.inputs, output_paths)
        should_run = force or item.always or bool(missing) or stale
        if not should_run:
            providers.append(
                {
                    "name": item.name,
                    "status": "current",
                    "command": item.command,
                    "outputs": item.outputs,
                    "missing": [],
                    "stale": False,
                }
            )
            continue
        result = subprocess.run(
            item.command,
            cwd=root,
            capture_output=True,
            text=True,
        )
        missing_after = [
            relative
            for relative, path in zip(item.outputs, output_paths)
            if not _materialized(path)
        ]
        passed = result.returncode == 0 and not missing_after
        providers.append(
            {
                "name": item.name,
                "status": "refreshed" if passed else "failed",
                "command": item.command,
                "outputs": item.outputs,
                "missing": missing_after,
                "stale": stale,
                "returncode": result.returncode,
                "stdout_tail": _tail(result.stdout),
                "stderr_tail": _tail(result.stderr),
            }
        )
    return {
        "configured": bool(providers),
        "pass": all(item["status"] != "failed" for item in providers),
        "refreshed": any(item["status"] == "refreshed" for item in providers),
        "providers": providers,
    }


def codegraph_health(root: Path, config: ProjectConfig) -> dict[str, Any]:
    """Expose symbol-index availability instead of silently hiding fallback quality."""
    if not config.codegraph:
        return {"configured": False, "status": "not_configured"}
    path = resolve_codegraph_database(root, config.codegraph.database)
    exists = path.is_file()
    return {
        "configured": True,
        "status": "current" if exists else "missing",
        "path": str(path),
        "fallback": None if exists else "file_level_code_traces",
        "suggested_action": None
        if exists
        else "restore/copy the CodeGraph index or run the project code-index command",
    }


def _materialized(path: Path) -> bool:
    if path.is_file():
        return True
    if path.is_dir():
        return any(child.is_file() for child in path.rglob("*"))
    return False


def _inputs_are_newer(
    root: Path, inputs: list[str], outputs: list[Path]
) -> bool:
    if not inputs or not outputs:
        return False
    input_mtimes = [
        mtime
        for value in inputs
        for mtime in _path_mtimes(root / value)
    ]
    output_mtimes = [mtime for path in outputs for mtime in _path_mtimes(path)]
    return bool(input_mtimes and output_mtimes) and max(input_mtimes) > min(output_mtimes)


def _path_mtimes(path: Path) -> list[float]:
    if path.is_file():
        return [path.stat().st_mtime]
    if path.is_dir():
        return [child.stat().st_mtime for child in path.rglob("*") if child.is_file()]
    return []


def _tail(value: str, limit: int = 2000) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[-limit:]

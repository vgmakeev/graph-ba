"""Content quality lint checks for graph-ba artifacts."""
from __future__ import annotations

import re
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from graph_ba.db import _FileCache, _resolve_file

_TODO_DEFAULT = re.compile(
    r'(?:TODO|TBD|FIXME|\?\?\?)', re.IGNORECASE
)
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)')
_GLOSSARY_ROW_RE = re.compile(
    r'^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|'
)
_CODE_FENCE_RE = re.compile(r'^```')

def _artifact_line_range(lines: List[str], start: int, heading_level: int) -> Tuple[int, int]:
    """Return (start, end) line indices for an artifact section.

    From `start` until the next heading of same or higher level, or EOF.
    start is 1-based line number, returns 0-based (start_idx, end_idx).
    """
    start_idx = max(start - 1, 0)
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        m = _HEADING_RE.match(lines[i])
        if m and len(m.group(1)) <= heading_level:
            end_idx = i
            break
    return start_idx, end_idx


def _lint_todo_markers(db, fcache: _FileCache, todo_re: re.Pattern,
                       node_id: Optional[str] = None) -> list:
    """Check 1: find TODO/TBD/FIXME markers in artifact source files."""
    findings = []
    query = ("SELECT id, type, source_file, line_number FROM artifacts "
             "WHERE defined = 1 AND type NOT IN ('FILE', 'CODE', 'TEST')")
    params: list = []
    if node_id:
        query += " AND id = ?"
        params.append(node_id)

    for row in db.execute(query, params).fetchall():
        fname = row["source_file"]
        full_path = _resolve_file(db, fname)
        if not full_path:
            continue
        lines = fcache.get_lines(full_path)
        if not lines:
            continue

        art_line = row["line_number"] or 1
        # Determine heading level from the definition line
        m = _HEADING_RE.match(lines[art_line - 1]) if art_line <= len(lines) else None
        hlevel = len(m.group(1)) if m else 2
        start_idx, end_idx = _artifact_line_range(lines, art_line, hlevel)

        for i in range(start_idx, end_idx):
            match = todo_re.search(lines[i])
            if match:
                snippet = lines[i].strip()[:80]
                findings.append({
                    "severity": "WARN", "category": "TODO_TBD",
                    "artifact_id": row["id"], "file": fname,
                    "line": i + 1, "message": snippet,
                })
    return findings


def _lint_empty_sections(db, fcache: _FileCache,
                         node_id: Optional[str] = None) -> list:
    """Check 2: detect markdown headings with no content below them."""
    findings = []
    # Collect unique source files for defined artifacts
    query = ("SELECT DISTINCT source_file FROM artifacts "
             "WHERE defined = 1 AND type NOT IN ('FILE', 'CODE', 'TEST')")
    if node_id:
        query = ("SELECT source_file FROM artifacts "
                 "WHERE id = ? AND defined = 1 AND type NOT IN ('FILE', 'CODE', 'TEST')")

    params = [node_id] if node_id else []
    files_seen: set = set()

    for row in db.execute(query, params).fetchall():
        fname = row["source_file"]
        if fname in files_seen:
            continue
        files_seen.add(fname)
        full_path = _resolve_file(db, fname)
        if not full_path:
            continue
        lines = fcache.get_lines(full_path)
        if not lines:
            continue

        # Walk headings, check for empty content between consecutive headings
        headings: list = []  # (line_idx, level, text)
        for i, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if m:
                headings.append((i, len(m.group(1)), m.group(2).strip()))

        for idx, (line_idx, level, text) in enumerate(headings):
            # Determine end of section
            if idx + 1 < len(headings):
                next_idx = headings[idx + 1][0]
            else:
                next_idx = len(lines)

            # Check if section body is empty (only blank lines or nothing)
            body = lines[line_idx + 1:next_idx]
            has_content = any(l.strip() for l in body)
            if not has_content and level >= 2:
                # Find which artifact owns this section
                owner = db.execute(
                    "SELECT id FROM artifacts WHERE source_file = ? "
                    "AND line_number <= ? AND defined = 1 "
                    "AND type NOT IN ('FILE', 'CODE', 'TEST') "
                    "ORDER BY line_number DESC LIMIT 1",
                    (fname, line_idx + 1)
                ).fetchone()
                aid = owner["id"] if owner else "?"
                findings.append({
                    "severity": "WARN", "category": "EMPTY_SECTION",
                    "artifact_id": aid, "file": fname,
                    "line": line_idx + 1,
                    "message": f"empty section \"{text}\"",
                })
    return findings


def _parse_glossary(glossary_path: str) -> List[Tuple[str, str]]:
    """Parse glossary.md, return list of (canonical, foreign_term) pairs."""
    pairs: list = []
    try:
        text = Path(glossary_path).read_text(encoding="utf-8")
    except Exception:
        return pairs
    for line in text.splitlines():
        m = _GLOSSARY_ROW_RE.match(line)
        if m:
            ru = m.group(1).strip()
            en = m.group(2).strip()
            # Skip short all-caps abbreviations (KDS, ETA, etc.) —
            # they are typically used as-is in Russian text
            if en.isupper() and len(en) <= 6:
                continue
            if en and en != ru and '|' not in en:
                pairs.append((ru, en))
    return pairs


def _lint_terminology(db, fcache: _FileCache, root: Path, config,
                      node_id: Optional[str] = None) -> list:
    """Check 3: flag non-canonical terms based on glossary."""
    findings = []

    # Find glossary file
    lint_cfg = config.lint if config else None
    glossary_path = None
    if lint_cfg and lint_cfg.glossary_file:
        candidate = root / lint_cfg.glossary_file
        if candidate.exists():
            glossary_path = str(candidate)
    if not glossary_path:
        # Auto-discover in scan dirs
        for sd in (config.scan_dirs if config else []):
            candidate = root / sd / "glossary.md"
            if candidate.exists():
                glossary_path = str(candidate)
                break
    if not glossary_path:
        return findings

    pairs = _parse_glossary(glossary_path)
    if not pairs:
        return findings

    # Build regex: search for EN terms in artifact files
    # We match whole words using word boundaries
    en_to_ru = {}
    patterns = []
    for ru, en in pairs:
        # Skip if EN term is a common short word
        if len(en) < 3:
            continue
        en_to_ru[en.lower()] = ru
        patterns.append(re.escape(en))

    if not patterns:
        return findings
    term_re = re.compile(r'\b(' + '|'.join(patterns) + r')\b', re.IGNORECASE)

    ignore_types = {"FILE", "CODE", "TEST"}
    if lint_cfg and lint_cfg.terminology_ignore_types:
        ignore_types.update(str(item) for item in lint_cfg.terminology_ignore_types)

    # Scan artifacts
    placeholders = ",".join("?" for _ in sorted(ignore_types))
    query = ("SELECT id, source_file, line_number FROM artifacts "
             f"WHERE defined = 1 AND type NOT IN ({placeholders})")
    params: list = sorted(ignore_types)
    if node_id:
        query += " AND id = ?"
        params.append(node_id)

    seen: set = set()  # (artifact_id, en_term) — one finding per term per artifact
    for row in db.execute(query, params).fetchall():
        fname = row["source_file"]
        full_path = _resolve_file(db, fname)
        if not full_path or full_path == glossary_path:
            continue
        lines = fcache.get_lines(full_path)
        if not lines:
            continue

        art_line = row["line_number"] or 1
        m = _HEADING_RE.match(lines[art_line - 1]) if art_line <= len(lines) else None
        hlevel = len(m.group(1)) if m else 2
        start_idx, end_idx = _artifact_line_range(lines, art_line, hlevel)

        in_code_fence = False
        for i in range(start_idx, end_idx):
            line = lines[i]
            if _CODE_FENCE_RE.match(line):
                in_code_fence = not in_code_fence
                continue
            if in_code_fence:
                continue
            # Skip inline code
            clean = re.sub(r'`[^`]+`', '', line)
            for match in term_re.finditer(clean):
                en_term = match.group(1)
                canonical = en_to_ru.get(en_term.lower(), "?")
                key = (row["id"], en_term.lower())
                if key in seen:
                    continue
                seen.add(key)
                findings.append({
                    "severity": "INFO", "category": "TERMINOLOGY",
                    "artifact_id": row["id"], "file": fname,
                    "line": i + 1,
                    "message": f"\"{en_term}\" → canonical \"{canonical}\"",
                })
    return findings


def _lint_stale(db, root: Path, config,
                node_id: Optional[str] = None) -> list:
    """Check 4: find artifacts whose source file hasn't been updated since the latest meeting."""
    findings = []
    lint_cfg = config.lint if config else None
    meetings_dir = root / (lint_cfg.meetings_dir if lint_cfg else "00_Inputs/meetings_refined")
    threshold_days = lint_cfg.stale_threshold_days if lint_cfg else 30

    if not meetings_dir.exists():
        return findings

    # Extract meeting dates from filenames
    date_re = re.compile(r'(\d{4}-\d{2}-\d{2})')
    meeting_dates: list = []
    for f in meetings_dir.iterdir():
        m = date_re.search(f.name)
        if m:
            try:
                meeting_dates.append(datetime.strptime(m.group(1), "%Y-%m-%d"))
            except ValueError:
                pass
    if not meeting_dates:
        return findings
    latest_meeting = max(meeting_dates)
    cutoff = latest_meeting - timedelta(days=threshold_days)

    # Get unique source files for artifacts
    query = ("SELECT id, source_file FROM artifacts "
             "WHERE defined = 1 AND type NOT IN ('FILE', 'CODE', 'TEST')")
    params: list = []
    if node_id:
        query += " AND id = ?"
        params.append(node_id)

    file_to_arts: dict = {}
    for row in db.execute(query, params).fetchall():
        fname = row["source_file"]
        full_path = _resolve_file(db, fname)
        if full_path:
            file_to_arts.setdefault(full_path, []).append(row["id"])

    # Get git last-modified date for each file
    for full_path, art_ids in file_to_arts.items():
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%aI", "--", full_path],
                capture_output=True, text=True, cwd=str(root), timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue
            # Parse ISO date (e.g. 2026-03-10T14:30:00+03:00)
            date_str = result.stdout.strip()[:10]
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            continue

        if file_date < cutoff:
            days_ago = (latest_meeting - file_date).days
            for aid in art_ids:
                findings.append({
                    "severity": "WARN", "category": "STALE",
                    "artifact_id": aid, "file": Path(full_path).name,
                    "line": 0,
                    "message": (f"git: {file_date.strftime('%Y-%m-%d')}, "
                                f"latest meeting: {latest_meeting.strftime('%Y-%m-%d')} "
                                f"({days_ago}d behind)"),
                })
    return findings


def _lint_code_coverage(db, config,
                        node_id: Optional[str] = None) -> list:
    """Check 5: find artifacts with no @trace references from code."""
    findings = []
    if not config or not config.code or not config.code.coverage_types:
        return findings

    for art_type in config.code.coverage_types:
        query = ("SELECT a.id, a.title FROM artifacts a "
                 "WHERE a.type = ? AND a.defined = 1 "
                 "AND NOT EXISTS ("
                 "  SELECT 1 FROM edges e "
                 "  WHERE e.target_id = a.id AND e.source_id LIKE 'CODE:%'"
                 ")")
        params: list = [art_type]
        if node_id:
            query += " AND a.id = ?"
            params.append(node_id)

        for row in db.execute(query, params).fetchall():
            findings.append({
                "severity": "INFO", "category": "CODE_COVERAGE",
                "artifact_id": row["id"], "file": "",
                "line": 0,
                "message": "no @trace references from code",
            })
    return findings


def do_lint(db, root: Path, config, node_id: Optional[str] = None,
            quick: bool = False) -> list:
    """Run all lint checks and return a list of findings."""
    fcache = _FileCache()
    findings: list = []

    # Build TODO regex from config
    lint_cfg = config.lint if config else None
    patterns = lint_cfg.todo_patterns if lint_cfg else _TODO_DEFAULT.pattern
    if isinstance(patterns, list):
        todo_re = re.compile(r'(?:' + '|'.join(re.escape(p) for p in patterns) + r')',
                             re.IGNORECASE)
    else:
        todo_re = _TODO_DEFAULT

    findings.extend(_lint_todo_markers(db, fcache, todo_re, node_id))
    findings.extend(_lint_empty_sections(db, fcache, node_id))
    findings.extend(_lint_terminology(db, fcache, root, config, node_id))

    if not quick:
        findings.extend(_lint_stale(db, root, config, node_id))

    findings.extend(_lint_code_coverage(db, config, node_id))

    # Sort: ERR first, then WARN, then INFO; within same severity — by artifact
    sev_order = {"ERR": 0, "WARN": 1, "INFO": 2}
    findings.sort(key=lambda f: (sev_order.get(f["severity"], 9),
                                 f["category"], f["artifact_id"]))
    return findings

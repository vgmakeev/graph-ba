"""Review and per-artifact validation logic."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

from graph_ba.db import _FileCache, _read_snippet, _resolve_file
from graph_ba.lint import _HEADING_RE, _artifact_line_range, _lint_todo_markers

def _check_layer_gaps(db, aid, atype, issues, config=None):
    """Check if this artifact has expected cross-layer links."""
    if config and config.expected_cross_layer:
        expected = config.expected_cross_layer
    else:
        expected = {}
    pairs = expected.get(atype, [])
    for target_type, label in pairs:
        linked = db.execute(
            "SELECT 1 FROM edges e JOIN artifacts a ON e.target_id = a.id "
            "WHERE e.source_id = ? AND a.type = ? "
            "UNION SELECT 1 FROM edges e JOIN artifacts a ON e.source_id = a.id "
            "WHERE e.target_id = ? AND a.type = ?",
            (aid, target_type, aid, target_type)
        ).fetchone()
        if not linked:
            issues.append(("GAP", aid, f"No links to {target_type} ({label})"))


# ── File / section reading helpers ────────────────────────────────

def _read_artifact_section(filepath: str, start_line: int,
                           max_lines: int = 200) -> Optional[str]:
    """Read from an artifact's definition line to the next same-or-higher-level heading.

    For heading-based artifacts (# / ## / ###): reads until the next heading
    of equal or higher level.
    For table-row artifacts (|...): reads the table header + this row + 2 lines.
    Falls back to max_lines if no boundary found.
    """
    p = Path(filepath)
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    if start_line < 1 or start_line > len(lines):
        return None

    idx = start_line - 1
    first = lines[idx]

    # Determine heading level (0 if not a heading)
    heading_level = 0
    for ch in first:
        if ch == '#':
            heading_level += 1
        else:
            break

    # Table row: show header row + separator + this row + a couple more
    if first.lstrip().startswith('|') and heading_level == 0:
        # Walk back to find table header
        tbl_start = idx
        for i in range(idx - 1, max(idx - 5, -1), -1):
            if lines[i].lstrip().startswith('|'):
                tbl_start = i
            else:
                break
        tbl_end = min(len(lines), idx + 3)
        result = []
        for i in range(tbl_start, tbl_end):
            marker = "→" if i == idx else " "
            result.append(f"  {marker}{i+1:4d}│ {lines[i]}")
        return "\n".join(result)

    # Heading or plain text: read until next heading of same/higher level
    result = []
    end = len(lines) if max_lines <= 0 else min(len(lines), idx + max_lines)
    for i in range(idx, end):
        line = lines[i]
        if i > idx and heading_level > 0:
            lvl = 0
            for ch in line:
                if ch == '#':
                    lvl += 1
                else:
                    break
            if lvl > 0 and lvl <= heading_level:
                break
        result.append(f"  {i+1:4d}│ {line}")

    return "\n".join(result)

# ── Numeric extraction for validation ────────────────────────────

_NUM_PATTERNS = [
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*%'), "%"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*[$€£₽¥]'), "currency"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:min(?:ute)?s?|sec(?:ond)?s?|hours?|days?)\b', re.IGNORECASE), "time"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:ms|s|m|h|d)\b'), "time"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:KB|MB|GB|TB)\b', re.IGNORECASE), "size"),
]


def _extract_numbers(text: str) -> List[Tuple[str, str, str]]:
    """Extract numeric values with units from text. Returns [(value, unit, context)]."""
    results = []
    for line in text.splitlines():
        for pattern, unit_label in _NUM_PATTERNS:
            for m in pattern.finditer(line):
                val = m.group(1)
                ctx = line.strip()[:80]
                results.append((val, unit_label, ctx))
    return results



def _print_edge_context(db, arrow, ref_id, ref_type, ref_title,
                        source_file, line_number, edge_context, radius):
    """Print a single edge with its source file snippet."""
    ref_title = ref_title or ""
    print(f"\n  {arrow} [{ref_type or '?'}] {ref_id} — {ref_title[:55]}")
    print(f"    Ref in: {source_file}:{line_number}")
    if edge_context:
        print(f"    Context: {edge_context[:70]}")

    if line_number and source_file:
        full_path = _resolve_file(db, source_file)
        if full_path:
            snippet = _read_snippet(full_path, line_number, radius)
            if snippet:
                print(snippet)


# ── Validation helpers (used by review) ─────────────────────────

_REQUIRED_SECTIONS: dict = {}  # populated from config at review time



def _check_numeric_conflicts(db, aid, fname, full_path,
                              nums: List[Tuple[str, str, str]],
                              issues: List[Tuple[str, str, str]]):
    """Check if numeric values in this artifact conflict with directly connected artifacts.

    Only compares numbers that share the same unit AND have overlapping context words
    (to avoid false positives like "timer 30 min" vs "delivery 10 min").
    Only checks direct neighbors, not FILE nodes.
    """
    if not nums:
        return

    # Get directly connected artifact files (skip FILE nodes)
    connected = db.execute(
        "SELECT DISTINCT a.id as ref_id, a.source_file "
        "FROM edges e JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? AND a.type NOT IN ('FILE', 'TEST') "
        "UNION "
        "SELECT DISTINCT a.id as ref_id, a.source_file "
        "FROM edges e JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? AND a.type NOT IN ('FILE', 'TEST')",
        (aid, aid)
    ).fetchall()

    # Extract nums from connected artifacts
    all_nums: List[Tuple[str, str, str, str]] = []  # (value, unit, context_words, artifact_id)
    for val, unit, ctx_line in nums:
        words = _context_keywords(ctx_line)
        all_nums.append((val, unit, words, aid))

    for conn in connected:
        ref_path = _resolve_file(db, conn["source_file"])
        if not ref_path or not Path(ref_path).exists():
            continue
        try:
            ref_content = Path(ref_path).read_text(encoding="utf-8")
        except Exception:
            continue
        for val, unit, ctx_line in _extract_numbers(ref_content):
            words = _context_keywords(ctx_line)
            all_nums.append((val, unit, words, conn["ref_id"]))

    # Group by unit, then check for conflicts only among entries with overlapping context
    by_unit: dict = {}
    for val, unit, words, src in all_nums:
        by_unit.setdefault(unit, []).append((val, words, src))

    for unit, entries in by_unit.items():
        # Compare pairs: only flag if different values AND shared context words
        seen_conflicts: set = set()
        for i, (v1, w1, s1) in enumerate(entries):
            if s1 != aid:
                continue  # only check from the artifact being validated
            for j, (v2, w2, s2) in enumerate(entries):
                if s2 == aid or v1 == v2:
                    continue
                overlap = w1 & w2
                if len(overlap) >= 2:  # at least 2 shared context words
                    key = (min(v1, v2), max(v1, v2), unit, frozenset({s1, s2}))
                    if key not in seen_conflicts:
                        seen_conflicts.add(key)
                        shared = ", ".join(sorted(overlap)[:3])
                        issues.append(("NUM", aid,
                                       f"{s1}: {v1} {unit} vs {s2}: {v2} {unit}"
                                       f" (shared context: {shared})"))


def _context_keywords(line: str) -> frozenset:
    """Extract meaningful keywords from a context line for matching.
    Uses word length (>=4) to skip stop words in any language.
    """
    words = set()
    for w in re.findall(r'[^\W\d]{4,}', line.lower(), re.UNICODE):
        words.add(w[:6])  # truncate for crude stemming
    return frozenset(words)


def _check_bidirectional(db, aid, atype, issues, expected_bidir=None):
    """Check for one-way links that should be bidirectional."""
    if expected_bidir is None:
        expected_bidir = {}
    expected = expected_bidir.get(atype, [])
    if not expected:
        return

    # Outgoing targets
    out_targets = db.execute(
        "SELECT DISTINCT e.target_id, a.type FROM edges e "
        "JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ?", (aid,)
    ).fetchall()

    for target_row in out_targets:
        tid = target_row["target_id"]
        ttype = target_row["type"]
        if ttype not in expected:
            continue
        # Check if reverse link exists
        rev = db.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ?",
            (tid, aid)
        ).fetchone()
        if not rev:
            issues.append(("REF", aid,
                           f"{aid}→{tid} exists, but {tid}→{aid} is missing"))


def _check_empty_links(db, aid, issues):
    """Find edges with no meaningful context."""
    empties = db.execute(
        "SELECT target_id, source_file, line_number FROM edges "
        "WHERE source_id = ? AND (context IS NULL OR context = '')",
        (aid,)
    ).fetchall()
    for e in empties:
        if e["line_number"] and e["line_number"] > 0:
            # Has line number — check if there's actual text around it
            full_path = _resolve_file(db, e["source_file"])
            if full_path:
                snippet = _read_snippet(full_path, e["line_number"], 1)
                if snippet and len(snippet.strip()) < 20:
                    issues.append(("EMPTY", aid,
                                   f"→{e['target_id']} in {e['source_file']}:{e['line_number']}"
                                   " — bare reference without context"))



# ── Validate command (deterministic per-artifact gate) ──────────

def _artifact_section_text(full_path: str, art_line: int) -> str:
    """Text of an artifact's own section (heading to next same/higher heading)."""
    lines = _FileCache().get_lines(full_path)
    if not lines:
        return ""
    m = _HEADING_RE.match(lines[art_line - 1]) if 0 < art_line <= len(lines) else None
    hlevel = len(m.group(1)) if m else 2
    start_idx, end_idx = _artifact_line_range(lines, art_line, hlevel)
    return "\n".join(lines[start_idx:end_idx])


def _is_meta_node(node_id: str) -> bool:
    return (node_id.startswith("FILE:") or node_id.startswith("CODE:")
            or node_id.startswith("TEST:") or node_id.startswith("UI:"))


def _resolve_artifact(db: sqlite3.Connection, node_id_or_file: str):
    explicit_file = None
    row = db.execute("SELECT * FROM artifacts WHERE id = ?",
                     (node_id_or_file,)).fetchone()
    if row:
        return row, explicit_file

    explicit_file = node_id_or_file
    basename = Path(node_id_or_file).name
    row = db.execute("SELECT * FROM artifacts WHERE id = ?",
                     (f"FILE:{basename}",)).fetchone()
    if row:
        return row, explicit_file

    fname_stem = Path(node_id_or_file).stem
    id_match = re.match(r'^([A-Z]{1,4}-?\d{1,3}(?:\.\d+)*)', fname_stem)
    if id_match:
        row = db.execute("SELECT * FROM artifacts WHERE id = ?",
                         (id_match.group(1),)).fetchone()
        if row:
            return row, explicit_file

    row = db.execute(
        "SELECT * FROM artifacts WHERE type != 'FILE' "
        "AND (source_file = ? OR source_file LIKE ?)",
        (node_id_or_file, f"%{basename}")
    ).fetchone()
    return row, explicit_file


def _review_issues(db: sqlite3.Connection, root: Path, row, *,
                   nums: bool = False, config=None) -> list[dict]:
    issues: List[Tuple[str, str, str]] = []
    node_id = row["id"]
    fname = row["source_file"]
    full_path = _resolve_file(db, fname)
    review_config = config

    if full_path and Path(full_path).exists():
        try:
            content = Path(full_path).read_text(encoding="utf-8")
        except Exception:
            content = ""

        atype = row["type"]
        req_sections = review_config.required_sections if review_config else {}
        bidir_expected = review_config.expected_bidir if review_config else {}

        if atype in req_sections:
            for section in req_sections[atype]:
                if section.lower() not in content.lower():
                    issues.append(("STRUCT", node_id, f"Missing section '{section}'"))

        if nums and content:
            num_vals = _extract_numbers(content)
            _check_numeric_conflicts(db, node_id, fname, full_path, num_vals, issues)

        _check_bidirectional(db, node_id, atype, issues, bidir_expected)
        _check_empty_links(db, node_id, issues)

    _check_layer_gaps(db, node_id, row["type"], issues, review_config)
    return [{"severity": sev, "id": aid, "message": msg}
            for sev, aid, msg in issues]


def run_review(db: sqlite3.Connection, root: Path, config, node_id_or_file: str,
               *, semantic: bool = False, lines: int = 0, nums: bool = False,
               types: Optional[str] = None) -> dict:
    """Return structured review data for an artifact ID or file path."""
    row, explicit_file = _resolve_artifact(db, node_id_or_file)
    if not row:
        return {"error": f"Artifact '{node_id_or_file}' not found "
                         "(neither as ID nor as file)"}

    node_id = row["id"]
    type_filter = {t.strip() for t in types.split(",")} if types else None
    clusters = [
        r["cluster_name"] for r in db.execute(
            "SELECT cluster_name FROM semantic_clusters WHERE artifact_id = ?",
            (node_id,)
        ).fetchall()
    ]
    out_edges = [dict(r) for r in db.execute(
        "SELECT e.target_id as ref_id, a.type, a.title, e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? AND COALESCE(a.type,'') NOT IN ('FILE', 'CODE', 'TEST') "
        "ORDER BY a.type, e.target_id",
        (node_id,)
    ).fetchall()]
    in_edges = [dict(r) for r in db.execute(
        "SELECT e.source_id as ref_id, a.type, a.title, e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? AND COALESCE(a.type,'') != 'FILE' "
        "ORDER BY a.type, e.source_id",
        (node_id,)
    ).fetchall()]

    linked_ids: list[str] = []
    if semantic:
        seen: set[str] = set()
        for r in out_edges + in_edges:
            if type_filter and r.get("type") not in type_filter:
                continue
            rid = r["ref_id"]
            if rid not in seen:
                seen.add(rid)
                linked_ids.append(rid)

    linked_artifacts = []
    for rid in linked_ids:
        art = db.execute("SELECT * FROM artifacts WHERE id = ?", (rid,)).fetchone()
        if not art:
            linked_artifacts.append({"id": rid, "defined": False})
            continue
        section = None
        def_path = _resolve_file(db, art["source_file"]) if art["source_file"] else None
        if def_path:
            section = _read_artifact_section(def_path, art["line_number"] or 1,
                                             max_lines=lines)
        linked_artifacts.append({
            "id": rid,
            "type": art["type"],
            "title": art["title"],
            "source_file": art["source_file"],
            "line_number": art["line_number"],
            "defined": bool(art["defined"]),
            "section": section,
        })

    return {
        "artifact": {
            "id": row["id"],
            "type": row["type"],
            "title": row["title"],
            "source_file": row["source_file"],
            "line_number": row["line_number"],
            "defined": bool(row["defined"]),
        },
        "clusters": clusters,
        "issues": _review_issues(db, root, row, nums=nums, config=config),
        "outgoing": out_edges,
        "incoming": in_edges,
        "linked_artifacts": linked_artifacts,
        "semantic": semantic,
        "explicit_file": explicit_file,
    }


def run_validate(db: sqlite3.Connection, root: Path, config, artifact_id: str) -> dict:
    """Run deterministic per-artifact validation and return the JSON shape."""
    from graph_ba.config import LintConfig

    checks: list = []
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if not row or not row["defined"]:
        detail = ("not found in graph" if not row
                  else "referenced but never defined (dangling)")
        checks.append({"name": "defined", "status": "fail", "detail": detail})
        return {"id": artifact_id, "verdict": "FAIL", "checks": checks}

    checks.append({"name": "defined", "status": "pass",
                   "detail": f"defined in {row['source_file']}:{row['line_number']}"})
    atype = row["type"]

    undefined = db.execute(
        "SELECT DISTINCT e.target_id FROM edges e "
        "LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? AND COALESCE(a.defined, 0) = 0",
        (artifact_id,)
    ).fetchall()
    bad = sorted(r["target_id"] for r in undefined
                 if not _is_meta_node(r["target_id"]))
    if bad:
        checks.append({"name": "dangling_out", "status": "fail",
                       "detail": "outgoing refs to undefined: " + ", ".join(bad)})
    else:
        checks.append({"name": "dangling_out", "status": "pass",
                       "detail": "all outgoing references resolve to defined artifacts"})

    req_sections = config.required_sections if config else {}
    if atype in req_sections:
        full_path = _resolve_file(db, row["source_file"])
        section_text = ""
        if full_path:
            section_text = _artifact_section_text(full_path, row["line_number"] or 1)
        missing = [s for s in req_sections[atype]
                   if s.lower() not in section_text.lower()]
        if missing:
            checks.append({"name": "required_sections", "status": "fail",
                           "detail": "missing sections: " + ", ".join(missing)})
        else:
            checks.append({"name": "required_sections", "status": "pass",
                           "detail": "all required sections present: "
                                     + ", ".join(req_sections[atype])})

    ecl = config.expected_cross_layer if config else {}
    if atype in ecl:
        missing_types = []
        for target_type, label in ecl[atype]:
            linked = db.execute(
                "SELECT 1 FROM edges e JOIN artifacts a ON e.target_id = a.id "
                "WHERE e.source_id = ? AND a.type = ? "
                "UNION SELECT 1 FROM edges e JOIN artifacts a ON e.source_id = a.id "
                "WHERE e.target_id = ? AND a.type = ?",
                (artifact_id, target_type, artifact_id, target_type)
            ).fetchone()
            if not linked:
                missing_types.append(f"{target_type} ({label})")
        if missing_types:
            checks.append({"name": "expected_cross_layer", "status": "fail",
                           "detail": "no links to expected types: "
                                     + ", ".join(missing_types)})
        else:
            checks.append({"name": "expected_cross_layer", "status": "pass",
                           "detail": "all expected cross-layer links present"})

    ebd = config.expected_bidir if config else {}
    if atype in ebd:
        bidir_issues: List[Tuple[str, str, str]] = []
        _check_bidirectional(db, artifact_id, atype, bidir_issues, ebd)
        if bidir_issues:
            checks.append({"name": "expected_bidir", "status": "warn",
                           "detail": "; ".join(msg for _, _, msg in bidir_issues)})
        else:
            checks.append({"name": "expected_bidir", "status": "pass",
                           "detail": "expected bidirectional links present"})

    lint_cfg = config.lint if config else None
    patterns = lint_cfg.todo_patterns if lint_cfg else LintConfig().todo_patterns
    todo_re = re.compile(
        r'(?:' + '|'.join(re.escape(p) for p in patterns) + r')', re.IGNORECASE)
    todo_findings = _lint_todo_markers(db, _FileCache(), todo_re, artifact_id)
    if todo_findings:
        first = todo_findings[0]
        checks.append({"name": "todo_markers", "status": "warn",
                       "detail": f"{len(todo_findings)} marker(s), e.g. "
                                 f"{first['file']}:{first['line']}: {first['message']}"})
    else:
        checks.append({"name": "todo_markers", "status": "pass",
                       "detail": "no TODO/TBD markers"})

    tests_cfg = config.tests if config else None
    if tests_cfg and atype in tests_cfg.coverage_types:
        test_files = db.execute(
            "SELECT count(DISTINCT source_id) as c FROM edges "
            "WHERE target_id = ? AND source_id LIKE 'TEST:%'",
            (artifact_id,)
        ).fetchone()["c"]
        if test_files:
            checks.append({"name": "test_evidence", "status": "pass",
                           "detail": f"referenced by {test_files} test file(s)"})
        else:
            checks.append({"name": "test_evidence", "status": "warn",
                           "detail": "no TEST references (no test evidence)"})

    verdict = "FAIL" if any(c["status"] == "fail" for c in checks) else "PASS"
    return {"id": artifact_id, "verdict": verdict, "checks": checks}

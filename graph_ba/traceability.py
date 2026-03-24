"""
Traceability Scanner — builds a cross-reference graph
of BA artifacts (ST, BR, BR-XX, BP, BD, BF, F, VAD, M, DM, SM, EN, RL) and
reports coverage gaps, orphans, and dangling references.

Usage:
    trace-ba --root /path/to/project
    trace-ba --root . --json-out reports/graph.json --dot-out reports/graph.dot -v
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import click
import networkx as nx

# ── Artifact types ────────────────────────────────────────────────

class ArtifactType(Enum):
    ST = "ST"
    BR_REQ = "BR_REQ"
    BR_RULE = "BR_RULE"
    BP = "BP"
    BD = "BD"
    BF = "BF"
    F = "F"
    VAD = "VAD"
    M = "M"
    DM = "DM"
    SM = "SM"
    EN = "EN"
    RL = "RL"


LABELS = {
    ArtifactType.ST: "Stakeholder Expectations",
    ArtifactType.BR_REQ: "Business Requirements (BR.X)",
    ArtifactType.BR_RULE: "Business Rules (BR-XX)",
    ArtifactType.BP: "Business Processes (BP-XX)",
    ArtifactType.BD: "Business Decisions (BD-XX)",
    ArtifactType.BF: "Business Functions (BF.XX)",
    ArtifactType.F: "Features (F-XX)",
    ArtifactType.VAD: "Value Chains (VAD-XX)",
    ArtifactType.M: "Modules (M-XX)",
    ArtifactType.DM: "Domain Model (DM-X.Y)",
    ArtifactType.SM: "Status Models (SM-XX)",
    ArtifactType.EN: "Enums / Reference Data (EN-XX)",
    ArtifactType.RL: "Roles & Permissions (RL-XX)",
}

# ── Data model ────────────────────────────────────────────────────

@dataclass
class Artifact:
    id: str
    artifact_type: ArtifactType
    source_file: Path
    line_number: int
    title: str = ""


@dataclass
class Reference:
    target_id: str
    source_file: Path
    line_number: int
    context: str = ""


# ── Regex patterns for matching references in text ────────────────

# Order matters: more specific patterns first to avoid false matches.
# Each tuple: (ArtifactType, compiled_regex, group_index_for_full_id)

def _build_ref_patterns():
    """Build list of (ArtifactType, regex) for scanning references."""
    return [
        (ArtifactType.BF, re.compile(r'(?<![A-Za-zА-Яа-я])(BF\.\d{2}\.\d+)(?!\d)')),
        (ArtifactType.BR_REQ, re.compile(r'(?<![A-Za-zА-Яа-я-])(BR\.\d+(?:\.\d+)?)(?!\.\d)')),
        (ArtifactType.BR_RULE, re.compile(r'(?<![A-Za-zА-Яа-я.])(BR-\d{2})(?!\d)')),
        (ArtifactType.VAD, re.compile(r'(?<![A-Za-zА-Яа-я])(VAD-\d{2})(?!\d)')),
        (ArtifactType.BP, re.compile(r'(?<![A-Za-zА-Яа-я])(BP-\d{2}[a-z]?)(?![a-z\d])')),
        (ArtifactType.BD, re.compile(r'(?<![A-Za-zА-Яа-я])(BD-\d{2})(?!\d)')),
        (ArtifactType.ST, re.compile(r'(?<![A-Za-zА-Яа-я])(ST-\d{2})(?!\d)')),
        (ArtifactType.F, re.compile(r'(?<![A-Za-zА-Яа-я])(F-\d{2})(?!\d)')),
        # Module: Cyrillic М + digits  (Latin M only in module_decomposition.md)
        (ArtifactType.M, re.compile(r'(?<![A-Za-zА-Яа-я])([МM]\d{1,2})(?!\d)')),
        # Domain Model: DM-X.Y
        (ArtifactType.DM, re.compile(r'(?<![A-Za-zА-Яа-я])(DM-\d+\.\d+)(?!\d)')),
        # Status Models: SM-XX
        (ArtifactType.SM, re.compile(r'(?<![A-Za-zА-Яа-я])(SM-\d{2})(?!\d)')),
        # Enums: EN-XX
        (ArtifactType.EN, re.compile(r'(?<![A-Za-zА-Яа-я])(EN-\d{2})(?!\d)')),
        # Roles: RL-XX
        (ArtifactType.RL, re.compile(r'(?<![A-Za-zА-Яа-я])(RL-\d{2})(?!\d)')),
    ]


REF_PATTERNS = _build_ref_patterns()

# Range pattern: e.g. BR.12.1–BR.12.6, BF.02.5–BF.02.11
RANGE_RE = re.compile(
    r'((?:BR|BF)\.\d+\.)(\d+)\s*[–\-]\s*(?:(?:BR|BF)\.\d+\.)(\d+)'
)

# ── ID normalisation ─────────────────────────────────────────────

def normalize_id(raw: str) -> str:
    """Canonical form: Latin M, zero-padded module numbers."""
    # Cyrillic М → Latin M
    s = raw.replace("М", "M")
    # Zero-pad module numbers: M1 → M01
    m = re.fullmatch(r'M(\d{1,2})', s)
    if m:
        return f"M{int(m.group(1)):02d}"
    return s


def classify_id(raw: str) -> Optional[ArtifactType]:
    nid = normalize_id(raw)
    if re.fullmatch(r'BF\.\d{2}\.\d+', nid):
        return ArtifactType.BF
    if re.fullmatch(r'BR\.\d+(?:\.\d+)?', nid):
        return ArtifactType.BR_REQ
    if re.fullmatch(r'BR-\d{2}', nid):
        return ArtifactType.BR_RULE
    if re.fullmatch(r'VAD-\d{2}', nid):
        return ArtifactType.VAD
    if re.fullmatch(r'BP-\d{2}[a-z]?', nid):
        return ArtifactType.BP
    if re.fullmatch(r'BD-\d{2}', nid):
        return ArtifactType.BD
    if re.fullmatch(r'ST-\d{2}', nid):
        return ArtifactType.ST
    if re.fullmatch(r'F-\d{2}', nid):
        return ArtifactType.F
    if re.fullmatch(r'M\d{2}', nid):
        return ArtifactType.M
    return None


# ── Phase 1: Definition scanning ─────────────────────────────────

def _read_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _register(registry: Dict[str, Artifact], art: Artifact):
    nid = normalize_id(art.id)
    art.id = nid
    if nid not in registry:
        registry[nid] = art


def scan_definitions(root: Path) -> Dict[str, Artifact]:
    disc = root / "02_Discovery"
    registry: Dict[str, Artifact] = {}

    # ST — headings in 01_Stakeholder_Expectations.md
    _scan_heading(registry, disc / "01_Stakeholder_Expectations.md",
                  re.compile(r'^###\s+(ST-\d{2})\s*[—–\-]\s*(.+)'), ArtifactType.ST)

    # BR.X.Y — table rows in 03_Business_Requirements.md
    _scan_table_first_col(registry, disc / "03_Business_Requirements.md",
                          re.compile(r'^\|\s*(BR\.\d+(?:\.\d+)?)\s*\|'), ArtifactType.BR_REQ)

    # BR-XX — H1 in individual files
    for f in sorted((disc / "06_Business_Rules").glob("BR-*.md")):
        _scan_heading(registry, f,
                      re.compile(r'^#\s+(BR-\d{2})\b\s*[—–\-:]*\s*(.*)'), ArtifactType.BR_RULE)

    # BR-XX — also register from index table (for row-level reference attribution)
    _scan_table_first_col(registry, disc / "06_Business_Rules.md",
                          re.compile(r'^\|\s*(BR-\d{2})\s*\|'), ArtifactType.BR_RULE)

    # BP-XX — H1 in E2E flow files
    for f in sorted((disc / "05_E2E_Flows").rglob("BP-*.md")):
        _scan_heading(registry, f,
                      re.compile(r'^#\s+(BP-\d{2}[a-z]?)\b\s*(.*)'), ArtifactType.BP)

    # BD-XX — H1 in decision files
    decisions_dir = disc / "Research" / "Decisions"
    if decisions_dir.exists():
        for f in sorted(decisions_dir.glob("BD-*.md")):
            # Skip _client duplicates — extract base BD-XX
            _scan_heading(registry, f,
                          re.compile(r'^#\s+(BD-\d{2})\b\s*[—–\-:]*\s*(.*)'), ArtifactType.BD)

    # BF.XX.Y — table rows
    _scan_table_first_col(registry, disc / "07_Business_Functions.md",
                          re.compile(r'^\|\s*(BF\.\d{2}\.\d+)\s*\|'), ArtifactType.BF)

    # F-XX — table rows
    _scan_table_first_col(registry, disc / "08_Features_List.md",
                          re.compile(r'^\|\s*(F-\d{2})\s*\|'), ArtifactType.F)

    # VAD-XX — table rows
    _scan_table_first_col(registry, disc / "04_Business_Processes_Structure.md",
                          re.compile(r'^\|\s*(VAD-\d{2})\s*\|'), ArtifactType.VAD)

    # М-XX — table rows in module_decomposition.md
    mod_file = disc / "module_decomposition.md"
    if mod_file.exists():
        _scan_table_first_col(registry, mod_file,
                              re.compile(r'^\|\s*([МM]\d{1,2})\s*\|'), ArtifactType.M)

    # DM-X.Y — headings in domain_model/*.md
    dm_dir = disc / "domain_model"
    if dm_dir.exists():
        for f in sorted(dm_dir.glob("0[1-5]_*.md")):
            _scan_heading(registry, f,
                          re.compile(r'^##\s+(DM-\d+\.\d+)\s*[—–\-:]*\s*(.*)'), ArtifactType.DM)

    # SM-XX — headings in domain_model/06_statuses.md
    statuses_file = dm_dir / "06_statuses.md" if dm_dir.exists() else disc / "domain_model" / "06_statuses.md"
    _scan_heading(registry, statuses_file,
                  re.compile(r'^##\s+(SM-\d{2})\s*[—–\-:]*\s*(.*)'), ArtifactType.SM)

    # EN-XX — headings in domain_model/08_enums.md
    enums_file = dm_dir / "08_enums.md" if dm_dir.exists() else disc / "domain_model" / "08_enums.md"
    _scan_heading(registry, enums_file,
                  re.compile(r'^##\s+(EN-\d{2})\s*[—–\-:]*\s*(.*)'), ArtifactType.EN)

    # RL-XX — headings in roles-permissions.md
    roles_file = disc / "roles-permissions.md"
    _scan_heading(registry, roles_file,
                  re.compile(r'^##\s+(RL-\d{2})\s*[—–\-:]*\s*(.*)'), ArtifactType.RL)

    return registry


def _scan_heading(registry, filepath, pattern, atype):
    if not filepath.exists():
        return
    for i, line in enumerate(_read_lines(filepath), 1):
        m = pattern.match(line)
        if m:
            raw_id = m.group(1)
            title = m.group(2).strip() if m.lastindex >= 2 else ""
            _register(registry, Artifact(raw_id, atype, filepath, i, title))


def _scan_table_first_col(registry, filepath, pattern, atype):
    if not filepath.exists():
        return
    for i, line in enumerate(_read_lines(filepath), 1):
        m = pattern.match(line)
        if m:
            raw_id = m.group(1)
            # Extract title: second table column
            cols = [c.strip() for c in line.split("|")]
            title = cols[2] if len(cols) > 2 else ""
            _register(registry, Artifact(raw_id, atype, filepath, i, title))


# ── Phase 2: Reference extraction ────────────────────────────────

def scan_index_cross_refs(root: Path) -> List[Tuple[str, str, Path, int]]:
    """Parse index tables where first column is the 'source' artifact and other
    columns contain 'target' artifact IDs.

    Returns list of (source_id, target_id, file, line_number).
    """
    disc = root / "02_Discovery"
    results: List[Tuple[str, str, Path, int]] = []

    # 06_Business_Rules.md — row: | BR-XX | ... | BP-XX, BP-XX | ... |
    _parse_index_table(results, disc / "06_Business_Rules.md",
                       re.compile(r'^\|\s*(BR-\d{2})\s*\|'))

    # 08_Features_List.md — row: | F-XX | ... | BR.X, BR.X.Y | BF.XX.Y | ... |
    _parse_index_table(results, disc / "08_Features_List.md",
                       re.compile(r'^\|\s*(F-\d{2})\s*\|'))

    # 07_Business_Functions.md — row: | BF.XX.Y | ... | BR.X, BR.X.Y | ... |
    _parse_index_table(results, disc / "07_Business_Functions.md",
                       re.compile(r'^\|\s*(BF\.\d{2}\.\d+)\s*\|'))

    return results


def _parse_index_table(
    results: List[Tuple[str, str, Path, int]],
    filepath: Path,
    first_col_re: re.Pattern,
):
    """For each row matching first_col_re, find the source ID in column 1,
    then scan the REST of the row for target artifact IDs."""
    if not filepath.exists():
        return
    lines = _read_lines(filepath)
    for line_num, line in enumerate(lines, 1):
        m = first_col_re.match(line)
        if not m:
            continue
        source_id = normalize_id(m.group(1))

        # Everything after the first two pipes = "other columns"
        rest = line[m.end():]

        # Find all artifact IDs in the rest of the row
        for _, pat in REF_PATTERNS:
            for rm in pat.finditer(rest):
                target_id = normalize_id(rm.group(1))
                if target_id != source_id:
                    results.append((source_id, target_id, filepath, line_num))

        # Also expand ranges in the row
        for eid in expand_ranges(rest):
            nid = normalize_id(eid)
            if nid != source_id:
                results.append((source_id, nid, filepath, line_num))


def expand_ranges(text: str) -> List[str]:
    """Expand BR.12.1–BR.12.6 or BF.02.5–BF.02.11 into individual IDs."""
    results = []
    for m in RANGE_RE.finditer(text):
        prefix, start_s, end_s = m.group(1), m.group(2), m.group(3)
        for i in range(int(start_s), int(end_s) + 1):
            results.append(f"{prefix}{i}")
    return results


def scan_references(
    root: Path,
    registry: Dict[str, Artifact],
    module_ref_in_all_files: bool = False,
) -> List[Reference]:
    """Scan all .md files for cross-references to known artifact types."""
    scan_dirs = [
        root / "02_Discovery",
        root / "04_PRD",
    ]
    md_files: List[Path] = []
    for d in scan_dirs:
        if d.exists():
            md_files.extend(sorted(d.rglob("*.md")))

    # Determine which files can have module refs (avoid false positives)
    module_files = set()
    mod_file = root / "02_Discovery" / "module_decomposition.md"
    if mod_file.exists():
        module_files.add(mod_file)
    # PRD files may contain Module mapping sections
    prd_dir = root / "04_PRD"
    if prd_dir.exists():
        module_files.update(prd_dir.rglob("*.md"))

    all_refs: List[Reference] = []

    for filepath in md_files:
        lines = _read_lines(filepath)
        in_code_fence = False
        current_section = ""

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            # Track code fences
            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                continue
            if in_code_fence:
                continue

            # Track section headings
            if stripped.startswith("## "):
                current_section = stripped.lstrip("# ").strip()

            # Expand ranges first
            expanded = expand_ranges(line)
            for eid in expanded:
                nid = normalize_id(eid)
                all_refs.append(Reference(nid, filepath, line_num, current_section))

            # Match all patterns
            for atype, pat in REF_PATTERNS:
                # Skip module pattern in non-module files to avoid false positives
                if atype == ArtifactType.M and not module_ref_in_all_files:
                    if filepath not in module_files:
                        continue

                for m in pat.finditer(line):
                    raw_id = m.group(1)
                    nid = normalize_id(raw_id)
                    all_refs.append(Reference(nid, filepath, line_num, current_section))

    return all_refs


# ── Phase 3: Graph construction ──────────────────────────────────

def _find_owner(
    file_arts: List[Tuple[int, str]],
    ref_line: int,
) -> Optional[str]:
    """Find the artifact defined at or just before ref_line (same table row or section).

    For table files, definition and reference are on the SAME line.
    For section files, the definition is the nearest preceding heading.
    """
    if not file_arts:
        return None
    if len(file_arts) == 1:
        return file_arts[0][1]
    # Binary search: find the last artifact defined at or before ref_line
    best = None
    for def_line, aid in file_arts:
        if def_line <= ref_line:
            best = aid
        else:
            break
    return best


def build_graph(
    registry: Dict[str, Artifact],
    references: List[Reference],
    index_xrefs: Optional[List[Tuple[str, str, Path, int]]] = None,
) -> nx.DiGraph:
    G = nx.DiGraph()

    # Nodes: all defined artifacts
    for aid, art in registry.items():
        G.add_node(aid, type=art.artifact_type.value, title=art.title,
                   source_file=str(art.source_file.name))

    # Build file → sorted [(line, artifact_id)] mapping
    file_arts_map: Dict[Path, List[Tuple[int, str]]] = defaultdict(list)
    for aid, art in registry.items():
        file_arts_map[art.source_file].append((art.line_number, aid))
    for k in file_arts_map:
        file_arts_map[k].sort()

    # Build edges using line-proximity matching
    for ref in references:
        target = ref.target_id
        # Ensure target node exists (might be dangling)
        if target not in G:
            atype = classify_id(target)
            G.add_node(target, type=atype.value if atype else "UNKNOWN",
                       title="", source_file="", defined=False)

        file_arts = file_arts_map.get(ref.source_file)
        owner = _find_owner(file_arts, ref.line_number) if file_arts else None

        if owner and owner != target:
            G.add_edge(owner, target, context=ref.context,
                       source_file=str(ref.source_file.name),
                       line=ref.line_number)
        elif not owner:
            # File-level reference (e.g. PRD, other docs without definitions)
            file_node = f"FILE:{ref.source_file.name}"
            if not G.has_node(file_node):
                G.add_node(file_node, type="FILE", title=ref.source_file.name,
                           source_file=str(ref.source_file.name), defined=True)
            if target != file_node:
                G.add_edge(file_node, target, context=ref.context,
                           source_file=str(ref.source_file.name),
                           line=ref.line_number)

    # Add index cross-references (explicit source→target from table rows)
    if index_xrefs:
        for src, tgt, fpath, lnum in index_xrefs:
            if tgt not in G:
                atype = classify_id(tgt)
                G.add_node(tgt, type=atype.value if atype else "UNKNOWN",
                           title="", source_file="", defined=False)
            if src not in G:
                continue  # source not in registry
            if src != tgt:
                G.add_edge(src, tgt, context="index_table",
                           source_file=str(fpath.name), line=lnum)

    # Mark defined nodes
    for aid in registry:
        G.nodes[aid]["defined"] = True

    # Resolve bare BP-01 → BP-01a, BP-01b
    _resolve_bare_bp(G, registry)

    return G


def _resolve_bare_bp(G: nx.DiGraph, registry: Dict[str, Artifact]):
    """If BP-01 is referenced but not defined, link to BP-01a/BP-01b if they exist."""
    dangling_bps = [
        n for n in G.nodes()
        if n.startswith("BP-") and not G.nodes[n].get("defined", False)
    ]
    for bp in dangling_bps:
        base = bp  # e.g. BP-01
        variants = [aid for aid in registry if aid.startswith(base) and aid != base]
        if variants:
            # Redirect all edges TO this node to point to each variant
            preds = list(G.predecessors(bp))
            for pred in preds:
                edge_data = G.edges[pred, bp]
                for var in variants:
                    if pred != var:
                        G.add_edge(pred, var, **edge_data)
            succs = list(G.successors(bp))
            for succ in succs:
                edge_data = G.edges[bp, succ]
                for var in variants:
                    if var != succ:
                        G.add_edge(var, succ, **edge_data)
            G.remove_node(bp)


# ── Phase 4: Verification checks ────────────────────────────────

@dataclass
class TraceReport:
    registry_count: Dict[str, int] = field(default_factory=dict)
    total_edges: int = 0
    orphans: List[str] = field(default_factory=list)
    dangling: List[Tuple[str, str, int]] = field(default_factory=list)  # (id, file, line)
    coverage: Dict[str, dict] = field(default_factory=dict)
    missing_expected: List[Tuple[str, str]] = field(default_factory=list)  # (id, missing_type)


def verify(
    G: nx.DiGraph,
    registry: Dict[str, Artifact],
    references: List[Reference],
) -> TraceReport:
    report = TraceReport()

    # Registry counts
    type_counts: Dict[str, int] = defaultdict(int)
    for art in registry.values():
        type_counts[art.artifact_type.value] += 1
    report.registry_count = dict(type_counts)
    report.total_edges = G.number_of_edges()

    # Orphans: defined but no incoming edges from other artifacts
    for aid in registry:
        if G.in_degree(aid) == 0:
            report.orphans.append(aid)

    # Dangling: referenced but not defined
    defined_ids = set(registry.keys())
    dangling_ids: Set[str] = set()
    for ref in references:
        if ref.target_id not in defined_ids:
            # Check if it was resolved (bare BP)
            if ref.target_id in G:
                continue
            dangling_ids.add(ref.target_id)

    # Collect first occurrence for each dangling ID
    dangling_first: Dict[str, Tuple[str, int]] = {}
    for ref in references:
        if ref.target_id in dangling_ids and ref.target_id not in dangling_first:
            dangling_first[ref.target_id] = (str(ref.source_file.name), ref.line_number)
    for did, (fname, lnum) in sorted(dangling_first.items()):
        report.dangling.append((did, fname, lnum))

    # Coverage matrix
    expected_pairs = [
        ("BR_RULE", "BR_REQ", "BR-XX → BR.X"),
        ("BR_RULE", "BP", "BR-XX → BP-XX"),
        ("F", "BR_REQ", "F-XX → BR.X"),
        ("F", "BF", "F-XX → BF.XX"),
        ("BF", "BR_REQ", "BF.XX → BR.X"),
        ("BP", "VAD", "BP-XX → VAD-XX"),
        ("BP", "BD", "BP-XX → BD-XX"),
    ]
    for src_type, tgt_type, label in expected_pairs:
        src_ids = [aid for aid, art in registry.items() if art.artifact_type.value == src_type]
        linked = []
        missing = []
        for sid in src_ids:
            if sid in G:
                has_link = any(
                    G.nodes.get(t, {}).get("type") == tgt_type
                    for t in G.successors(sid)
                )
                if has_link:
                    linked.append(sid)
                else:
                    missing.append(sid)
            else:
                missing.append(sid)
        total = len(src_ids)
        pct = (len(linked) / total * 100) if total > 0 else 0
        report.coverage[label] = {
            "total": total,
            "linked": len(linked),
            "pct": round(pct, 1),
            "missing": missing,
        }

    # Missing expected links (individual)
    for aid, art in registry.items():
        if art.artifact_type == ArtifactType.BR_RULE and aid in G:
            if not any(G.nodes.get(t, {}).get("type") == "BR_REQ" for t in G.successors(aid)):
                report.missing_expected.append((aid, "BR.X (нормативная ссылка)"))
            if not any(G.nodes.get(t, {}).get("type") == "BP" for t in G.successors(aid)):
                report.missing_expected.append((aid, "BP-XX (применение)"))
        if art.artifact_type == ArtifactType.F and aid in G:
            if not any(G.nodes.get(t, {}).get("type") == "BR_REQ" for t in G.successors(aid)):
                report.missing_expected.append((aid, "BR.X (требования)"))
            if not any(G.nodes.get(t, {}).get("type") == "BF" for t in G.successors(aid)):
                report.missing_expected.append((aid, "BF.XX (бизнес-функции)"))

    return report


# ── Output: Console report ───────────────────────────────────────

def print_report(report: TraceReport, registry: Dict[str, Artifact], verbose: bool):
    print("=" * 60)
    print("  Traceability Report")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # Registry summary
    print("\n--- Registry Summary ---")
    total = 0
    for atype in ArtifactType:
        cnt = report.registry_count.get(atype.value, 0)
        total += cnt
        print(f"  {LABELS[atype]:40s} {cnt:>4d}")
    print(f"  {'TOTAL':40s} {total:>4d} artifacts, {report.total_edges} edges")

    # Coverage matrix
    print("\n--- Coverage Matrix ---")
    for label, data in report.coverage.items():
        status = "OK" if data["pct"] >= 100 else f"WARN: {len(data['missing'])} missing"
        bar = f"{data['linked']}/{data['total']} ({data['pct']}%)"
        print(f"  {label:25s} {bar:>20s}  [{status}]")
        if verbose and data["missing"]:
            for mid in data["missing"]:
                print(f"       - {mid}")

    # Dangling references
    if report.dangling:
        print(f"\n--- Dangling References ({len(report.dangling)}) ---")
        for did, fname, lnum in report.dangling:
            print(f"  [ERROR] {did} referenced in {fname}:{lnum} but NOT defined")
    else:
        print("\n--- Dangling References: none ---")

    # Orphans
    if report.orphans:
        print(f"\n--- Orphan Artifacts ({len(report.orphans)}) ---")
        orphan_types = defaultdict(list)
        for oid in report.orphans:
            if oid in registry:
                orphan_types[registry[oid].artifact_type.value].append(oid)
        for tval, ids in sorted(orphan_types.items()):
            print(f"  [{tval}] ({len(ids)}): {', '.join(sorted(ids))}")
    else:
        print("\n--- Orphan Artifacts: none ---")

    # Missing expected links
    if report.missing_expected:
        print(f"\n--- Missing Expected Links ({len(report.missing_expected)}) ---")
        for aid, missing_type in sorted(report.missing_expected):
            print(f"  [WARN] {aid} has no link to {missing_type}")
    else:
        print("\n--- Missing Expected Links: none ---")

    # Summary line
    errors = len(report.dangling)
    warnings = len(report.orphans) + len(report.missing_expected)
    ok_pairs = sum(1 for d in report.coverage.values() if d["pct"] >= 100)
    print(f"\n{'=' * 60}")
    print(f"  {errors} errors, {warnings} warnings, "
          f"{ok_pairs}/{len(report.coverage)} coverage pairs at 100%")
    print(f"{'=' * 60}")


# ── Output: JSON export ──────────────────────────────────────────

def export_json(G: nx.DiGraph, registry: Dict[str, Artifact],
                report: TraceReport, path: Path):
    data = {
        "metadata": {
            "generated": datetime.now().isoformat(),
            "artifact_count": len(registry),
            "edge_count": G.number_of_edges(),
        },
        "nodes": [
            {
                "id": n,
                "type": d.get("type", ""),
                "title": d.get("title", ""),
                "source_file": d.get("source_file", ""),
                "defined": d.get("defined", False),
            }
            for n, d in G.nodes(data=True)
        ],
        "edges": [
            {
                "source": u,
                "target": v,
                "context": d.get("context", ""),
                "source_file": d.get("source_file", ""),
                "line": d.get("line", 0),
            }
            for u, v, d in G.edges(data=True)
        ],
        "report": {
            "dangling": [{"id": d, "file": f, "line": l} for d, f, l in report.dangling],
            "orphans": report.orphans,
            "coverage": report.coverage,
            "missing_expected": [{"id": a, "missing": m} for a, m in report.missing_expected],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON exported to {path}")


# ── Output: DOT export ───────────────────────────────────────────

DOT_COLORS = {
    "ST": "#FFF3C4",       # soft amber
    "BR_REQ": "#C3DAF9",   # soft blue
    "BR_RULE": "#FCCFCF",  # soft red
    "BP": "#C6F6D5",       # soft green
    "BD": "#FEEBC8",       # soft orange
    "BF": "#E9D8FD",       # soft purple
    "F": "#B2F5EA",        # soft teal
    "VAD": "#FED7E2",      # soft pink
    "M": "#E2E8F0",        # soft grey
    "FILE": "#F7FAFC",     # very light grey
    "UNKNOWN": "#FED7D7",  # soft red
}

DOT_BORDER_COLORS = {
    "ST": "#D69E2E",
    "BR_REQ": "#2B6CB0",
    "BR_RULE": "#C53030",
    "BP": "#276749",
    "BD": "#C05621",
    "BF": "#6B46C1",
    "F": "#285E61",
    "VAD": "#B83280",
    "M": "#4A5568",
    "FILE": "#A0AEC0",
    "UNKNOWN": "#E53E3E",
}

DOT_SHAPES = {
    "ST": "house",          # stakeholders at the top
    "BR_REQ": "box",        # requirements — rectangular
    "BR_RULE": "octagon",   # rules — stop-sign shape
    "BP": "cds",            # processes — cylinder-like
    "BD": "diamond",        # decisions
    "BF": "component",      # business functions
    "F": "note",            # features
    "VAD": "hexagon",       # value chains
    "M": "box3d",           # modules — 3D box
    "FILE": "folder",       # file references
    "UNKNOWN": "plaintext",
}

DOT_CLUSTER_LABELS = {
    "ST": "Стейкхолдеры",
    "BR_REQ": "Бизнес-требования (BR.X)",
    "BR_RULE": "Бизнес-правила (BR-XX)",
    "BP": "Бизнес-процессы (BP-XX)",
    "BD": "Решения (BD-XX)",
    "BF": "Бизнес-функции (BF.XX)",
    "F": "Фичи (F-XX)",
    "VAD": "Цепочки ценности (VAD-XX)",
    "M": "Модули (М-XX)",
    "FILE": "Документы",
}

# Logical hierarchy ranks (top → bottom)
DOT_RANK_ORDER = ["ST", "BR_REQ", "BR_RULE", "BP", "BD", "BF", "VAD", "F", "M"]

DOT_EDGE_COLORS = {
    "ST": "#D69E2E",
    "BR_REQ": "#2B6CB0",
    "BR_RULE": "#C53030",
    "BP": "#276749",
    "BD": "#C05621",
    "BF": "#6B46C1",
    "F": "#285E61",
    "VAD": "#B83280",
    "M": "#4A5568",
    "FILE": "#A0AEC0",
    "UNKNOWN": "#E53E3E",
}


# ── Output: ARTIFACT_INDEX.md ─────────────────────────────────────

# Semantic clusters: glossary term → related artifact IDs
# Derived from project structure and process/feature naming.
SEMANTIC_CLUSTERS = {
    "Приём заказа / КЦ / Касса": [
        "BP-01a", "BP-01b", "F-01", "F-02", "F-03",
        "BF.01.1", "BF.01.2", "BF.01.3", "BF.01.4", "BF.01.5",
        "BF.01.6", "BF.01.7", "BF.01.8", "BF.01.9",
        "BR.1", "BR.1.1", "BR.1.2", "BR.1.3",
        "BR.6", "BR.6.1", "BR.6.2", "BR.6.3",
        "BR.7", "BR.7.1", "BR.7.2", "BR.7.3",
        "BR-10", "BR-12", "BR-13", "BR-14", "BR-16", "BR-17", "BR-18",
        "BD-01", "BD-13", "BD-14",
        "M08",
    ],
    "Набивка кухни / Производительность": [
        "BP-02", "F-04",
        "BF.02.1", "BF.02.2", "BF.02.3", "BF.02.4",
        "BR.2", "BR.2.1", "BR.2.2", "BR.2.3", "BR.2.4",
        "BR-01", "BR-02", "BR-11", "BR-15", "BR-19", "BR-23",
        "BD-02",
        "M09",
    ],
    "KDS повара / Приготовление": [
        "BP-03", "F-05", "F-07",
        "BF.02.5", "BF.02.6", "BF.02.7", "BF.02.8", "BF.02.9",
        "BF.02.10", "BF.02.11",
        "BR.12", "BR.12.1", "BR.12.2", "BR.12.3", "BR.12.4", "BR.12.5", "BR.12.6",
        "BR-27", "BR-28", "BR-29", "BR-30", "BR-31", "BR-32", "BR-33",
        "BD-06", "BD-11",
        "M10",
    ],
    "KDS сборки / Упаковка": [
        "F-06",
        "BF.02.12", "BF.02.13", "BF.02.14", "BF.02.15",
        "BR.13", "BR.13.1", "BR.13.2", "BR.13.3", "BR.13.4", "BR.13.5", "BR.13.6",
        "BR-34", "BR-35", "BR-36", "BR-37",
        "BD-08", "BD-10",
        "M11",
    ],
    "Схемы станций / Брак / Маркировка": [
        "BF.02.16", "BF.02.17", "BF.02.18", "BF.02.19",
        "BR.14", "BR.14.1", "BR.14.2",
        "BR.18", "BR.18.1", "BR.19",
        "BR-38", "BR-39", "BR-40", "BR-44", "BR-45",
        "BD-09", "BD-10",
        "M13",
    ],
    "Обещанное время / ETA": [
        "F-08",
        "BF.03.1", "BF.03.2", "BF.03.3",
        "BR.3", "BR.3.1", "BR.3.2",
        "BR-03", "BR-04", "BR-21",
        "BD-14",
    ],
    "Маршрутизация / Назначение курьеров": [
        "BP-05", "F-09",
        "BF.03.4", "BF.03.5", "BF.03.6", "BF.03.7", "BF.03.8", "BF.03.9", "BF.03.10",
        "BR.4", "BR.4.1", "BR.4.2", "BR.4.3", "BR.4.4", "BR.4.5", "BR.4.6", "BR.4.7",
        "BR-05", "BR-06", "BR-08", "BR-09", "BR-24", "BR-25", "BR-26",
        "BD-12",
        "M12",
    ],
    "Доставка / Курьерская служба": [
        "BP-06", "BP-10", "F-10", "F-11", "F-12",
        "BF.04.1", "BF.04.2", "BF.04.3", "BF.04.4", "BF.04.5", "BF.04.6",
        "BR.5", "BR.5.1",
        "BR.15", "BR.15.1", "BR.15.2", "BR.16",
        "BR-07", "BR-41", "BR-42",
        "M06", "M07",
    ],
    "Оплата / Фискализация / Чаевые": [
        "BP-07", "F-13",
        "BF.05.1", "BF.05.2", "BF.05.3",
        "BR.9", "BR.9.1", "BR.9.2",
        "M17",
    ],
    "Меню / Стоп-листы / Конфигурация": [
        "BP-08", "F-15",
        "BF.06.4", "BF.06.5", "BF.06.6",
        "BR.8", "BR.8.1",
        "M01", "M13",
    ],
    "Профиль гостя / CRM / Лояльность": [
        "BP-09", "F-14",
        "BF.06.1", "BF.06.2", "BF.06.3",
        "BR-20",
        "M04", "M14",
    ],
    "Персонал / Смены / Графики": [
        "BP-11", "F-16",
        "BF.07.1", "BF.07.2", "BF.07.3",
        "BR.10", "BR.10.1",
        "BR-43",
        "BD-15",
        "M15",
    ],
    "Мониторинг / Аналитика / Дашборд": [
        "BP-12", "BP-13", "F-17",
        "BF.08.1", "BF.08.2", "BF.08.3", "BF.08.4",
        "M16",
    ],
    "Управление партнёрами / Франшиза": [
        "BP-14", "F-19",
        "BF.09.1", "BF.09.2", "BF.09.3",
        "BR.11", "BR.11.1",
    ],
    "Обратная связь / NPS / Отзывы": [
        "BP-15", "F-18", "F-20",
        "BF.10.1", "BF.10.2", "BF.10.3",
    ],
    "Мобильное приложение (B2C)": [
        "M01", "M02", "M03", "M04", "M05",
    ],
}


def export_index(G: nx.DiGraph, registry: Dict[str, Artifact], root: Path, path: Path):
    """Generate compact ARTIFACT_INDEX.md for agent navigation.

    Design: minimal token footprint, agent-grep-friendly.
    - Semantic map: topic → IDs (for business-term lookup)
    - Flat lookup: ID  file:line  title (for direct navigation)
    - No link columns (agent can use --json-out or script for links)
    """
    lines = [
        "# Artifact Index",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d')}. "
        "Rebuild: `uv run scripts/traceability.py --index-auto`",
        "",
        "## Semantic Map",
        "",
    ]

    # Semantic map — compact
    for topic, ids in SEMANTIC_CLUSTERS.items():
        existing = [i for i in ids if normalize_id(i) in registry]
        if existing:
            lines.append(f"**{topic}:** {', '.join(existing)}")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Per-type: flat list, grouped by common directory prefix
    for atype in ArtifactType:
        arts = sorted(
            [(aid, art) for aid, art in registry.items() if art.artifact_type == atype],
            key=lambda x: x[0],
        )
        if not arts:
            continue

        lines.append(f"## {LABELS[atype]} ({len(arts)})")
        lines.append("")

        # Group by directory to reduce repetition
        dir_groups: Dict[str, List[Tuple[str, Artifact]]] = defaultdict(list)
        for aid, art in arts:
            d = str(art.source_file.parent.relative_to(root)) if root in art.source_file.parents else str(art.source_file.parent)
            dir_groups[d].append((aid, art))

        for dirpath, group in dir_groups.items():
            if len(dir_groups) > 1:
                lines.append(f"_{dirpath}/_")
            for aid, art in group:
                fname = art.source_file.name
                title = art.title[:55] if art.title else ""
                if title:
                    lines.append(f"- `{aid}` {fname}:{art.line_number} — {title}")
                else:
                    lines.append(f"- `{aid}` {fname}:{art.line_number}")

        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Index exported to {path}")


def _relpath(filepath: Path, root: Path) -> str:
    try:
        return str(filepath.relative_to(root))
    except ValueError:
        return str(filepath)


def export_dot(G: nx.DiGraph, path: Path):
    """Export graph as a richly styled DOT file with clusters, shapes, and legend."""
    # Group nodes by type
    type_groups: Dict[str, List[str]] = defaultdict(list)
    for n, d in G.nodes(data=True):
        atype = d.get("type", "UNKNOWN")
        type_groups[atype].append(n)

    lines: List[str] = []
    lines.append('digraph traceability {')
    lines.append('  // Global settings')
    lines.append('  rankdir=TB;')
    lines.append('  newrank=true;')
    lines.append('  concentrate=true;')
    lines.append('  compound=true;')
    lines.append('  splines=ortho;')
    lines.append('  nodesep=0.4;')
    lines.append('  ranksep=0.8;')
    lines.append('  fontname="Helvetica";')
    lines.append('  bgcolor="#FAFAFA";')
    lines.append('  pad=0.5;')
    lines.append('')
    lines.append('  // Default node style')
    lines.append('  node [style="filled,rounded", fontname="Helvetica", fontsize=10, '
                 'penwidth=1.5, margin="0.15,0.08"];')
    lines.append('  edge [fontname="Helvetica", fontsize=8, penwidth=0.8, '
                 'arrowsize=0.7, color="#718096"];')
    lines.append('')

    # Emit clustered subgraphs in hierarchy order
    for atype in DOT_RANK_ORDER:
        nodes = type_groups.get(atype, [])
        if not nodes:
            continue
        cluster_label = DOT_CLUSTER_LABELS.get(atype, atype)
        fill = DOT_COLORS.get(atype, "#FFFFFF")
        border = DOT_BORDER_COLORS.get(atype, "#999999")
        shape = DOT_SHAPES.get(atype, "box")
        # Sort nodes for deterministic output
        nodes.sort()

        lines.append(f'  subgraph cluster_{atype} {{')
        lines.append(f'    label="{cluster_label}";')
        lines.append(f'    style="rounded,filled"; fillcolor="{fill}20"; '
                     f'color="{border}"; penwidth=1.5;')
        lines.append(f'    fontname="Helvetica"; fontsize=12; fontcolor="{border}"; labeljust=l;')
        lines.append('')
        for n in nodes:
            d = G.nodes[n]
            label = _dot_node_label(n, d)
            defined = d.get("defined", True)
            node_style = "filled,rounded,dashed" if not defined else "filled,rounded"
            lines.append(
                f'    "{_esc(n)}" [label="{label}", shape={shape}, '
                f'fillcolor="{fill}", color="{border}", style="{node_style}"];'
            )
        lines.append('  }')
        lines.append('')

    # FILE and UNKNOWN nodes (outside clusters)
    for atype in ("FILE", "UNKNOWN"):
        nodes = type_groups.get(atype, [])
        if not nodes:
            continue
        cluster_label = DOT_CLUSTER_LABELS.get(atype, atype)
        fill = DOT_COLORS.get(atype, "#FFFFFF")
        border = DOT_BORDER_COLORS.get(atype, "#999999")
        shape = DOT_SHAPES.get(atype, "box")
        nodes.sort()

        lines.append(f'  subgraph cluster_{atype} {{')
        lines.append(f'    label="{cluster_label}";')
        lines.append(f'    style="rounded,dashed"; color="{border}"; penwidth=1.0;')
        lines.append(f'    fontname="Helvetica"; fontsize=10; fontcolor="{border}";')
        lines.append('')
        for n in nodes:
            d = G.nodes[n]
            label = _dot_node_label(n, d)
            lines.append(
                f'    "{_esc(n)}" [label="{label}", shape={shape}, '
                f'fillcolor="{fill}", color="{border}"];'
            )
        lines.append('  }')
        lines.append('')

    # Edges — colored by source node type
    lines.append('  // Edges')
    for u, v, d in G.edges(data=True):
        src_type = G.nodes[u].get("type", "UNKNOWN") if u in G.nodes else "UNKNOWN"
        edge_color = DOT_EDGE_COLORS.get(src_type, "#718096")
        ctx = d.get("context", "").replace('"', '\\"')[:60]
        lines.append(
            f'  "{_esc(u)}" -> "{_esc(v)}" '
            f'[color="{edge_color}80", tooltip="{ctx}"];'
        )
    lines.append('')

    # Legend
    lines.append('  // Legend')
    lines.append('  subgraph cluster_legend {')
    lines.append('    label="Легенда";')
    lines.append('    style="rounded,filled"; fillcolor="#FFFFFF"; color="#CBD5E0"; penwidth=1.5;')
    lines.append('    fontname="Helvetica"; fontsize=12; fontcolor="#2D3748"; labeljust=l;')
    lines.append('    node [fontsize=9, width=0.3, height=0.2, margin="0.08,0.04"];')
    lines.append('')
    for atype in DOT_RANK_ORDER:
        lbl = DOT_CLUSTER_LABELS.get(atype, atype)
        fill = DOT_COLORS.get(atype, "#FFFFFF")
        border = DOT_BORDER_COLORS.get(atype, "#999999")
        shape = DOT_SHAPES.get(atype, "box")
        lines.append(
            f'    "legend_{atype}" [label="{lbl}", shape={shape}, '
            f'fillcolor="{fill}", color="{border}"];'
        )
    # Invisible edges to order legend items vertically
    legend_ids = [f'"legend_{a}"' for a in DOT_RANK_ORDER]
    lines.append(f'    {" -> ".join(legend_ids)} [style=invis];')
    lines.append('  }')
    lines.append('')

    lines.append('}')

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"DOT exported to {path}")


def _esc(s: str) -> str:
    """Escape a string for DOT identifiers."""
    return s.replace('"', '\\"')


def _dot_node_label(node_id: str, data: dict) -> str:
    """Build a compact two-line label: ID + truncated title."""
    eid = node_id.replace('"', '\\"')
    title = data.get("title", "")
    if not title:
        return eid
    # Truncate to 35 chars for readability
    short = title[:35].replace('"', '\\"')
    if len(title) > 35:
        short += "…"
    return f"{eid}\\n{short}"


# ── Output: Interactive HTML (vis-network) ───────────────────────

HTML_COLORS = {
    "ST":      {"bg": "#FFF3C4", "border": "#D69E2E"},
    "BR_REQ":  {"bg": "#C3DAF9", "border": "#2B6CB0"},
    "BR_RULE": {"bg": "#FCCFCF", "border": "#C53030"},
    "BP":      {"bg": "#C6F6D5", "border": "#276749"},
    "BD":      {"bg": "#FEEBC8", "border": "#C05621"},
    "BF":      {"bg": "#E9D8FD", "border": "#6B46C1"},
    "F":       {"bg": "#B2F5EA", "border": "#285E61"},
    "VAD":     {"bg": "#FED7E2", "border": "#B83280"},
    "M":       {"bg": "#E2E8F0", "border": "#4A5568"},
    "FILE":    {"bg": "#F7FAFC", "border": "#A0AEC0"},
    "UNKNOWN": {"bg": "#FED7D7", "border": "#E53E3E"},
}

HTML_SHAPES = {
    "ST": "triangle", "BR_REQ": "box", "BR_RULE": "diamond",
    "BP": "ellipse", "BD": "hexagon", "BF": "box",
    "F": "star", "VAD": "hexagon", "M": "database",
    "FILE": "text", "UNKNOWN": "dot",
}

HTML_SIZES = {
    "ST": 25, "BR_REQ": 18, "BR_RULE": 16, "BP": 22, "BD": 20,
    "BF": 14, "F": 24, "VAD": 22, "M": 26, "FILE": 10, "UNKNOWN": 10,
}

HTML_GROUP_LABELS = {
    "ST": "Стейкхолдеры", "BR_REQ": "Бизнес-требования",
    "BR_RULE": "Бизнес-правила", "BP": "Бизнес-процессы",
    "BD": "Решения", "BF": "Бизнес-функции", "F": "Фичи",
    "VAD": "Цепочки ценности", "M": "Модули",
    "FILE": "Документы", "UNKNOWN": "Прочее",
}


def export_html(G: nx.DiGraph, path: Path):
    """Export a standalone interactive HTML visualization (vis-network via CDN)."""
    nodes_js: List[str] = []
    for n, d in G.nodes(data=True):
        atype = d.get("type", "UNKNOWN")
        title_text = d.get("title", "").replace("'", "\\'").replace("\n", " ")
        source_file = d.get("source_file", "")
        defined = d.get("defined", True)

        colors = HTML_COLORS.get(atype, HTML_COLORS["UNKNOWN"])
        shape = HTML_SHAPES.get(atype, "dot")
        size = HTML_SIZES.get(atype, 15)
        group = HTML_GROUP_LABELS.get(atype, "Прочее")

        in_deg = G.in_degree(n)
        out_deg = G.out_degree(n)
        tip_parts = [f"<b>{n}</b>"]
        if title_text:
            tip_parts.append(title_text)
        tip_parts.append(f"<i>Тип:</i> {group}")
        if source_file:
            tip_parts.append(f"<i>Файл:</i> {source_file}")
        tip_parts.append(f"<i>Входящие:</i> {in_deg} | <i>Исходящие:</i> {out_deg}")
        if not defined:
            tip_parts.append("<b style='color:red'>⚠ Не определён</b>")
        tooltip = "<br>".join(tip_parts).replace("'", "\\'")

        bg = colors["bg"] if defined else "#FED7D7"
        border = colors["border"] if defined else "#E53E3E"
        esc_n = n.replace("'", "\\'")

        nodes_js.append(
            f"  {{id:'{esc_n}',label:'{esc_n}',title:'{tooltip}',"
            f"shape:'{shape}',size:{size},group:'{atype}',"
            f"color:{{background:'{bg}',border:'{border}',"
            f"highlight:{{background:'{bg}',border:'{border}'}}}}}}"
        )

    edges_js: List[str] = []
    for u, v, d in G.edges(data=True):
        ctx = d.get("context", "").replace("'", "\\'")[:60]
        src_file = d.get("source_file", "").replace("'", "\\'")
        tip = f"{u} → {v}"
        if ctx:
            tip += f"\\n{ctx}"
        if src_file:
            tip += f"\\n({src_file})"
        tip = tip.replace("'", "\\'")
        eu = u.replace("'", "\\'")
        ev = v.replace("'", "\\'")
        edges_js.append(f"  {{from:'{eu}',to:'{ev}',title:'{tip}'}}")

    # Legend items
    legend_items_html = []
    for atype in DOT_RANK_ORDER:
        c = HTML_COLORS.get(atype, HTML_COLORS["UNKNOWN"])
        lbl = HTML_GROUP_LABELS.get(atype, atype)
        legend_items_html.append(
            f'<label style="display:flex;align-items:center;gap:6px;margin:2px 0;cursor:pointer">'
            f'<input type="checkbox" checked data-group="{atype}" '
            f'onchange="toggleGroup(this)">'
            f'<span style="display:inline-block;width:14px;height:14px;'
            f'background:{c["bg"]};border:2px solid {c["border"]};border-radius:3px"></span>'
            f'<span style="font-size:11px;color:#4A5568">{lbl}</span></label>'
        )

    html = _HTML_TEMPLATE.format(
        nodes_data=",\n".join(nodes_js),
        edges_data=",\n".join(edges_js),
        legend_items="\n".join(legend_items_html),
        node_count=G.number_of_nodes(),
        edge_count=G.number_of_edges(),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    print(f"HTML exported to {path}")


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Graph BA — Граф трассируемости артефактов</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/dist/vis-network.min.css" crossorigin="anonymous"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.js" crossorigin="anonymous"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: Helvetica, Arial, sans-serif; background: #FAFAFA; overflow: hidden; }}
  #graph {{ width:100vw; height:100vh; }}
  #legend {{
    position:fixed; top:12px; left:12px; z-index:9999;
    background:white; border:1px solid #CBD5E0; border-radius:8px;
    padding:10px 14px; box-shadow:0 2px 8px rgba(0,0,0,0.12);
    max-width:220px; max-height:90vh; overflow-y:auto;
  }}
  #legend h3 {{ font-size:13px; color:#2D3748; margin-bottom:6px; }}
  #stats {{
    position:fixed; bottom:12px; left:12px; z-index:9999;
    background:white; border:1px solid #CBD5E0; border-radius:8px;
    padding:8px 12px; box-shadow:0 2px 8px rgba(0,0,0,0.08);
    font-size:11px; color:#4A5568;
  }}
  #search {{
    position:fixed; top:12px; right:12px; z-index:9999;
    background:white; border:1px solid #CBD5E0; border-radius:8px;
    padding:8px 12px; box-shadow:0 2px 8px rgba(0,0,0,0.08);
  }}
  #search input {{
    border:1px solid #CBD5E0; border-radius:4px; padding:5px 8px;
    font-size:12px; width:200px; outline:none;
  }}
  #search input:focus {{ border-color:#4299E1; }}
  #stabilize-msg {{
    position:fixed; top:50%; left:50%; transform:translate(-50%,-50%);
    background:rgba(45,55,72,0.85); color:white; padding:16px 28px;
    border-radius:10px; font-size:14px; z-index:99999;
    transition: opacity 0.5s;
  }}
</style>
</head>
<body>
<div id="graph"></div>

<div id="legend">
  <h3>Легенда (клик — фильтр)</h3>
  {legend_items}
</div>

<div id="search">
  <input type="text" id="searchBox" placeholder="Поиск по ID..." oninput="onSearch(this.value)">
</div>

<div id="stats">Узлов: {node_count} | Рёбер: {edge_count}</div>

<div id="stabilize-msg">Стабилизация графа…</div>

<script>
var allNodes = new vis.DataSet([
{nodes_data}
]);
var allEdges = new vis.DataSet([
{edges_data}
]);

var container = document.getElementById('graph');
var data = {{ nodes: allNodes, edges: allEdges }};
var options = {{
  physics: {{
    enabled: true,
    barnesHut: {{
      gravitationalConstant: -6000,
      centralGravity: 0.25,
      springLength: 140,
      springConstant: 0.04,
      damping: 0.09,
      avoidOverlap: 0.4
    }},
    stabilization: {{ enabled: true, iterations: 500, updateInterval: 25 }}
  }},
  interaction: {{
    hover: true, tooltipDelay: 100,
    navigationButtons: true, keyboard: {{ enabled: true }}
  }},
  edges: {{
    arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }},
    smooth: {{ type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.4 }},
    color: {{ inherit: 'from', opacity: 0.35 }},
    width: 0.7, hoverWidth: 2
  }},
  nodes: {{
    font: {{ size: 11, face: 'Helvetica, Arial, sans-serif' }},
    borderWidth: 2, borderWidthSelected: 3
  }}
}};

var network = new vis.Network(container, data, options);

network.on('stabilizationIterationsDone', function() {{
  network.setOptions({{ physics: {{ enabled: false }} }});
  document.getElementById('stabilize-msg').style.opacity = '0';
  setTimeout(function() {{
    document.getElementById('stabilize-msg').style.display = 'none';
  }}, 600);
}});

// Search
function onSearch(q) {{
  q = q.trim().toUpperCase();
  if (!q) {{
    allNodes.forEach(function(n) {{ allNodes.update({{id:n.id, opacity:1, font:{{size:11}}}}); }});
    allEdges.forEach(function(e) {{ allEdges.update({{id:e.id, hidden:false}}); }});
    return;
  }}
  allNodes.forEach(function(n) {{
    var match = n.id.toUpperCase().indexOf(q) >= 0;
    allNodes.update({{id:n.id, opacity: match ? 1 : 0.15, font:{{size: match ? 14 : 8}}}});
  }});
}}

// Filter by group (legend checkboxes)
var hiddenGroups = {{}};
function toggleGroup(cb) {{
  var g = cb.dataset.group;
  if (cb.checked) {{ delete hiddenGroups[g]; }} else {{ hiddenGroups[g] = true; }}
  allNodes.forEach(function(n) {{
    var hidden = !!hiddenGroups[n.group];
    allNodes.update({{id:n.id, hidden:hidden}});
  }});
}}
</script>
</body>
</html>
"""


# ── CLI ───────────────────────────────────────────────────────────

@click.command()
@click.option("--root", type=click.Path(exists=True, path_type=Path),
              default=".", help="Project root directory")
@click.option("--json-out", type=click.Path(path_type=Path), default=None,
              help="Path for JSON graph export")
@click.option("--dot-out", type=click.Path(path_type=Path), default=None,
              help="Path for DOT file export")
@click.option("--html-out", type=click.Path(path_type=Path), default=None,
              help="Path for interactive HTML export (vis-network)")
@click.option("--no-file-nodes", is_flag=True,
              help="Exclude FILE nodes from HTML/DOT (reduce noise)")
@click.option("--no-transitive", is_flag=True,
              help="Remove transitive edges A→C where A→B→C exists")
@click.option("--index", "index_out", type=click.Path(path_type=Path), default=None,
              help="Path for ARTIFACT_INDEX.md (default: 02_Discovery/ARTIFACT_INDEX.md)")
@click.option("--index-auto", is_flag=True,
              help="Generate index at default path 02_Discovery/ARTIFACT_INDEX.md")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed info")
def main(root: Path, json_out: Optional[Path], dot_out: Optional[Path],
         html_out: Optional[Path],
         no_file_nodes: bool, no_transitive: bool,
         index_out: Optional[Path], index_auto: bool, verbose: bool):
    """Traceability Scanner — parse BA artifacts and verify cross-references."""
    root = root.resolve()

    # Phase 1: Definitions
    registry = scan_definitions(root)
    if verbose:
        print(f"[scan] {len(registry)} artifact definitions found")

    # Phase 2: References
    references = scan_references(root, registry)
    index_xrefs = scan_index_cross_refs(root)
    if verbose:
        print(f"[scan] {len(references)} references found, {len(index_xrefs)} index cross-refs")

    # Phase 3: Graph
    G = build_graph(registry, references, index_xrefs)

    # Phase 4: Verify (always on full graph)
    report = verify(G, registry, references)

    # Output
    print_report(report, registry, verbose)

    if json_out:
        export_json(G, registry, report, json_out)

    # Build filtered graph for visual exports
    G_vis = _filter_graph(G, no_file_nodes, no_transitive, verbose)

    if dot_out:
        export_dot(G_vis, dot_out)
    if html_out:
        export_html(G_vis, html_out)

    # Index generation
    idx_path = index_out or (root / "02_Discovery" / "ARTIFACT_INDEX.md" if index_auto else None)
    if idx_path:
        export_index(G, registry, root, idx_path)


def _filter_graph(G: nx.DiGraph, no_file_nodes: bool, no_transitive: bool,
                  verbose: bool) -> nx.DiGraph:
    """Return a filtered copy of G for visualization."""
    if not no_file_nodes and not no_transitive:
        return G

    H = G.copy()
    removed_nodes = 0
    removed_edges = 0

    if no_file_nodes:
        file_nodes = [n for n, d in H.nodes(data=True) if d.get("type") == "FILE"]
        H.remove_nodes_from(file_nodes)
        removed_nodes = len(file_nodes)

    if no_transitive:
        to_remove = []
        for u, v in list(H.edges()):
            # Check if there's an alternative path u→…→v of length ≥ 2
            H.remove_edge(u, v)
            if nx.has_path(H, u, v):
                to_remove.append((u, v))
            else:
                H.add_edge(u, v, **G.edges[u, v])
        for u, v in to_remove:
            if H.has_edge(u, v):
                H.remove_edge(u, v)
        removed_edges = len(to_remove)

    if verbose or (removed_nodes or removed_edges):
        orig_n, orig_e = G.number_of_nodes(), G.number_of_edges()
        new_n, new_e = H.number_of_nodes(), H.number_of_edges()
        print(f"\n[filter] {orig_n} → {new_n} nodes, {orig_e} → {new_e} edges"
              f" (removed {removed_nodes} FILE nodes, {removed_edges} transitive edges)")

    return H


if __name__ == "__main__":
    main()

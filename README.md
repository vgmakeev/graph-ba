# Graph BA

Traceability graph for business analysis artifacts. Scans markdown files and source code, builds a cross-reference graph in SQLite, and provides CLI for search, validation, and linting.

## Why

BA projects have hundreds of interlinked markdown documents. Cross-references between them break silently — renamed IDs, missing links, conflicting numbers, stale content. Manual checking doesn't scale.

Graph BA turns your documents into a queryable graph and lints them automatically. Artifact types and ID patterns are defined in a TOML config — the tool works with any naming convention.

## What it does

```
$ graph-ba import
Imported: 356 artifacts, 2059 edges, 16 semantic clusters

$ graph-ba lint
── Incompleteness markers (24) ──
  [WARN]  BP-08   ...md:80   TODO: manual stop-list ...
── Empty sections (29) ──
  [WARN]  BP-02   ...md:112  empty section "Exceptions"
── Terminology vs glossary (81) ──
  [INFO]  BP-09   ...md:22   "Courier" → canonical "Курьер"
Lint: 134 WARN, 81 INFO

$ graph-ba audit
── Issues (47) ──
  DANGLING (3), COVERAGE_GAP (8), MISSING_BIDIR (12) ...
── Review Candidates (15) ──
  HIGH  REQ-99  DANGLING
  HIGH  F-01    BRIDGE, CYCLE

$ graph-ba review F-01 --semantic
  REVIEW: F-01 — Order Management
  ⚠ [GAP] No links to type: BR (business rules)
  ── LINKED ARTIFACTS (8) ──
  → REQ-01 — Must manage orders ...
```

## Install

Python 3.11+.

```bash
uvx --from git+https://github.com/vgmakeev/graph-ba graph-ba --help
# or
uv tool install git+https://github.com/vgmakeev/graph-ba
```

## Quick start

```bash
graph-ba init          # create graph-ba.toml template
# edit graph-ba.toml — define your artifact types and scan rules
graph-ba import        # scan docs → build graph
graph-ba lint          # content quality: TODOs, empty sections, terminology, staleness
graph-ba audit         # structure quality: dangling refs, cycles, coverage gaps
```

## Commands

| Command | What it does |
|---|---|
| `import` | Scan artifacts and build SQLite DB |
| **`lint [ID]`** | Content lint: TODO markers, empty sections, terminology, staleness, code coverage |
| **`audit`** | Structural audit: dangling refs, cycles, coverage gaps, bottlenecks |
| `review <ID> --semantic` | Full text of all linked artifacts for deep validation |
| `search <query>` | FTS5 full-text search |
| `node <ID>` | Node details + neighbors |
| `path <from> <to>` | Shortest path between artifacts |
| `impact <ID>` | Cascade analysis |
| `coverage` | Cross-layer coverage matrix |
| `code-refs` | Code → artifact links (`@trace` comments) |
| `sql <query>` | Raw SQL |

All commands: `--json` for machine output, `--root`/`--db` for paths.

## Configuration

Everything is config-driven via `graph-ba.toml`. Define your own artifact types, ID patterns, scan rules, and validation expectations. The tool doesn't assume any specific naming convention.

```toml
[scan]
dirs = ["docs"]

# Define artifact types with regex patterns
[types.REQ]
label = "Requirements"
ref = '(?<![A-Za-z])(REQ-\d{2,4})(?!\d)'
classify = 'REQ-\d{2,4}'

# Where artifacts are defined (heading or table)
[[definitions]]
type = "REQ"
file = "docs/requirements.md"       # supports globs
mode = "table"                       # or "heading"
pattern = '^\|\s*(REQ-\d{2,4})\s*\|'

# Expected coverage between layers
[[coverage]]
source = "FEAT"
target = "REQ"
label = "FEAT → REQ"

# Validation rules
[review]
required_sections = { "FEAT" = ["Goal", "Scope"] }
expected_bidir = { "FEAT" = ["REQ"] }

# Code traceability (// @trace: F-01, REQ-01)
[code]
dirs = ["src"]
coverage_types = ["FEAT", "REQ"]

# Content linting
[lint]
glossary_file = "docs/glossary.md"
meetings_dir = "inputs/meetings_refined"
stale_threshold_days = 30
todo_patterns = ["TODO", "TBD", "FIXME", "???"]

# Semantic clusters (for grouping)
[clusters]
"Order Management" = ["REQ-01", "F-01", "BP-01"]

# ID normalization
[normalize]
char_map = { "М" = "M" }
```

Run `graph-ba init` for a full template with comments.

## Tests

```bash
uv run pytest tests/ -v    # 170 tests
```

## License

MIT

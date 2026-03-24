# Graph BA

Config-driven traceability graph for business analysis artifacts. Scans markdown documents, builds a cross-reference graph in SQLite, and provides CLI commands for navigation, validation, and anomaly detection.

## The problem

A typical BA project has hundreds of markdown documents: requirements, features, business rules, processes, domain models. They reference each other — `REQ-01` mentions `F-01`, `BP-03` references `BR.2`, and so on.

Over time, these cross-references break silently:
- Someone renames `REQ-05` to `REQ-5` and 12 links go dangling
- A new feature `F-08` has no requirements linked — nobody notices for weeks
- `BP-03` says "delivery within 30 min", but `BR.7` says "45 min" — the conflict hides across two files
- Requirement `REQ-12` becomes a bottleneck with 40 inbound links — a change there cascades everywhere

Manual cross-checking doesn't scale past ~50 documents. You need a graph.

## What it does

Graph BA scans your markdown files, extracts artifact definitions and cross-references using regex patterns from a TOML config, and builds a queryable graph in SQLite.

```
$ graph-ba import
Imported: 214 artifacts, 847 edges, 12 semantic clusters

$ graph-ba anomalies
Graph anomalies (214 nodes, 847 edges):

── CYCLE ──
  2 cycle(s) found
    Cycle: BP-01 → F-01 → REQ-01 → REQ-02
    Cycle: BR.3 → F-05 → REQ-11
── ROOT ──
  3 root node(s) (no incoming edges)
    [FEAT] (1): F-08
    [REQ] (2): REQ-03, REQ-12
── BRIDGE ──
  2 bridge edge(s) (critical connections)
    F-01 — REQ-02
    BP-05 — BR.9

$ graph-ba coverage
Cross-layer coverage matrix:

  FEAT     ↔ REQ         8/10   ████████████████░░░░   80.0%  [WARN]
  REQ      ↔ BP         12/15   ████████████████░░░░   80.0%  [WARN]
  REQ      ↔ BR         11/15   ██████████████░░░░░░   73.3%  [WARN]

$ graph-ba impact REQ-01
Каскадное влияние REQ-01: 8 артефактов
  [BP] (2): BP-01, BP-03
  [BR] (3): BR.1, BR.2, BR.5
  [FEAT] (3): F-01, F-02, F-04
```

The main use case is **semantic review** — gathering the full text of all linked artifacts to check for completeness, contradictions, and missing links:

```
$ graph-ba review F-01 --semantic --lines 20
══════════════════════════════════════════════════════════════════════
  REVIEW: F-01 — Order Management
  Тип: FEAT  |  Файл: features.md:3
══════════════════════════════════════════════════════════════════════

⚠ [STRUCT] Missing section: Scope
⚠ [GAP] No links to type: BR (business rules)

── СВЯЗАННЫЕ АРТЕФАКТЫ ──

  → [REQ] REQ-01 — Must manage orders
    ...first 20 lines of REQ-01 content...

  ← [BR] BR.1 — Base Pricing
    ...first 20 lines of BR.1 content...
```

This output is designed for both human reading and [Claude Code](https://claude.ai/claude-code) agent pipelines (`--json` flag returns structured JSON).

## Install

Requires Python 3.11+.

```bash
# Run directly with uvx (no install needed)
uvx --from git+https://github.com/vgmakeev/graph-ba graph-ba --help

# Or install as a tool
uv tool install git+https://github.com/vgmakeev/graph-ba

# Or add to a project
uv add --dev git+https://github.com/vgmakeev/graph-ba
```

## Quick start

```bash
# 1. Create a config in your project root
graph-ba init

# 2. Edit graph-ba.toml — define artifact types, scan rules, validation

# 3. Scan documents and build the graph
graph-ba import

# 4. Semantic review of an artifact (the main use case)
graph-ba review F-01 --semantic --lines 20

# 5. Explore
graph-ba search "delivery"
graph-ba anomalies
graph-ba impact REQ-01
```

## Commands

| Command | Description |
|---|---|
| `import` | Scan artifacts and populate the SQLite DB |
| `init` | Create a template `graph-ba.toml` |
| `search <query>` | FTS5 full-text search across titles and IDs |
| `node <id>` | Show node details and immediate neighbors |
| `path <from> <to>` | Shortest path between two artifacts |
| `impact <id>` | Cascade analysis — what does changing this affect? |
| `review <id>` | Validate structure + show context from linked artifacts |
| `review <id> --semantic` | Full text of linked artifacts (default 20 lines each) |
| `review <id> --semantic --lines 0` | Full text with no line limit per artifact |
| `anomalies` | Detect islands, cycles, bridges, bottlenecks, dangling refs |
| `coverage` | Cross-layer coverage matrix |
| `audit` | Global audit: anomalies + coverage + prioritized review candidates |
| `sql <query>` | Raw SQL against the DB |

Global options: `--root <path>` (project root, default `.`), `--db <path>` (SQLite DB path), `--json` (output as JSON for programmatic use).

## Configuration

All artifact types, scan rules, and validation expectations are defined in `graph-ba.toml` at the project root. Run `graph-ba init` to generate a template.

### Step 1: Define artifact types (`[types.*]`)

Each artifact type needs a regex for finding references in text and a regex for classifying ID strings. This is the foundation — graph-ba uses these patterns to discover cross-references automatically.

```toml
# Simple numeric IDs: REQ-01, REQ-123
[types.REQ]
label = "Requirements"
ref = '(?<![A-Za-z])(REQ-\d{2,4})(?!\d)'     # regex for finding references (group 1 = full ID)
classify = 'REQ-\d{2,4}'                       # regex for classifying an ID string (fullmatch)

# Dotted IDs: BR.1, BR.2.1
[types.BR]
label = "Business Rules"
ref = '(?<![A-Za-z.])(BR\.\d+(?:\.\d+)?)(?!\d)'
classify = 'BR\.\d+(?:\.\d+)?'

# Prefixed with letters: FEAT-01, BP-03
[types.FEAT]
label = "Features"
ref = '(?<![A-Za-z])(FEAT-\d{2,4})(?!\d)'
classify = 'FEAT-\d{2,4}'

# Restrict pattern to specific dirs (avoids false matches)
[types.M]
label = "Modules"
ref = '(?<![A-Za-z])(M\d{2})(?!\d)'
classify = 'M\d{2}'
restrict_to = ["docs/modules"]
```

**How references work:** When graph-ba scans a markdown file and finds text like `"see REQ-01 and BR.2"`, it matches these against the `ref` patterns and creates edges from the current artifact to `REQ-01` and `BR.2`.

### Step 2: Define where artifacts are declared (`[[definitions]]`)

Tell graph-ba where each artifact is **defined** — in a table row or a heading:

```toml
# Table mode: artifacts defined as rows in a markdown table
# | REQ-01 | User authentication | Must support SSO |
[[definitions]]
type = "REQ"
file = "docs/requirements.md"                  # supports glob: "docs/reqs/REQ-*.md"
mode = "table"
pattern = '^\|\s*(REQ-\d{2,4})\s*\|'           # group 1 = ID, group 2 = title (optional)

# Heading mode: artifacts defined as markdown headings
# ## FEAT-01 — Online Ordering
[[definitions]]
type = "FEAT"
file = "docs/features/*.md"                    # glob pattern
mode = "heading"
pattern = '^##\s+(FEAT-\d{2,4})\s*[—–\-]\s*(.*)'  # group 1 = ID, group 2 = title
```

### Step 3: Configure cross-reference extraction (`[[index_tables]]`)

For tables where each row contains an artifact ID and references to other artifacts:

```toml
# Extracts cross-refs from rows like: | FEAT-01 | REQ-01, REQ-02 | BR.1 |
[[index_tables]]
file = "docs/traceability-matrix.md"
first_col = '^\|\s*(FEAT-\d{2,4})\s*\|'       # regex for the source ID in column 1
```

### Step 4: Set validation rules (`[review]`, `[[coverage]]`)

These rules are used by `graph-ba review --semantic` and `graph-ba coverage`:

```toml
# Expected cross-layer links (for coverage matrix)
[[coverage]]
source = "FEAT"
target = "REQ"
label = "FEAT → REQ"

[[coverage]]
source = "REQ"
target = "BR"
label = "REQ → BR"

# Validation rules for review
[review]
# Required sections per artifact type
required_sections = { "FEAT" = ["Goal", "Scope", "Acceptance Criteria"] }
# Which link types should be bidirectional
expected_bidir = { "FEAT" = ["REQ", "BR"] }

# Expected cross-layer links for review validation
# [[review.expected_cross_layer.FEAT]]
# type = "REQ"
# label = "requirements"
```

### Other sections

**`[scan]`** — directories to scan:
```toml
[scan]
dirs = ["docs", "specs"]
```

**`[clusters]`** — semantic grouping:
```toml
[clusters]
"Order Management" = ["REQ-01", "REQ-02", "FEAT-01", "BR.5"]
```

**`[normalize]`** — ID normalization:
```toml
[normalize]
char_map = { "М" = "M" }  # Cyrillic → Latin
zero_pad = [{ pattern = 'M(\d{1,2})', format = "M{:02d}" }]
```

## How it works

1. **Scan definitions** — reads markdown files, finds artifact definitions (headings or table rows) using regex patterns from config
2. **Scan references** — finds cross-references to known artifact IDs in all markdown files
3. **Build graph** — constructs a NetworkX directed graph with file:line attribution on every edge
4. **Import to SQLite** — stores the graph in SQLite with FTS5 indexes for fast search
5. **Query & validate** — CLI commands query the DB for navigation, coverage analysis, and anomaly detection

## Architecture

```
graph_ba/
├── config.py         — loads and validates graph-ba.toml
├── traceability.py   — scanner, graph builder, verification, export (JSON/DOT/HTML)
└── graph_db.py       — SQLite + FTS5 storage, CLI (click), anomaly detection
tests/
├── conftest.py       — synthetic BA project (fixtures)
├── test_config.py    — config loading, normalization, classification
├── test_scanning.py  — definition/reference scanning
├── test_graph.py     — graph construction, verification
├── test_db.py        — SQLite import, FTS, helpers
└── test_cli.py       — CLI commands + JSON output
```

## Tests

```bash
uv run pytest tests/ -v
```

122 tests cover every layer: config → scanning → graph → DB → CLI → audit. A synthetic BA project (5 artifact types, 11 definitions, cross-references, dangling refs, coverage gaps) is built in session-scoped fixtures.

## Claude Code integration

The `.claude/skills/` directory contains skills for [Claude Code](https://claude.ai/claude-code). They are auto-activated by the Claude agent when relevant — no setup needed.

| Skill | Description |
|---|---|
| **`/review <ID>`** | Semantic review — gather full text of linked artifacts, validate completeness and consistency |
| **`/reindex`** | Re-scan artifacts and rebuild the graph DB |
| **`/find-anomalies`** | Detect and explain graph anomalies (islands, cycles, dangling refs) |
| **`/audit`** | Global audit — funnel: structural analysis → coverage gaps → semantic review of flagged artifacts via subagents |

The primary workflow with Claude Code:
1. `/reindex` — build/update the graph
2. `/review FEAT-01` — deep semantic review of any artifact
3. `/audit` — full graph audit with prioritized review

All commands support `--json` output, making them suitable for agent pipelines.

## License

MIT

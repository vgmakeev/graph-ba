# Graph BA

Config-driven traceability graph for business analysis artifacts. Scans markdown documents, builds a cross-reference graph in SQLite with FTS5 full-text search, and provides CLI commands for navigation, validation, and anomaly detection.

## Why

BA projects accumulate hundreds of interconnected artifacts — requirements, business rules, processes, decisions, features, domain models. Keeping cross-references consistent and complete across 300+ documents is hard. Graph BA automates this:

- **Indexes** artifact definitions and cross-references from markdown files
- **Builds** a directed graph with file:line attribution for every edge
- **Validates** coverage, bidirectional links, dangling references, numeric conflicts
- **Detects anomalies** — islands, cycles, bridges, bottleneck nodes, dead ends
- **Searches** with FTS5 full-text search and semantic clustering

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

## Core workflow

The two key actions — the essence of the project:

1. **`graph-ba import`** — reindex: scan markdown files, build traceability graph in SQLite
2. **`graph-ba review <ID> --semantic`** — semantic review: gather full text of all linked artifacts, validate completeness, consistency, and traceability

Everything else (search, anomalies, coverage, path, impact) — auxiliary navigation tools over the graph.

## Quick start

```bash
# 1. Create a config file in your project root
graph-ba init

# 2. Edit graph-ba.toml — define your artifact types and scan rules

# 3. Import: scan documents and build the graph DB
graph-ba import

# 4. Semantic review of an artifact (the main use case)
graph-ba review REQ-01 --semantic --lines 20

# 5. Explore
graph-ba search "delivery"
graph-ba node REQ-01
graph-ba anomalies
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
| `review <id> --semantic` | Full text of all linked artifacts for deep review |
| `anomalies` | Detect islands, cycles, bridges, bottlenecks, dangling refs |
| `coverage` | Cross-layer coverage matrix |
| `sql <query>` | Raw SQL against the DB |

Global options: `--root <path>` (project root, default `.`), `--db <path>` (SQLite DB path).

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
```

## Claude Code integration

The `.agents/` directory contains skills for [Claude Code](https://claude.ai/claude-code). They are installed automatically when you clone the repository — no extra setup needed.

| Skill | Description |
|---|---|
| **`/review <ID>`** | Semantic review — gather full text of linked artifacts, validate completeness and consistency |
| **`/reindex`** | Re-scan artifacts and rebuild the graph DB |
| **`/find-anomalies`** | Detect and explain graph anomalies (islands, cycles, dangling refs) |

The primary workflow with Claude Code:
1. `/reindex` — build/update the graph
2. `/review FEAT-01` — deep semantic review of any artifact

## License

MIT

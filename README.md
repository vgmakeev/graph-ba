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

## Quick start

```bash
# 1. Create a config file in your project root
graph-ba init

# 2. Edit graph-ba.toml — define your artifact types and scan rules

# 3. Import: scan documents and build the graph DB
graph-ba import

# 4. Explore
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
| `neighbors <id>` | List neighbors with type/direction filters |
| `path <from> <to>` | Shortest path between two artifacts |
| `walk <id> --depth N` | BFS traversal tree from a node |
| `impact <id>` | Cascade analysis — what does changing this affect? |
| `review <id>` | Validate structure + show context from linked artifacts |
| `review <id> --semantic` | Full text of all linked artifacts for deep review |
| `anomalies` | Detect islands, cycles, bridges, bottlenecks, dangling refs |
| `coverage` | Cross-layer coverage matrix |
| `gaps <type1> <type2>` | Artifacts of type1 with no links to type2 |
| `hubs` | Most connected nodes |
| `orphans` | Poorly connected artifacts |
| `cluster <term>` | Semantic cluster lookup |
| `stats` | Summary statistics |
| `sql <query>` | Raw SQL against the DB |
| `render` | Interactive HTML visualization (vis-network) |

Global options: `--root <path>` (project root, default `.`), `--db <path>` (SQLite DB path).

## Configuration

All artifact types, scan rules, and validation expectations are defined in `graph-ba.toml` at the project root. Run `graph-ba init` to generate a template.

### Sections

**`[scan]`** — directories to scan for `.md` files:
```toml
[scan]
dirs = ["docs", "specs"]
```

**`[types.*]`** — artifact types with regex patterns:
```toml
[types.REQ]
label = "Requirements"
ref = '(?<![A-Za-z])(REQ-\d{2,4})(?!\d)'    # regex for finding references (group 1 = ID)
classify = 'REQ-\d{2,4}'                      # regex for classifying an ID string
restrict_to = ["docs/requirements"]            # optional: only match in these paths
```

**`[[definitions]]`** — where to find artifact definitions:
```toml
[[definitions]]
type = "REQ"
file = "docs/requirements.md"        # supports glob: "docs/reqs/REQ-*.md"
mode = "table"                       # "table" or "heading"
pattern = '^\|\s*(REQ-\d{2,4})\s*\|' # group 1 = ID, group 2 = title (optional)
```

**`[[coverage]]`** — expected cross-layer links:
```toml
[[coverage]]
source = "FEAT"
target = "REQ"
label = "FEAT → REQ"
```

**`[review]`** — validation rules:
```toml
[review]
required_sections = { "FEAT" = ["Goal", "Scope", "Acceptance Criteria"] }
expected_bidir = { "FEAT" = ["REQ", "RULE"] }
```

**`[clusters]`** — semantic grouping:
```toml
[clusters]
"Order Management" = ["REQ-01", "REQ-02", "FEAT-01", "RULE-05"]
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

The `.agents/` directory contains skills for [Claude Code](https://claude.ai/claude-code):

- **`/reindex`** — re-scan artifacts and rebuild the graph DB
- **`/review-artifact`** — gather all traceability context and validate an artifact
- **`/find-anomalies`** — detect and explain graph anomalies

## License

MIT

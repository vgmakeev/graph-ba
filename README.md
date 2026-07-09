# graph-ba

Traceability graph for business analysis artifacts. Scans markdown files and source code, builds a cross-reference graph in SQLite, and provides CLI for search, validation, and linting.

## Why

BA projects have hundreds of interlinked markdown documents. Cross-references between them break silently — renamed IDs, missing links, conflicting numbers, stale content. Manual checking doesn't scale.

graph-ba turns your documents into a queryable graph and lints them automatically. Artifact types and ID patterns are defined in a TOML config — the tool works with any naming convention.

The graph keeps two kinds of metadata useful for agent workflows:

- artifact `origin` comes from `types.<TYPE>.origin` and can classify whole
  artifact classes as `human`, `derived`, `canonical`, `evidence`, etc.;
- edge `relation_type` distinguishes raw text mentions from stronger links such
  as `INDEX`, `CODE_TRACE`, `TEST_EVIDENCE` and `UI_TRACE`.

Origins are project-configurable. Relation types are intentionally small and
generic: graph-ba ships the canonical vocabulary (`CONTAINS`, `TRACES_TO`,
`DEPENDS_ON`, `IMPLEMENTS`, `VERIFIES`, `RENDERS`, plus a few system edges such
as `MENTIONS`, `CODE_TRACE` and `TEST_EVIDENCE`). Projects should normally
configure a sparse matrix of allowed class-to-class edges instead of inventing
new relation words. Runtime/framework-specific facts should be exported by
adapter packages as graph-native artifact blocks; graph-ba core imports those
blocks without knowing the framework.

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
| **`validate <ID>`** | Deterministic per-artifact gate: PASS/FAIL verdict, exit 1 on FAIL |
| `review <ID> --semantic` | Full text of all linked artifacts for deep validation |
| `search <query>` | FTS5 full-text search |
| `node <ID>` | Node details + neighbors |
| `path <from> <to>` | Shortest path between artifacts |
| `impact <ID>` | Cascade analysis |
| `coverage` | Cross-layer coverage matrix |
| `matrix` | Sparse JSON matrix of typed artifact relationships |
| `artifact-state` | Fingerprints + computed implemented/verified/changing/stale state |
| `change create/show/accept/archive` | Minimal graph-native change workflow |
| `gate <ID>` | Explore/dev/review/release readiness gate |
| `graph <ID>` | Agent-facing JSON graph slice with nodes, typed edges, content excerpts and findings |
| `pack <ID>` | Agent pack for a change, screen family, screen or artifact |
| `code-refs` | Code → artifact links (`@trace` comments) |
| `sql <query>` | Raw SQL |

All commands: `--json` for machine output, `--root`/`--db` for paths.

Read commands keep themselves honest: on an empty or stale database they
rebuild the graph automatically before answering (import is cheap). Disable
with `--no-auto-import` to get a hard error on empty and a stderr warning on
stale instead — a silently clean result on a graph that was never imported is
the worst failure mode for agent workflows.

### validate — deterministic gate for one artifact

```bash
graph-ba validate F-01           # ✓/✗/⚠ checks + VERDICT: PASS|FAIL
graph-ba --json validate F-01    # {"id", "verdict", "checks": [...]}
```

Fail-level checks: artifact is defined, all outgoing refs resolve, required
sections present, expected cross-layer links exist. Warn-level (don't fail):
TODO markers and missing test evidence. Bidirectional links are not a default
modeling goal; use incoming/outgoing graph queries instead of duplicating edges.
Exit code 0 on PASS, 1 on FAIL — usable directly in CI and agent loops.

### audit --baseline — ratchet for brownfield corpora

```bash
graph-ba audit --write-baseline baseline.json   # snapshot current issues
graph-ba audit --baseline baseline.json         # exit 1 only on NEW issues
```

Every issue gets a stable fingerprint (`DANGLING:REQ-99`,
`COVERAGE_GAP:FEAT:REQ:F-02`, ...). With `--baseline`, known issues are
tolerated, resolved ones reported, and only new regressions fail the run —
so audit stays useful on corpora with hundreds of legacy issues.

### matrix — sparse relationship projection for agents

```bash
graph-ba matrix \
  --source-type TEST \
  --target-type REQ \
  --relation TEST_EVIDENCE \
  --out reports/graphba/test-req-matrix.json
```

The output is `graph-ba.sparse-matrix.v1`: typed nodes plus sparse entries
like `TEST --TEST_EVIDENCE--> REQ` with file/line/context evidence. Use it as
machine-readable input for agent packs, CI gates and project dashboards.

### artifact-state — fingerprints and computed status

```bash
graph-ba artifact-state AC-ORD-001 \
  --snapshot .graphba/state/accepted-fingerprints.json

graph-ba artifact-state \
  --write-snapshot .graphba/state/accepted-fingerprints.json \
  --out reports/graphba/artifact-state.json
```

Manual lifecycle stays small: `draft`, `planned`, `accepted`, `archived`.
Everything else is computed from graph facts:

- `implemented`: observed implementation edge exists;
- `verified`: test/evidence edge exists;
- `changing`: active `CHG-*` contains the artifact;
- `stale`: current content/link/observed/evidence fingerprint differs from
  the accepted snapshot.

### graph-native changes, gates and packs

Graph-native projects can define artifacts in markdown blocks and scope them
through `.graphba/changes/<change-id>/change.yaml`:

```md
:::artifact type="AC" id="AC-ORD-001" state="planned" title="Order live updates"
Orders update without reloading the screen.
:::
```

```yaml
id: CHG-orders-live-update
title: Live updates for orders
state: planned
mode: review
scope:
  - AC-ORD-001
```

Useful commands:

```bash
graph-ba change create CHG-orders-live-update --scope AC-ORD-001
graph-ba change show CHG-orders-live-update --json
graph-ba graph CHG-orders-live-update --out .graphba/changes/CHG-orders-live-update/compiled/graph.json
graph-ba pack CHG-orders-live-update --out .graphba/changes/CHG-orders-live-update/compiled/pack.md
graph-ba gate CHG-orders-live-update --mode review
graph-ba change accept CHG-orders-live-update --snapshot .graphba/state/accepted-fingerprints.json
```

Use `graph` as the default agent-facing format. It returns
`graph-ba.graph-slice.v1` JSON:

- `nodes`: scoped artifacts with type, origin, source location, computed flags
  and optional content excerpts;
- `edges`: directed typed relationships inside the scope;
- `relation_catalog`: relation meanings so agents do not need hard-coded
  interpretations;
- `findings`: the same gate findings for the requested mode.

Weak `MENTIONS` edges are excluded by default; pass `--include-mentions` only
when doing broad investigation rather than acceptance or implementation work.
Use `pack` when a human-readable markdown bundle is preferable.

`explore` never blocks, `dev` reports warnings, `review` blocks scoped
contract artifacts that lack observed implementation or AC test evidence, and
`release` also requires an accepted fingerprint snapshot and rejects stale
scope.

## Configuration

Everything is config-driven via `graph-ba.toml`. Define your own artifact types, ID patterns, scan rules, and validation expectations. The tool doesn't assume any specific naming convention.

```toml
[scan]
dirs = ["docs"]

# Define artifact types with regex patterns
[origins.human]
label = "Human primary source"
description = "Client, stakeholder, refined meeting or human dictation input."

[origins.reviewed_derived]
label = "Reviewed derived artifact"
description = "Agent output reviewed by a human analyst."

# Relation terminology comes from graph-ba's small default enum. Override
# [relations.*] only to clarify wording for a project; express project policy
# as a sparse class matrix outside the relation vocabulary.

[types.REQ]
label = "Requirements"
origin = "canonical"  # optional provenance class for all REQ nodes
ref = '(?<![A-Za-z])(REQ-\d{2,4})(?!\d)'
classify = 'REQ-\d{2,4}'

[types.RAW]
label = "Raw Criteria"
origin = "human"
ref = '(?<![A-Za-z])(RAW-\d{2,4})(?!\d)'
classify = 'RAW-\d{2,4}'

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

# Code traceability (// @trace: F-01, REQ-01)
[code]
dirs = ["src"]
coverage_types = ["FEAT", "REQ"]

# Test traceability — test files become TEST: nodes; any artifact ID
# in a test file (comments, names, asserts) counts as test evidence.
# `coverage` shows a "Test coverage" block per listed type.
[tests]
dirs = ["tests"]
extensions = ["py", "ts", "tsx", "js", "dart"]  # default
coverage_types = ["REQ"]

# UI traceability — machine-readable trace sidecars (e.g. a feature-level
# trace.json mapping data-testid → AC IDs) become UI: nodes; any artifact ID
# in them counts as a UI-to-artifact link. `coverage` shows a
# "UI trace coverage" block per listed type.
[ui]
files = ["app/src/features/*/api/trace.json"]  # root-relative globs
coverage_types = ["REQ"]

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
uv run pytest tests/ -v    # 200+ tests
```

## License

MIT

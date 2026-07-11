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
| `change init/discover/diff/context/compile/check/approve/status` | Git-native semantic change workflow |
| `gate <ID>` | Explore/dev/review/release readiness gate |
| `evidence-plan <ID>` | Classify scoped AC and explain required test/evidence kinds |
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

### Git-native changes, gates and packs

Git owns the change lifecycle: branch is the draft, pull request is the
proposal, protected review is approval, and merge is acceptance. graph-ba adds
one semantic manifest and computes the actual artifact delta from stable IDs;
canonical artifacts are edited in place and are never copied into a change
directory.

```md
:::artifact type="AC" id="AC-ORD-001" state="planned" origin="canonical" title="Order live updates"
Orders update without reloading the screen.
:::
```

`origin` may override the type default for a block. Provider adapters must mark
observed aliases as `implementation` or `evidence`; sharing a semantic type
with a canonical artifact must not make an observation part of the proposal.

```yaml
# .graphba/changes/CHG-orders-live-update.yaml
id: CHG-orders-live-update
title: Live updates for orders
intent: Keep the order board current without an operator reload
base_ref: 0123456789abcdef0123456789abcdef01234567
target_ref: main
sources:
  - RAC-ORD-014
# Optional discovery hints; the actual scope comes from semantic Git diff.
scope:
  - AC-ORD-001
```

Useful commands:

```bash
graph-ba change init CHG-orders-live-update \
  --intent "Keep the order board current without an operator reload" \
  --source RAC-ORD-014 --base-ref main \
  --worktree ../project-chg-orders-live-update
graph-ba change discover CHG-orders-live-update
graph-ba change add-artifact CHG-orders-live-update FLOW FLOW-ORDERS-LIVE \
  --title "Keep the order board live" --link TRACES_TO=AC-ORD-001
graph-ba change add-link CHG-orders-live-update FLOW-ORDERS-LIVE CONTAINS AC-ORD-001
graph-ba change diff CHG-orders-live-update
graph-ba change rebase-check CHG-orders-live-update
graph-ba change check CHG-orders-live-update --stage proposal
graph-ba change context CHG-orders-live-update
graph-ba change compile CHG-orders-live-update
graph-ba change review CHG-orders-live-update --out reports/change-review.md
graph-ba change approve CHG-orders-live-update \
  --reviewer "reviewer@example.com" \
  --evidence "https://github.com/org/repo/pull/123"
graph-ba change status CHG-orders-live-update
graph-ba change show CHG-orders-live-update --json
graph-ba evidence-plan CHG-orders-live-update --format md
graph-ba change check CHG-orders-live-update --stage release --mode release
```

`change init --worktree PATH` is the recommended default. It creates an
isolated `change/<change-id>` branch without touching a dirty primary checkout,
stores an immutable `base_ref` commit and remembers the integration
`target_ref`. Plain `change init` switches the current clean checkout to a
change branch. Use `--no-branch` only when the caller owns the Git lifecycle.

`change discover` starts with manifest `sources` and `scope`; free-text search
only supplements those seeds. `change diff` preserves the complete Git file
list for delivery review and also separates `contract_files`,
`supporting_files` and `delivery_files`, so unrelated dirty-worktree edits are
visible without being confused with the semantic proposal.

`change compile` builds separate base, proposed-contract and delivery views,
then writes the typed graph delta and transitive impact paths under
`reports/graphba/changes/<change-id>/`. Historical base graphs are cached per
repository, commit and graph-ba schema in the user cache directory; repeated
`compile`, `context` and release checks do not rescan the same Git tree.
`change rebase-check` compares stable-ID changes on `target_ref` since the
recorded base and fails only on overlapping artifacts or proposal-policy
changes. `change review` renders intent, sources, files, semantic/graph delta,
impact, rebase state, approval and delivery findings as one Markdown or JSON
payload.

Proposal fingerprints bind both canonical artifact deltas and the project
files that define graph/gate meaning (`graph-ba.toml`, project/class matrices
and evidence policy). Changing those files invalidates approval. Duplicate
canonical definitions fail proposal review unless exactly one owning file has
the migration marker:

```md
<!-- graph-ba: canonical-owner -->
```

`change add-artifact` and `change add-link` validate type/ID classification,
relation vocabulary, owner ambiguity and target paths before editing. The same
agent-safe operations are exposed through MCP.

The canonical artifact is the accepted set of stable-ID graph-native artifact
blocks in normal project files, not the change manifest or a generated bundle.
The manifest records intent, sources, scope hints and the Git base. A human
approval attestation binds reviewer, external review evidence, review commit,
base commit and canonical proposal fingerprint. Proposal files must be
committed before approval; the approval record must itself be committed before
release accepts it. Later contract/policy edits or a non-ancestor review commit
make it stale. Implementation and test edits do not invalidate approval. A
protected PR/branch remains the external trust boundary. Merge and Git history
accept and archive the change. `change accept` and `change archive` remain
compatibility commands for the legacy directory layout.

Agents can use the same service through MCP tools `ba_change_init`,
`ba_change_discover`, `ba_change_diff`, `ba_change_context`,
`ba_change_check`, `ba_change_rebase_check`, `ba_change_add_artifact`,
`ba_change_add_link` and `ba_change_status`. Approval is deliberately CLI-only
so an agent-facing MCP connection cannot attest its own proposal.

Use `graph` as the default agent-facing format. It returns
`graph-ba.graph-slice.v1` JSON:

- `nodes`: scoped artifacts with type, origin, source location, computed flags
  and optional content excerpts;
- `edges`: directed typed relationships inside the scope;
- `relation_catalog`: relation meanings so agents do not need hard-coded
  interpretations;
- `evidence_plan`: scoped AC classification, required evidence kinds and
  missing evidence gaps;
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

# Optional symbol-level enrichment. When enabled, graph-ba resolves each
# @trace file:line to the enclosing function/class in a local CodeGraph index.
# Missing indexes or unmatched symbols gracefully keep the existing file node.
[providers.codegraph]
database = ".codegraph/codegraph.db"

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

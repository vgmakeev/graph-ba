"""Shared fixtures: synthetic BA project for graph-ba tests."""
import pytest
from pathlib import Path

from click.testing import CliRunner

from graph_ba.config import load_config
from graph_ba.traceability import (
    scan_definitions, scan_references, scan_index_cross_refs, build_graph,
)
from graph_ba.graph_db import get_db, do_import, cli


# ── Synthetic TOML config ─────────────────────────────────────────

TOML_CONFIG = """\
[scan]
dirs = ["docs"]

[normalize]
char_map = {"_" = "-"}

[[normalize.zero_pad]]
pattern = 'REQ-(\\d+)'
format = 'REQ-{:02d}'

[types.ST]
label = "Stakeholders"
ref = '(?<![A-Za-z])(ST-\\d{2})(?!\\d)'
classify = 'ST-\\d{2}'

[types.FEAT]
label = "Features"
ref = '(?<![A-Za-z])(F-\\d{2})(?!\\d)'
classify = 'F-\\d{2}'

[types.REQ]
label = "Requirements"
ref = '(?<![A-Za-z])(REQ-\\d{2,4})(?!\\d)'
classify = 'REQ-\\d{2,4}'

[types.BP]
label = "Business Processes"
ref = '(?<![A-Za-z])(BP-\\d{2})(?!\\d)'
classify = 'BP-\\d{2}'

[types.BR]
label = "Business Rules"
ref = '(?<![A-Za-z])(BR\\.\\d+)(?!\\d)'
classify = 'BR\\.\\d+'

[[definitions]]
type = "ST"
file = "docs/stakeholders.md"
mode = "heading"
pattern = '^##\\s+(ST-\\d{2})\\s*[\u2014\u2013\\-]\\s*(.*)'

[[definitions]]
type = "FEAT"
file = "docs/features.md"
mode = "heading"
pattern = '^##\\s+(F-\\d{2})\\s*[\u2014\u2013\\-]\\s*(.*)'

[[definitions]]
type = "REQ"
file = "docs/requirements.md"
mode = "table"
pattern = '^\\|\\s*(REQ-\\d{2,4})\\s*\\|'

[[definitions]]
type = "BP"
file = "docs/processes/bp-main.md"
mode = "heading"
pattern = '^##\\s+(BP-\\d{2})\\s*[\u2014\u2013\\-]\\s*(.*)'

[[definitions]]
type = "BR"
file = "docs/rules/BR-*.md"
mode = "heading"
pattern = '^##\\s+(BR\\.\\d+)\\s*[\u2014\u2013\\-]\\s*(.*)'

[[index_tables]]
file = "docs/index.md"
first_col = '^\\|\\s*(F-\\d{2})\\s*\\|'

[[coverage]]
source = "FEAT"
target = "REQ"
label = "FEAT \u2192 REQ"

[[coverage]]
source = "REQ"
target = "BP"
label = "REQ \u2192 BP"

[review]
required_sections = {"FEAT" = ["Goal", "Scope"]}
expected_bidir = {"FEAT" = ["REQ"]}

[[review.expected_cross_layer.FEAT]]
type = "REQ"
label = "requirements"

[clusters]
"Order Management" = ["F-01", "REQ-01", "BP-01"]
"Delivery" = ["F-02", "BR.2"]
"""

# ── Synthetic markdown files ──────────────────────────────────────

STAKEHOLDERS_MD = """\
# Stakeholders

## ST-01 \u2014 Administrator
System admin user.

## ST-02 \u2014 Client
End client user.
"""

FEATURES_MD = """\
# Features

## F-01 \u2014 Order Management

### Goal
Manage the full order lifecycle.

References: REQ-01, REQ-02, BP-01.
Also REQ-99 which does not exist.

```
REQ-50 should be ignored inside code fence
```

## F-02 \u2014 Delivery Tracking

References: BR.1, BP-01.
Delivery time: 30 \u043c\u0438\u043d.
"""

REQUIREMENTS_MD = """\
# Requirements

| ID | Description | Links |
|---|---|---|
| REQ-01 | Must manage orders | F-01 |
| REQ-02 | Must track delivery | F-01 |
| REQ-03 | Must support payments | |
"""

PROCESSES_MD = """\
# Business Processes

## BP-01 \u2014 Main Order Process
Order processing flow. References: REQ-01.
Delivery time: 45 \u043c\u0438\u043d.

## BP-02 \u2014 Secondary Process
Secondary flow. References: REQ-02.
"""

BR_PRICING_MD = """\
# Pricing Rules

## BR.1 \u2014 Pricing Rule
Base pricing. References: F-01.
"""

BR_DELIVERY_MD = """\
# Delivery Rules

## BR.2 \u2014 Delivery Rule
Delivery SLA. References: F-02.
"""

INDEX_MD = """\
# Cross-Reference Index

| Feature | Requirements |
|---|---|
| F-01 | REQ-01, REQ-02 |
| F-02 | BR.1, BP-01 |
"""


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def ba_project(tmp_path_factory):
    """Create a complete synthetic BA project."""
    root = tmp_path_factory.mktemp("ba_project")
    (root / "graph-ba.toml").write_text(TOML_CONFIG, encoding="utf-8")

    docs = root / "docs"
    docs.mkdir()
    (docs / "processes").mkdir()
    (docs / "rules").mkdir()

    (docs / "stakeholders.md").write_text(STAKEHOLDERS_MD, encoding="utf-8")
    (docs / "features.md").write_text(FEATURES_MD, encoding="utf-8")
    (docs / "requirements.md").write_text(REQUIREMENTS_MD, encoding="utf-8")
    (docs / "processes" / "bp-main.md").write_text(PROCESSES_MD, encoding="utf-8")
    (docs / "rules" / "BR-pricing.md").write_text(BR_PRICING_MD, encoding="utf-8")
    (docs / "rules" / "BR-delivery.md").write_text(BR_DELIVERY_MD, encoding="utf-8")
    (docs / "index.md").write_text(INDEX_MD, encoding="utf-8")

    return root


@pytest.fixture(scope="session")
def project_config(ba_project):
    return load_config(ba_project)


@pytest.fixture(scope="session")
def scan_result(ba_project, project_config):
    registry = scan_definitions(ba_project, project_config)
    references = scan_references(ba_project, registry, project_config)
    index_xrefs = scan_index_cross_refs(ba_project, project_config)
    return registry, references, index_xrefs


@pytest.fixture(scope="session")
def built_graph(scan_result, project_config):
    registry, references, index_xrefs = scan_result
    G = build_graph(registry, references, project_config, index_xrefs)
    return G, registry


@pytest.fixture(scope="session")
def db_path(ba_project, tmp_path_factory):
    """Session-scoped populated DB for read-only tests."""
    path = tmp_path_factory.mktemp("db") / "test.db"
    db = get_db(path)
    do_import(ba_project, db)
    db.close()
    return path


@pytest.fixture
def db_conn(db_path):
    """Per-test DB connection (read-only use)."""
    conn = get_db(db_path)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def cli_env(ba_project, db_path):
    """Session-scoped DB + CliRunner for read-only CLI tests."""
    runner = CliRunner()
    return runner, ba_project, db_path


@pytest.fixture
def cli_env_rw(ba_project, tmp_path):
    """Fresh DB + CliRunner for CLI tests that write."""
    path = tmp_path / "cli_test.db"
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--root", str(ba_project), "--db", str(path), "import"
    ])
    assert result.exit_code == 0, result.output
    return runner, ba_project, path

from graph_ba.config import load_config
from graph_ba.db import do_import, get_db
from graph_ba.traceability import (
    build_graph,
    scan_definitions,
    scan_mini_registry_traces,
    scan_references,
)


TOML = """\
[scan]
dirs = ["docs"]

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.BP]
label = "Business Process"
origin = "derived"
ref = '(BP-\\d{2})'
classify = 'BP-\\d{2}'

[[definitions]]
type = "AC"
file = "docs/spec.md"
mode = "table"
pattern = '^\\|\\s*(AC-[A-Z]+-\\d{3})\\s*\\|'

[[definitions]]
type = "BP"
file = "docs/bp.md"
mode = "heading"
pattern = '^##\\s+(BP-\\d{2})\\s*[-—]\\s*(.*)'

[mini_registry]
dirs = ["mini_app"]
resource_type = "CRUDL_RESOURCE"
custom_method_type = "CUSTOM_METHOD"
"""


REGISTRY_PY = """\
from app.registry.types import CustomMethod, Resource, TraceLink, Traceability
from app.registry import field

orders = Resource(
    name="orders",
    title="Orders",
    table_name="orders",
    fields=(field.id(),),
    permissions={"read": "orders.read", "write": "orders.write", "delete": "orders.delete"},
    trace=Traceability(
        artifacts=("AC-ORD-001",),
        links=(TraceLink("BP-01", relation="depends_on"),),
    ),
)

recalculate = CustomMethod(
    code="orders.recalculate",
    permission="orders.write",
    endpoint="/api/admin/orders:recalculate",
    trace=Traceability.implements("AC-ORD-002"),
)
"""


def _write_project(root):
    (root / "graph-ba.toml").write_text(TOML, encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "spec.md").write_text(
        "| ID | Text |\n|---|---|\n| AC-ORD-001 | list orders |\n| AC-ORD-002 | recalculate |\n",
        encoding="utf-8",
    )
    (root / "docs" / "bp.md").write_text("## BP-01 — Process\n", encoding="utf-8")
    (root / "mini_app").mkdir()
    (root / "mini_app" / "resources.py").write_text(REGISTRY_PY, encoding="utf-8")


def test_mini_registry_trace_scanner_creates_typed_edges(tmp_path):
    _write_project(tmp_path)
    config = load_config(tmp_path)
    registry = scan_definitions(tmp_path, config)
    references = scan_references(tmp_path, registry, config)
    mini_refs = scan_mini_registry_traces(tmp_path, config)

    graph = build_graph(
        registry,
        references,
        config,
        mini_registry_traces=mini_refs,
    )

    assert graph.nodes["CRUDL_RESOURCE:orders"]["type"] == "CRUDL_RESOURCE"
    assert graph.nodes["CUSTOM_METHOD:orders.recalculate"]["type"] == "CUSTOM_METHOD"
    assert graph.edges["CRUDL_RESOURCE:orders", "AC-ORD-001"]["relation_type"] == "IMPLEMENTS"
    assert graph.edges["CRUDL_RESOURCE:orders", "BP-01"]["relation_type"] == "DEPENDS_ON"
    assert graph.edges["CUSTOM_METHOD:orders.recalculate", "AC-ORD-002"]["relation_type"] == "IMPLEMENTS"


def test_mini_registry_trace_imports_to_sqlite(tmp_path):
    _write_project(tmp_path)
    db = get_db(tmp_path / "graph.db")

    do_import(tmp_path, db, quiet=True)

    resources = db.execute(
        "SELECT id, type FROM artifacts WHERE type IN ('CRUDL_RESOURCE', 'CUSTOM_METHOD')"
    ).fetchall()
    edges = db.execute(
        "SELECT source_id, target_id, relation_type FROM edges "
        "WHERE source_id LIKE 'CRUDL_RESOURCE:%' OR source_id LIKE 'CUSTOM_METHOD:%'"
    ).fetchall()

    assert {row["id"] for row in resources} == {
        "CRUDL_RESOURCE:orders",
        "CUSTOM_METHOD:orders.recalculate",
    }
    assert {
        (row["source_id"], row["target_id"], row["relation_type"]) for row in edges
    } == {
        ("CRUDL_RESOURCE:orders", "AC-ORD-001", "IMPLEMENTS"),
        ("CRUDL_RESOURCE:orders", "BP-01", "DEPENDS_ON"),
        ("CUSTOM_METHOD:orders.recalculate", "AC-ORD-002", "IMPLEMENTS"),
    }

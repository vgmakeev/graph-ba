from graph_ba.config import load_config
from graph_ba.db import do_import, get_db
from graph_ba.traceability import (
    build_graph,
    build_mini_admin_component_trace_entries,
    export_mini_admin_component_trace_map,
    scan_definitions,
    scan_mini_admin_component_traces,
    scan_mini_admin_source_traces,
    scan_references,
)


TOML = """\
[scan]
dirs = ["docs"]

[types.SCR]
label = "Screen"
origin = "human_designed"
ref = '(SCR-ADMIN-[A-Z0-9-]+)'
classify = 'SCR-ADMIN-[A-Z0-9-]+'

[types.UIC]
label = "UI component"
origin = "human_designed"
ref = '(UIC-[A-Z0-9-]+)'
classify = 'UIC-[A-Z0-9-]+'

[types.AC]
label = "Acceptance"
origin = "canonical"
ref = '(AC-[A-Z]+-[0-9]{3})'
classify = 'AC-[A-Z]+-[0-9]{3}'

[types.RAC]
label = "Raw acceptance"
origin = "human"
ref = '(RAC-[A-Z]+-[0-9]{3})'
classify = 'RAC-[A-Z]+-[0-9]{3}'

[[definitions]]
type = "SCR"
file = "docs/screens.md"
mode = "heading"
pattern = '^##\\s+(SCR-ADMIN-[A-Z0-9-]+)\\s*[-—]\\s*(.*)'

[react_ui]
dirs = ["admin/src/features"]
include_patterns = ["^UIC-"]

[mini_admin_sources]
dirs = ["admin/src/features"]
resource_type = "CRUDL_RESOURCE"
custom_method_type = "CUSTOM_METHOD"

[tests]
files = ["admin/src/**/*.test.ts"]
"""


RESOURCES_TS = """\
export const ORDER_RESOURCES = {
  orders: "orders",
  orderItems: "order_items",
} as const;
"""


SOURCES_TS = """\
import { computedFrontendDataSource, crudlDataSource, customMethodDataSource } from "@mini/admin/data-source-inspector";
import { ORDER_RESOURCES } from "./resources";

export const orderSources = {
  orders: crudlDataSource({
    resource: ORDER_RESOURCES.orders,
  }),
  rollup: {
    kind: "computed-service",
    resource: ORDER_RESOURCES.orderItems,
    derivedFrom: [ORDER_RESOURCES.orders, "couriers"],
  },
  recalculate: {
    ...customMethodDataSource({
      endpoint: "/api/admin/orders:recalculate",
    }),
    meta: { customMethods: ["orders.recalculate"] },
  },
  health: computedFrontendDataSource({
    derivedFrom: [ORDER_RESOURCES.orders],
  }),
};
"""


TRACE_JSON = """\
{
  "order-save": {
    "acIds": ["AC-ORD-001"],
    "rawIds": ["RAC-ORD-001"],
    "source": "orders"
  },
  "order-recalculate": {
    "acIds": ["AC-ORD-002"],
    "rawIds": ["RAC-ORD-002"],
    "source": "recalculate"
  },
  "order-health": {
    "acIds": ["AC-ORD-001"],
    "rawIds": ["RAC-ORD-001"],
    "source": "health"
  }
}
"""


SCREEN_TSX = """\
export function OrderScreen() {
  return (
    <section data-screen-family-id="SCR-ADMIN-ORDER">
      <button data-testid="order-save" data-uic-id="UIC-ORDER-SAVE">Save</button>
      <button data-testid="order-recalculate">Recalculate</button>
      <div data-testid="order-health">Health</div>
    </section>
  );
}
"""


TEST_TS = """\
test("AC-ORD-001 saves order", () => {});
test("AC-ORD-002 recalculates order", () => {});
"""


def _write_project(root):
    (root / "graph-ba.toml").write_text(TOML, encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "screens.md").write_text(
        "## SCR-ADMIN-ORDER — Order screen\n",
        encoding="utf-8",
    )
    api_dir = root / "admin" / "src" / "features" / "order" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "resources.ts").write_text(RESOURCES_TS, encoding="utf-8")
    (api_dir / "sources.ts").write_text(SOURCES_TS, encoding="utf-8")
    (api_dir / "trace.json").write_text(TRACE_JSON, encoding="utf-8")
    ui_dir = root / "admin" / "src" / "features" / "order" / "ui"
    ui_dir.mkdir(parents=True)
    (ui_dir / "order-screen.tsx").write_text(SCREEN_TSX, encoding="utf-8")
    (ui_dir / "order-screen.test.ts").write_text(TEST_TS, encoding="utf-8")


def test_mini_admin_sources_create_screen_dependencies(tmp_path):
    _write_project(tmp_path)
    config = load_config(tmp_path)
    registry = scan_definitions(tmp_path, config)
    references = scan_references(tmp_path, registry, config)
    traces = scan_mini_admin_source_traces(tmp_path, config)
    component_traces = scan_mini_admin_component_traces(tmp_path, config)

    graph = build_graph(
        registry,
        references,
        config,
        mini_admin_source_traces=traces,
        mini_admin_component_traces=component_traces,
    )

    assert graph.edges["SCR-ADMIN-ORDER", "CRUDL_RESOURCE:orders"]["relation_type"] == "DEPENDS_ON"
    assert graph.edges["SCR-ADMIN-ORDER", "CRUDL_RESOURCE:order_items"]["relation_type"] == "DEPENDS_ON"
    assert graph.edges["SCR-ADMIN-ORDER", "CRUDL_RESOURCE:couriers"]["relation_type"] == "DEPENDS_ON"
    assert graph.edges["SCR-ADMIN-ORDER", "CUSTOM_METHOD:orders.recalculate"]["relation_type"] == "DEPENDS_ON"
    assert graph.edges["SCR-ADMIN-ORDER", "FRONTEND_COMPUTED:SCR-ADMIN-ORDER:health"]["relation_type"] == "DEPENDS_ON"
    assert graph.edges["SCR-ADMIN-ORDER", "UIC-ORDER-SAVE"]["relation_type"] == "CONTAINS"
    assert graph.edges["UIC-ORDER-SAVE", "AC-ORD-001"]["relation_type"] == "TRACES_TO"
    assert graph.edges["UIC-ORDER-SAVE", "RAC-ORD-001"]["relation_type"] == "TRACES_TO"
    assert graph.edges["UIC-ORDER-SAVE", "CRUDL_RESOURCE:orders"]["relation_type"] == "DEPENDS_ON"
    assert graph.edges["UIC:order-recalculate", "CUSTOM_METHOD:orders.recalculate"]["relation_type"] == "DEPENDS_ON"
    assert graph.edges["UIC:order-health", "FRONTEND_COMPUTED:SCR-ADMIN-ORDER:health"]["relation_type"] == "DEPENDS_ON"


def test_mini_admin_sources_import_to_sqlite(tmp_path):
    _write_project(tmp_path)
    db = get_db(tmp_path / "graph.db")

    do_import(tmp_path, db, quiet=True)

    edges = db.execute(
        "SELECT source_id, target_id, relation_type FROM edges "
        "WHERE source_id = 'SCR-ADMIN-ORDER'"
    ).fetchall()

    assert {
        (row["source_id"], row["target_id"], row["relation_type"]) for row in edges
    } == {
        ("SCR-ADMIN-ORDER", "CRUDL_RESOURCE:orders", "DEPENDS_ON"),
        ("SCR-ADMIN-ORDER", "CRUDL_RESOURCE:order_items", "DEPENDS_ON"),
        ("SCR-ADMIN-ORDER", "CRUDL_RESOURCE:couriers", "DEPENDS_ON"),
        ("SCR-ADMIN-ORDER", "CUSTOM_METHOD:orders.recalculate", "DEPENDS_ON"),
        ("SCR-ADMIN-ORDER", "FRONTEND_COMPUTED:SCR-ADMIN-ORDER:health", "DEPENDS_ON"),
        ("SCR-ADMIN-ORDER", "UIC-ORDER-SAVE", "CONTAINS"),
        ("SCR-ADMIN-ORDER", "UIC:order-recalculate", "CONTAINS"),
        ("SCR-ADMIN-ORDER", "UIC:order-health", "CONTAINS"),
    }


def test_mini_admin_component_trace_export_groups_by_screen(tmp_path):
    _write_project(tmp_path)
    config = load_config(tmp_path)

    entries = build_mini_admin_component_trace_entries(tmp_path, config)
    exported = export_mini_admin_component_trace_map(tmp_path, config)

    assert {entry["component_id"] for entry in entries} == {
        "UIC-ORDER-SAVE",
        "UIC:order-recalculate",
        "UIC:order-health",
    }
    assert exported["SCR-ADMIN-ORDER"]["order-save"] == {
        "sources": ["orders"],
        "acIds": ["AC-ORD-001"],
        "rawIds": ["RAC-ORD-001"],
        "testIds": ["TEST:admin/src/features/order/ui/order-screen.test.ts"],
    }
    assert exported["SCR-ADMIN-ORDER"]["UIC-ORDER-SAVE"] == exported["SCR-ADMIN-ORDER"]["order-save"]


def test_mini_admin_component_trace_accepts_uic_ids_without_testid_alias(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(TOML, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "screens.md").write_text(
        "## SCR-ADMIN-ORDER — Order screen\n",
        encoding="utf-8",
    )
    api_dir = tmp_path / "admin" / "src" / "features" / "order" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "resources.ts").write_text(RESOURCES_TS, encoding="utf-8")
    (api_dir / "sources.ts").write_text(SOURCES_TS, encoding="utf-8")
    (api_dir / "trace.json").write_text(
        """\
{
  "UIC-ORDER-SAVE": {
    "acIds": ["AC-ORD-001"],
    "rawIds": ["RAC-ORD-001"],
    "source": "orders"
  }
}
""",
        encoding="utf-8",
    )
    ui_dir = tmp_path / "admin" / "src" / "features" / "order" / "ui"
    ui_dir.mkdir(parents=True)
    (ui_dir / "order-screen.tsx").write_text(
        """\
export function OrderScreen() {
  return (
    <section data-screen-family-id="SCR-ADMIN-ORDER">
      <button data-uic-id="UIC-ORDER-SAVE">Save</button>
    </section>
  );
}
""",
        encoding="utf-8",
    )
    (ui_dir / "order-screen.test.ts").write_text(TEST_TS, encoding="utf-8")

    config = load_config(tmp_path)
    entries = build_mini_admin_component_trace_entries(tmp_path, config)
    exported = export_mini_admin_component_trace_map(tmp_path, config)

    assert entries[0]["component_id"] == "UIC-ORDER-SAVE"
    assert entries[0]["selector"] == "UIC-ORDER-SAVE"
    assert entries[0]["aliases"] == []
    assert exported["SCR-ADMIN-ORDER"]["UIC-ORDER-SAVE"] == {
        "sources": ["orders"],
        "acIds": ["AC-ORD-001"],
        "rawIds": ["RAC-ORD-001"],
        "testIds": ["TEST:admin/src/features/order/ui/order-screen.test.ts"],
    }

    db = get_db(tmp_path / "graph.db")
    do_import(tmp_path, db, quiet=True)
    artifact = db.execute(
        "SELECT defined, source_file FROM artifacts WHERE id = 'UIC-ORDER-SAVE'"
    ).fetchone()
    assert artifact["defined"] == 1
    assert artifact["source_file"] == "admin/src/features/order/ui/order-screen.tsx"

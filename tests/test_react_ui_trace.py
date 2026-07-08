from graph_ba.config import load_config
from graph_ba.db import do_import, get_db
from graph_ba.traceability import build_graph, scan_react_ui_elements


TOML = """\
[scan]
dirs = ["docs"]

[types.UIC]
label = "UI Components"
origin = "implementation"
ref = '(UIC-[A-Z0-9]+(?:-[A-Z0-9]+)*)'
classify = 'UIC-[A-Z0-9]+(?:-[A-Z0-9]+)*'

[types.SC]
label = "Screens"
origin = "human"
ref = '(SC-[0-9]+)'
classify = 'SC-[0-9]+'

[types.SCR-ADMIN]
label = "Admin screen families"
origin = "human"
ref = '(SCR-ADMIN-[A-Z0-9]+(?:-[A-Z0-9]+)*)'
classify = 'SCR-ADMIN-[A-Z0-9]+(?:-[A-Z0-9]+)*'

[react_ui]
dirs = ["src"]
include_patterns = ["^UIC-"]
"""


SCREEN_TSX = """\
export function OrdersScreen() {
  return (
    <section
      data-testid="orders-screen"
      data-screen-family-id="SCR-ADMIN-ORDERS"
      data-screen-id="SC-02"
    >
      <button data-uic-id="UIC-ORDERS-SAVE">Save</button>
      <div data-uic-id='UIC-ORDERS-EMPTY'>Empty</div>
      <span data-uic-id={"UIC-ORDERS-STATUS"} />
      <span data-uic-id={`UIC-ORDERS-DYNAMIC-${id}`} />
      {/* <div data-uic-id="UIC-ORDERS-COMMENTED" /> */}
    </section>
  );
}
"""


def _write_project(root):
    (root / "graph-ba.toml").write_text(TOML, encoding="utf-8")
    (root / "docs").mkdir()
    src = root / "src"
    src.mkdir()
    (src / "orders-screen.tsx").write_text(SCREEN_TSX, encoding="utf-8")


def test_react_ui_scanner_imports_only_filtered_literal_uic_ids(tmp_path):
    _write_project(tmp_path)
    config = load_config(tmp_path)

    elements = scan_react_ui_elements(tmp_path, config)

    assert {element.target_id for element in elements} == {
        "SCR-ADMIN-ORDERS",
        "SC-02",
        "UIC-ORDERS-SAVE",
        "UIC-ORDERS-EMPTY",
        "UIC-ORDERS-STATUS",
    }
    assert {element.target_id: element.role for element in elements} == {
        "SCR-ADMIN-ORDERS": "screen_family",
        "SC-02": "screen",
        "UIC-ORDERS-SAVE": "component",
        "UIC-ORDERS-EMPTY": "component",
        "UIC-ORDERS-STATUS": "component",
    }
    assert all(element.source_id == "REACT:src/orders-screen.tsx" for element in elements)


def test_react_ui_graph_edges_include_screen_ownership(tmp_path):
    _write_project(tmp_path)
    config = load_config(tmp_path)
    elements = scan_react_ui_elements(tmp_path, config)

    graph = build_graph({}, [], config, react_ui_elements=elements)

    assert graph.nodes["REACT:src/orders-screen.tsx"]["type"] == "REACT_UI"
    assert graph.nodes["SCR-ADMIN-ORDERS"]["type"] == "SCR-ADMIN"
    assert graph.nodes["SC-02"]["type"] == "SC"
    assert graph.nodes["UIC-ORDERS-SAVE"]["type"] == "UIC"
    assert graph.edges["REACT:src/orders-screen.tsx", "UIC-ORDERS-SAVE"]["relation_type"] == "RENDERS"
    assert graph.edges["REACT:src/orders-screen.tsx", "SC-02"]["relation_type"] == "IMPLEMENTS"
    assert graph.edges["SCR-ADMIN-ORDERS", "SC-02"]["relation_type"] == "CONTAINS"
    assert graph.edges["SC-02", "UIC-ORDERS-SAVE"]["relation_type"] == "CONTAINS"
    assert "orders-screen" not in graph
    assert "UIC-ORDERS-COMMENTED" not in graph


def test_react_ui_trace_imports_to_sqlite(tmp_path):
    _write_project(tmp_path)
    db = get_db(tmp_path / "graph.db")

    do_import(tmp_path, db, quiet=True)

    rows = db.execute(
        """
        SELECT source_id, target_id, relation_type FROM edges
        WHERE source_id LIKE 'REACT:%'
           OR source_id IN ('SCR-ADMIN-ORDERS', 'SC-02')
        """
    ).fetchall()

    assert {
        (row["source_id"], row["target_id"], row["relation_type"]) for row in rows
    } == {
        ("REACT:src/orders-screen.tsx", "SCR-ADMIN-ORDERS", "IMPLEMENTS"),
        ("REACT:src/orders-screen.tsx", "SC-02", "IMPLEMENTS"),
        ("REACT:src/orders-screen.tsx", "UIC-ORDERS-SAVE", "RENDERS"),
        ("REACT:src/orders-screen.tsx", "UIC-ORDERS-EMPTY", "RENDERS"),
        ("REACT:src/orders-screen.tsx", "UIC-ORDERS-STATUS", "RENDERS"),
        ("SCR-ADMIN-ORDERS", "SC-02", "CONTAINS"),
        ("SC-02", "UIC-ORDERS-SAVE", "CONTAINS"),
        ("SC-02", "UIC-ORDERS-EMPTY", "CONTAINS"),
        ("SC-02", "UIC-ORDERS-STATUS", "CONTAINS"),
    }

import json

from click.testing import CliRunner

from graph_ba.cli import cli
from graph_ba.config import load_config
from graph_ba.db import do_import, get_db
from graph_ba.gates import _evidence_requirement_satisfied, _load_evidence_policy
from graph_ba.traceability import (
    build_graph,
    scan_definitions,
    scan_graph_native_artifact_traces,
    scan_graph_native_change_traces,
    scan_references,
)


def test_graph_native_artifact_blocks_and_change_scope(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba", "reports/graphba/observed"]

[types.CHG]
label = "Changes"
origin = "derived"
ref = '(?<![A-Za-z])(CHG-[A-Za-z0-9-]+)(?![A-Za-z0-9-])'
classify = 'CHG-[A-Za-z0-9-]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(?<![A-Za-z])(AC-[A-Z]+-\\d{3})(?!\\d)'
classify = 'AC-[A-Z]+-\\d{3}'

[types.ENT]
label = "Entities"
origin = "canonical"
ref = '(?<![A-Za-z])(ENT-[A-Za-z0-9-]+)(?![A-Za-z0-9-])'
classify = 'ENT-[A-Za-z0-9-]+'

[graph_native]
dirs = [".graphba", "reports/graphba/observed"]
change_files = [".graphba/changes/*/change.yaml"]
change_type = "CHG"
scope_relation_type = "DEPENDS_ON"
        """.strip(),
        encoding="utf-8",
    )
    change_dir = tmp_path / ".graphba" / "changes" / "CHG-orders-live-update"
    change_dir.mkdir(parents=True)
    (change_dir / "change.yaml").write_text(
        """
id: CHG-orders-live-update
title: Live updates for orders
state: draft
mode: dev
scope:
  - AC-ORD-001
  - ENT-Order
        """.strip(),
        encoding="utf-8",
    )
    (change_dir / "source.md").write_text(
        """
:::artifact type="AC" id="AC-ORD-001" state="draft" title="Order live update"
The order screen uses ENT-Order.
:::
        """.strip(),
        encoding="utf-8",
    )
    observed_dir = tmp_path / "reports" / "graphba" / "observed"
    observed_dir.mkdir(parents=True)
    (observed_dir / "mini.md").write_text(
        """
:::artifact type="ENT" id="ENT-Order" state="observed" origin="implementation" title="Order" depends_on="AC-ORD-001"
Observed mini resource.
:::
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(tmp_path)
    registry = scan_definitions(tmp_path, config)
    assert {"CHG-orders-live-update", "AC-ORD-001", "ENT-Order"} <= set(registry)

    graph = build_graph(
        registry,
        scan_references(tmp_path, registry, config),
        config,
        graph_native_change_traces=scan_graph_native_change_traces(tmp_path, config),
        graph_native_artifact_traces=scan_graph_native_artifact_traces(tmp_path, config),
    )
    assert graph.has_edge("CHG-orders-live-update", "AC-ORD-001")
    assert graph["CHG-orders-live-update"]["AC-ORD-001"]["relation_type"] == "DEPENDS_ON"
    assert graph.has_edge("AC-ORD-001", "ENT-Order")
    assert graph.has_edge("ENT-Order", "AC-ORD-001")
    assert graph["ENT-Order"]["AC-ORD-001"]["relation_type"] == "DEPENDS_ON"
    assert graph.nodes["ENT-Order"]["origin"] == "implementation"


def test_graph_native_imports_to_sqlite(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba"]

[types.CHG]
label = "Changes"
origin = "derived"
ref = '(?<![A-Za-z])(CHG-[A-Za-z0-9-]+)(?![A-Za-z0-9-])'
classify = 'CHG-[A-Za-z0-9-]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(?<![A-Za-z])(AC-[A-Z]+-\\d{3})(?!\\d)'
classify = 'AC-[A-Z]+-\\d{3}'

[graph_native]
dirs = [".graphba"]
change_files = [".graphba/changes/*/change.yaml"]
        """.strip(),
        encoding="utf-8",
    )
    change_dir = tmp_path / ".graphba" / "changes" / "CHG-smoke"
    change_dir.mkdir(parents=True)
    (change_dir / "change.yaml").write_text(
        "id: CHG-smoke\ntitle: Smoke\nscope:\n  - AC-SMK-001\n",
        encoding="utf-8",
    )
    (change_dir / "source.md").write_text(
        ':::artifact type="AC" id="AC-SMK-001" title="Smoke AC"\n:::\n',
        encoding="utf-8",
    )

    db = get_db(tmp_path / "graph.db")
    do_import(tmp_path, db, quiet=True)

    artifact = db.execute(
        "SELECT id, type, title FROM artifacts WHERE id = 'AC-SMK-001'"
    ).fetchone()
    assert dict(artifact) == {"id": "AC-SMK-001", "type": "AC", "title": "Smoke AC"}
    edge = db.execute(
        "SELECT source_id, target_id FROM edges WHERE source_id = 'CHG-smoke'"
    ).fetchone()
    assert dict(edge) == {"source_id": "CHG-smoke", "target_id": "AC-SMK-001"}


def test_matrix_exports_sparse_relationship_json(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = ["docs"]

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
required_proofs = ["implementation", "verification"]
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[[definitions]]
type = "AC"
file = "docs/spec.md"
mode = "heading"
pattern = '^##\\s+(AC-[A-Z]+-\\d{3})\\s*-\\s*(.*)'

[tests]
dirs = ["tests"]
coverage_types = ["AC"]
        """.strip(),
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text("## AC-ORD-001 - Order AC\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_orders.py").write_text(
        "def test_ac_ord_001():\n    # AC-ORD-001\n    assert True\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "matrix",
            "--source-type",
            "TEST",
            "--target-type",
            "AC",
            "--relation",
            "VERIFIES",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["schema"] == "graph-ba.sparse-matrix.v1"
    assert payload["matrix"]["entries"] == [
        {
            "source": "TEST:tests/test_orders.py",
            "target": "AC-ORD-001",
            "relation_type": "VERIFIES",
            "source_type": "TEST",
            "target_type": "AC",
            "count": 1,
            "evidence": [
                {
                    "source_file": "tests/test_orders.py",
                    "line_number": 2,
                    "context": "# AC-ORD-001",
                }
            ],
        }
    ]


def test_artifact_state_computes_fingerprints_and_stale_status(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = ["docs", "tests", ".graphba", "reports/graphba/observed"]

[types.CHG]
label = "Changes"
origin = "derived"
ref = '(CHG-[A-Za-z0-9-]+)'
classify = 'CHG-[A-Za-z0-9-]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.CRUDL_RESOURCE]
label = "CRUDL Resources"
origin = "implementation"
ref = '(CRUDL_RESOURCE:[A-Za-z0-9_.:-]+)'
classify = 'CRUDL_RESOURCE:[A-Za-z0-9_.:-]+'

[[definitions]]
type = "AC"
file = "docs/spec.md"
mode = "heading"
pattern = '^##\\s+(AC-[A-Z]+-\\d{3})\\s*-\\s*(.*)'

[tests]
dirs = ["tests"]
coverage_types = ["AC"]

[graph_native]
dirs = [".graphba", "reports/graphba/observed"]
change_files = [".graphba/changes/*/change.yaml"]
scope_relation_type = "CONTAINS"
        """.strip(),
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    spec_path = tmp_path / "docs" / "spec.md"
    spec_path.write_text("## AC-ORD-001 - Order AC\nInitial text.\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_orders.py").write_text(
        "def test_ac_ord_001():\n    # AC-ORD-001\n    assert True\n",
        encoding="utf-8",
    )
    change_dir = tmp_path / ".graphba" / "changes" / "CHG-order-update"
    change_dir.mkdir(parents=True)
    (change_dir / "change.yaml").write_text(
        "id: CHG-order-update\nstate: planned\nmode: review\nscope:\n  - AC-ORD-001\n",
        encoding="utf-8",
    )
    observed_dir = tmp_path / "reports" / "graphba" / "observed"
    observed_dir.mkdir(parents=True)
    (observed_dir / "mini.md").write_text(
        ':::artifact type="CRUDL_RESOURCE" id="CRUDL_RESOURCE:orders" '
        'state="accepted" title="Orders" implements="AC-ORD-001"\n:::\n',
        encoding="utf-8",
    )

    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    snapshot_path = tmp_path / ".graphba" / "state" / "accepted-fingerprints.json"
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "artifact-state",
            "AC-ORD-001",
            "--write-snapshot",
            str(snapshot_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    item = payload["artifacts"][0]
    assert item["lifecycle"] == "accepted"
    assert item["computed"]["implemented"] is True
    assert item["computed"]["verified"] is True
    assert item["computed"]["changing"] is True
    assert item["active_changes"] == [
        {"id": "CHG-order-update", "title": "", "state": "planned", "mode": "review"}
    ]
    assert snapshot_path.exists()

    spec_path.write_text("## AC-ORD-001 - Order AC\nChanged text.\n", encoding="utf-8")
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True, force=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "artifact-state",
            "AC-ORD-001",
            "--snapshot",
            str(snapshot_path),
        ],
    )
    assert result.exit_code == 0, result.output
    item = json.loads(result.output)["artifacts"][0]
    assert item["computed"]["stale"] is True
    assert "content" in item["baseline"]["stale_reasons"]


def test_change_create_show_pack_gate_and_accept(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba", "reports/graphba/observed", "tests"]

[types.CHG]
label = "Changes"
origin = "derived"
ref = '(CHG-[A-Za-z0-9-]+)'
classify = 'CHG-[A-Za-z0-9-]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.CRUDL_RESOURCE]
label = "CRUDL Resources"
origin = "implementation"
ref = '(CRUDL_RESOURCE:[A-Za-z0-9_.:-]+)'
classify = 'CRUDL_RESOURCE:[A-Za-z0-9_.:-]+'

[tests]
dirs = ["tests"]
coverage_types = ["AC"]

[graph_native]
dirs = [".graphba", "reports/graphba/observed"]
change_files = [".graphba/changes/*/change.yaml"]
scope_relation_type = "CONTAINS"
        """.strip(),
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "change",
            "create",
            "CHG-order-flow",
            "--title",
            "Order flow",
            "--state",
            "planned",
            "--mode",
            "review",
            "--scope",
            "AC-ORD-001",
        ],
    )
    assert result.exit_code == 0, result.output
    change_dir = tmp_path / ".graphba" / "changes" / "CHG-order-flow"
    assert (change_dir / "change.yaml").exists()
    (change_dir / "source.md").write_text(
        ':::artifact type="AC" id="AC-ORD-001" state="planned" title="Order flow AC"\n:::\n',
        encoding="utf-8",
    )
    observed_dir = tmp_path / "reports" / "graphba" / "observed"
    observed_dir.mkdir(parents=True)
    (observed_dir / "mini.md").write_text(
        ':::artifact type="CRUDL_RESOURCE" id="CRUDL_RESOURCE:orders" '
        'title="Orders" implements="AC-ORD-001"\n:::\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_orders.py").write_text(
        "def test_ac_ord_001():\n    # AC-ORD-001\n    assert True\n",
        encoding="utf-8",
    )

    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    show = runner.invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "--json", "change", "show", "CHG-order-flow"],
    )
    assert show.exit_code == 0, show.output
    payload = json.loads(show.output)
    assert payload["change"]["state"] == "planned"
    assert payload["change"]["mode"] == "review"
    assert payload["scope"][0]["id"] == "AC-ORD-001"
    assert payload["scope"][0]["computed"]["implemented"] is True
    assert payload["scope"][0]["computed"]["verified"] is True

    pack_path = tmp_path / "pack.md"
    pack = runner.invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "pack", "CHG-order-flow", "--out", str(pack_path)],
    )
    assert pack.exit_code == 0, pack.output
    assert "AC-ORD-001" in pack_path.read_text(encoding="utf-8")
    assert "--CONTAINS-->" in pack_path.read_text(encoding="utf-8")

    gate = runner.invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "gate", "CHG-order-flow", "--mode", "review"],
    )
    assert gate.exit_code == 0, gate.output
    assert json.loads(gate.output)["verdict"] == "PASS_WITH_UNKNOWN"

    snapshot_path = tmp_path / ".graphba" / "state" / "accepted-fingerprints.json"
    accept = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "change",
            "accept",
            "CHG-order-flow",
            "--snapshot",
            str(snapshot_path),
        ],
    )
    assert accept.exit_code == 0, accept.output
    assert (change_dir / "archive" / "accepted-delta.json").exists()
    assert (change_dir / "archive" / "accepted-snapshot.json").exists()
    assert snapshot_path.exists()
    assert "state: accepted" in (change_dir / "change.yaml").read_text(encoding="utf-8")


def test_screen_scope_keeps_evidence_as_terminal_profile_not_recursive_scope(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba", "tests"]

[types.SCR]
label = "Screens"
origin = "canonical"
capabilities = ["screen"]
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.REACT_COMPONENT]
label = "React components"
origin = "implementation"
ref = '(REACT_COMPONENT:[A-Za-z0-9_:\\-]+)'
classify = 'REACT_COMPONENT:[A-Za-z0-9_:\\-]+'

[tests]
dirs = ["tests"]
coverage_types = ["AC"]

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba_dir = tmp_path / ".graphba"
    graphba_dir.mkdir()
    (graphba_dir / "source.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" state="accepted" '
        'title="Kitchen" contains="AC-KIT-001"\n:::\n'
        ':::artifact type="AC" id="AC-KIT-001" state="accepted" '
        'title="Kitchen AC"\n:::\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_kitchen.py").write_text(
        "def test_ac_kit_001():\n    # AC-KIT-001\n    assert True\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "gate", "SCR-KITCHEN", "--mode", "dev"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    scope_ids = {item["id"] for item in payload["scope"]}
    assert "AC-KIT-001" in scope_ids
    assert "TEST:tests/test_kitchen.py" not in scope_ids
    assert payload["evidence_profile"]["ac_verified"] == 1
    assert payload["summary"]["evidence_plan"]["ac_total"] == 1
    assert payload["summary"]["evidence_plan"]["gap"] == 1


def test_graph_slice_exports_nodes_edges_content_and_findings(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba", "admin/e2e/tests"]

[types.SCR]
label = "Screens"
origin = "canonical"
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.UIC]
label = "UI Components"
origin = "human_designed"
ref = '(UIC-[A-Z]+)'
classify = 'UIC-[A-Z]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.REACT_COMPONENT]
label = "React components"
origin = "implementation"
ref = '(REACT_COMPONENT:[A-Za-z0-9_:\\-]+)'
classify = 'REACT_COMPONENT:[A-Za-z0-9_:\\-]+'

[tests]
dirs = ["admin/e2e/tests"]
coverage_types = ["AC"]

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba_dir = tmp_path / ".graphba"
    graphba_dir.mkdir()
    (graphba_dir / "artifact-class-matrix.json").write_text(
        json.dumps({
            "schema": "graph-ba.artifact-class-matrix.v1",
            "project": "test",
            "entries": [
                {
                    "source_type": "REACT_COMPONENT",
                    "relation": "RENDERS",
                    "target_type": "UIC",
                    "meaning": "React component renders a stable UI zone.",
                }
            ],
        }),
        encoding="utf-8",
    )
    (graphba_dir / "source.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" state="accepted" '
        'title="Kitchen" contains="UIC-HEADER,REACT_COMPONENT:KitchenHeader"\n'
        'Screen text mentions AC-KIT-001 but the typed edge goes through UIC-HEADER.\n'
        ':::\n'
        ':::artifact type="UIC" id="UIC-HEADER" state="accepted" '
        'title="Header" traces_to="AC-KIT-001"\n:::\n'
        ':::artifact type="REACT_COMPONENT" id="REACT_COMPONENT:KitchenHeader" state="accepted" '
        'title="KitchenHeader" renders="UIC-HEADER"\n:::\n'
        ':::artifact type="AC" id="AC-KIT-001" state="accepted" '
        'title="Kitchen screen visible AC"\n'
        'A long acceptance criterion body that should be excerpted.\n'
        ':::\n',
        encoding="utf-8",
    )
    e2e_dir = tmp_path / "admin" / "e2e" / "tests"
    e2e_dir.mkdir(parents=True)
    (e2e_dir / "kitchen.spec.ts").write_text(
        "test('AC-KIT-001 renders kitchen', async () => {})\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "graph",
            "SCR-KITCHEN",
            "--content-limit",
            "32",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "graph-ba.graph-slice.v1"
    assert payload["target"] == "SCR-KITCHEN"
    assert payload["summary"]["mentions_included"] is False
    node_ids = {node["id"] for node in payload["nodes"]}
    assert {"SCR-KITCHEN", "UIC-HEADER", "AC-KIT-001"} <= node_ids
    assert "TEST:admin/e2e/tests/kitchen.spec.ts" not in node_ids
    assert "REACT_COMPONENT:KitchenHeader" not in node_ids
    assert all(edge["relation"] != "MENTIONS" for edge in payload["edges"])
    assert {
        ("SCR-KITCHEN", "CONTAINS", "UIC-HEADER"),
        ("UIC-HEADER", "TRACES_TO", "AC-KIT-001"),
    } <= {(edge["from"], edge["relation"], edge["to"]) for edge in payload["edges"]}
    ac_node = next(node for node in payload["nodes"] if node["id"] == "AC-KIT-001")
    assert ac_node["computed"]["implemented"] is True
    assert ac_node["implementation_proofs"][0]["source"] == "REACT_COMPONENT:KitchenHeader"
    assert payload["evidence_profile"]["sample"]["AC-KIT-001"][0]["source"] == "TEST:admin/e2e/tests/kitchen.spec.ts"
    assert ac_node["content"]["mode"] == "excerpt"
    assert len(ac_node["content"]["text"]) <= 32
    assert any(item["relation"] == "TRACES_TO" for item in payload["relation_catalog"])
    assert payload["class_matrices"][0]["provider"] == "test"
    assert payload["agent_worklist"] == []

    full = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "graph",
            "SCR-KITCHEN",
            "--view",
            "full",
            "--content",
            "none",
        ],
    )
    assert full.exit_code == 0, full.output
    full_ids = {node["id"] for node in json.loads(full.output)["nodes"]}
    assert "TEST:admin/e2e/tests/kitchen.spec.ts" in full_ids
    assert "REACT_COMPONENT:KitchenHeader" in full_ids


def test_flow_scope_stays_bounded_and_rule_uses_ac_implementation_proof(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba"]

[types.FLOW]
label = "Flows"
origin = "canonical"
ref = '(FLOW-[A-Z-]+)'
classify = 'FLOW-[A-Z-]+'

[types.RULE]
label = "Rules"
origin = "canonical"
ref = '(RULE-[A-Z-]+)'
classify = 'RULE-[A-Z-]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.UIC]
label = "UI Components"
origin = "implementation"
ref = '(UIC-[A-Z-]+)'
classify = 'UIC-[A-Z-]+'

[types.REACT_COMPONENT]
label = "React Components"
origin = "implementation"
ref = '(REACT_COMPONENT:[A-Za-z]+)'
classify = 'REACT_COMPONENT:[A-Za-z]+'

[graph_native]
dirs = [".graphba"]
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    contract = tmp_path / ".graphba" / "contract.md"
    contract.parent.mkdir()
    contract.write_text(
        ':::artifact type="FLOW" id="FLOW-MENU-LIVE" title="Menu live" '
        'contains="RULE-MENU-LIVE,AC-MENU-001"\n:::\n'
        ':::artifact type="RULE" id="RULE-MENU-LIVE" title="Live rule" '
        'traces_to="AC-MENU-001"\n:::\n'
        ':::artifact type="AC" id="AC-MENU-001" title="Menu criterion"\n:::\n'
        ':::artifact type="AC" id="AC-MENU-002" title="Sibling criterion"\n:::\n'
        ':::artifact type="UIC" id="UIC-MENU-SHARED" origin="implementation" '
        'title="Shared zone" traces_to="AC-MENU-001,AC-MENU-002"\n:::\n'
        ':::artifact type="REACT_COMPONENT" id="REACT_COMPONENT:Menu" '
        'origin="implementation" title="Menu" renders="UIC-MENU-SHARED"\n:::\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "graph", "FLOW-MENU-LIVE"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    node_ids = {node["id"] for node in payload["nodes"]}
    assert "AC-MENU-001" in node_ids
    assert "AC-MENU-002" not in node_ids
    flow = next(node for node in payload["nodes"] if node["id"] == "FLOW-MENU-LIVE")
    assert flow["computed"]["implemented"] is True
    rule = next(node for node in payload["nodes"] if node["id"] == "RULE-MENU-LIVE")
    assert rule["computed"]["implemented"] is True
    assert rule["implementation_proofs"][0]["source"] == "UIC-MENU-SHARED"


def test_graph_slice_worklist_reports_screen_readiness_gaps(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba"]

[types.SCR]
label = "Screens"
origin = "canonical"
capabilities = ["screen"]
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.UIC]
label = "UI Components"
origin = "human_designed"
capabilities = ["ui_zone"]
ref = '(UIC-[A-Z]+)'
classify = 'UIC-[A-Z]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
capabilities = ["acceptance"]
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba_dir = tmp_path / ".graphba"
    graphba_dir.mkdir()
    (graphba_dir / "source.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" state="planned" title="Kitchen"\n:::\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "graph", "SCR-KITCHEN"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    worklist = payload["agent_worklist"]
    assert {item["kind"] for item in worklist} == {"add_trace"}
    assert any("no scoped UIC" in item["reason"] for item in worklist)
    assert any("no reachable AC" in item["reason"] for item in worklist)


def test_graph_slice_quality_axes_mark_dynamic_trace_only_scope_partial(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba", "tests"]

[types.SCR]
label = "Screens"
origin = "canonical"
capabilities = ["screen"]
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.UIC]
label = "UI Components"
origin = "human_designed"
ref = '(UIC-[A-Z]+)'
classify = 'UIC-[A-Z]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.EVT]
label = "Events"
origin = "canonical"
capabilities = ["event"]
ref = '(EVT-[A-Z]+)'
classify = 'EVT-[A-Z]+'

[types.STATE]
label = "States"
origin = "canonical"
capabilities = ["state"]
ref = '(STATE-[A-Z]+)'
classify = 'STATE-[A-Z]+'

[types.REACT_COMPONENT]
label = "React components"
origin = "implementation"
ref = '(REACT_COMPONENT:[A-Za-z0-9_:\\-]+)'
classify = 'REACT_COMPONENT:[A-Za-z0-9_:\\-]+'

[tests]
dirs = ["tests"]
coverage_types = ["AC"]

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba_dir = tmp_path / ".graphba"
    graphba_dir.mkdir()
    (graphba_dir / "source.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" state="accepted" title="Kitchen" contains="UIC-SLOT,EVT-ORDER,STATE-SLOT"\n:::\n'
        ':::artifact type="UIC" id="UIC-SLOT" state="accepted" title="Slot" traces_to="AC-KIT-001"\n:::\n'
        ':::artifact type="AC" id="AC-KIT-001" state="accepted" title="New order updates slot capacity"\n:::\n'
        ':::artifact type="EVT" id="EVT-ORDER" state="accepted" title="Order created" traces_to="AC-KIT-001"\n:::\n'
        ':::artifact type="STATE" id="STATE-SLOT" state="accepted" title="Slot lifecycle" traces_to="AC-KIT-001"\n:::\n'
        ':::artifact type="REACT_COMPONENT" id="REACT_COMPONENT:Kitchen" state="accepted" title="Kitchen" renders="UIC-SLOT"\n:::\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "trace.test.ts").write_text("// AC-KIT-001\n", encoding="utf-8")
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "graph", "SCR-KITCHEN"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["overall_confidence"] == "PARTIAL"
    assert payload["verdict"] == "PASS_WITH_GAPS"
    assert payload["readiness"] == "PARTIAL"
    assert payload["quality_axes"]["test_evidence"]["status"] == "PARTIAL"
    assert payload["quality_axes"]["behavior_model"]["status"] == "PARTIAL"
    assert payload["quality_axes"]["behavior_model"]["missing"] == ["flow", "decision"]
    assert payload["evidence_profile"]["ac_trace_only"] == 1
    assert {item["kind"] for item in payload["agent_worklist"]} >= {
        "add_behavior_capability",
        "add_evidence",
    }


def test_screen_worklist_promotes_weak_behavior_links_without_rewriting_legacy_docs(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba"]

[types.SCR]
label = "Screens"
origin = "canonical"
capabilities = ["screen"]
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.AC]
label = "Acceptance"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.UIC]
label = "UI zones"
origin = "human_designed"
ref = '(UIC-[A-Z]+)'
classify = 'UIC-[A-Z]+'

[types.RULE]
label = "Rules"
origin = "canonical"
capabilities = ["decision"]
ref = '(RULE-[A-Z]+)'
classify = 'RULE-[A-Z]+'

[types.STATE]
label = "States"
origin = "canonical"
capabilities = ["state"]
ref = '(STATE-[A-Z]+)'
classify = 'STATE-[A-Z]+'

[types.EVT]
label = "Events"
origin = "canonical"
capabilities = ["event"]
ref = '(EVT-[A-Z]+)'
classify = 'EVT-[A-Z]+'

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba = tmp_path / ".graphba"
    graphba.mkdir()
    (graphba / "contract.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" title="Kitchen" contains="UIC-SLOT,STATE-SLOT,EVT-SLOT"\n:::\n'
        ':::artifact type="UIC" id="UIC-SLOT" title="Slot" traces_to="AC-KIT-001"\n:::\n'
        ':::artifact type="AC" id="AC-KIT-001" title="Live slot capacity"\n:::\n'
        ':::artifact type="STATE" id="STATE-SLOT" title="Slot state"\n:::\n'
        ':::artifact type="EVT" id="EVT-SLOT" title="Slot event"\n:::\n'
        ':::artifact type="RULE" id="RULE-LOAD" title="Load rule"\n'
        'Legacy prose mentions AC-KIT-001 but has no declared trace.\n:::\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "graph", "SCR-KITCHEN"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["quality_axes"]["behavior_model"]["missing"] == [
        "flow",
        "decision",
    ]
    assert payload["quality_axes"]["behavior_model"]["weak_candidates"] == [
        {
            "id": "RULE-LOAD",
            "type": "RULE",
            "title": "Load rule",
            "capabilities": ["decision"],
        }
    ]
    work = {(item["kind"], item["artifact"]) for item in payload["agent_worklist"]}
    assert ("upgrade_relation", "RULE-LOAD") in work
    assert ("add_behavior_capability", "SCR-KITCHEN") in work


def test_old_project_types_can_satisfy_behavior_capabilities_without_new_vocabulary(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba"]

[types.SCR]
label = "Screens"
origin = "canonical"
capabilities = ["screen"]
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.AC]
label = "Acceptance"
origin = "canonical"
capabilities = ["acceptance"]
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.BP]
label = "Legacy business processes"
origin = "derived"
view_role = "contract"
capabilities = ["flow"]
ref = '(BP-\\d{2})'
classify = 'BP-\\d{2}'

[types.BD]
label = "Legacy business decisions"
origin = "derived"
view_role = "contract"
capabilities = ["decision"]
ref = '(BD-\\d{2})'
classify = 'BD-\\d{2}'

[types.LIFECYCLE]
label = "Existing lifecycle model"
origin = "canonical"
capabilities = ["state"]
ref = '(LIFECYCLE-[A-Z]+)'
classify = 'LIFECYCLE-[A-Z]+'

[types.SIGNAL]
label = "Existing domain signals"
origin = "canonical"
capabilities = ["event"]
ref = '(SIGNAL-[A-Z]+)'
classify = 'SIGNAL-[A-Z]+'

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba = tmp_path / ".graphba"
    graphba.mkdir()
    (graphba / "contract.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" title="Kitchen" contains="AC-KIT-001"\n:::\n'
        ':::artifact type="AC" id="AC-KIT-001" title="Live kitchen behavior" '
        'traces_to="BP-02,BD-02"\n:::\n'
        ':::artifact type="BP" id="BP-02" title="Kitchen operation"\n:::\n'
        ':::artifact type="BD" id="BD-02" title="Capacity decision"\n:::\n'
        ':::artifact type="LIFECYCLE" id="LIFECYCLE-SLOT" title="Slot lifecycle" '
        'traces_to="AC-KIT-001"\n:::\n'
        ':::artifact type="SIGNAL" id="SIGNAL-ORDER" title="Order signal" '
        'traces_to="AC-KIT-001"\n:::\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "graph", "SCR-KITCHEN"],
    )

    assert result.exit_code == 0, result.output
    behavior = json.loads(result.output)["quality_axes"]["behavior_model"]
    assert behavior["status"] == "PASS"
    assert behavior["missing"] == []


def test_gate_fails_when_scoped_provider_artifact_is_undefined(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba"]

[types.FLOW]
label = "Flows"
origin = "canonical"
ref = '(FLOW-[A-Z-]+)'
classify = 'FLOW-[A-Z-]+'

[types.SCR]
label = "Screens"
origin = "canonical"
ref = '(SCR-[A-Z-]+)'
classify = 'SCR-[A-Z-]+'

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba = tmp_path / ".graphba"
    graphba.mkdir()
    (graphba / "contract.md").write_text(
        ':::artifact type="FLOW" id="FLOW-KITCHEN-LIVE" title="Kitchen live" traces_to="SCR-KITCHEN"\n:::\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "gate", "FLOW-KITCHEN-LIVE"],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["verdict"] == "FAIL"
    assert payload["readiness"] == "BLOCKED"
    assert payload["quality_axes"]["traceability"]["status"] == "FAIL"
    graph_result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "graph", "FLOW-KITCHEN-LIVE"],
    )
    graph_payload = json.loads(graph_result.output)
    assert any(
        item["kind"] == "resolve_artifact"
        for item in graph_payload["agent_worklist"]
    )


def test_enforced_class_matrix_controls_direction_and_blocks_undeclared_edges(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba"]

[types.SCR]
label = "Screens"
origin = "canonical"
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.UIC]
label = "UI zones"
origin = "canonical"
ref = '(UIC-[A-Z]+)'
classify = 'UIC-[A-Z]+'

[types.AC]
label = "Acceptance"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba = tmp_path / ".graphba"
    graphba.mkdir()
    (graphba / "artifact-class-matrix.json").write_text(
        json.dumps(
            {
                "schema": "graph-ba.artifact-class-matrix.v2",
                "policy": {"enforce": True},
                "entries": [
                    {
                        "source_type": "SCR",
                        "relation": "CONTAINS",
                        "target_type": "UIC",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (graphba / "contract.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" title="Kitchen" contains="UIC-SLOT"\n:::\n'
        ':::artifact type="UIC" id="UIC-SLOT" title="Slot" traces_to="AC-KIT-001"\n:::\n'
        ':::artifact type="AC" id="AC-KIT-001" title="Slot visible"\n:::\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    dev = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "graph", "SCR-KITCHEN"],
    )
    assert dev.exit_code == 0, dev.output
    payload = json.loads(dev.output)
    assert payload["class_matrix_policy"]["enforce"] is True
    assert {node["id"] for node in payload["nodes"]} == {
        "SCR-KITCHEN",
        "UIC-SLOT",
    }
    assert any(item["code"] == "undeclared_class_edge" for item in payload["findings"])
    assert any(item["kind"] == "add_matrix_rule" for item in payload["agent_worklist"])

    review = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path),
            "--db",
            str(db_path),
            "gate",
            "SCR-KITCHEN",
            "--mode",
            "review",
        ],
    )
    assert review.exit_code == 1
    review_payload = json.loads(review.output[review.output.index("{"):])
    assert review_payload["verdict"] == "FAIL"


def test_gate_blocks_unimplemented_review_scope(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba"]

[types.CHG]
label = "Changes"
origin = "derived"
ref = '(CHG-[A-Za-z0-9-]+)'
classify = 'CHG-[A-Za-z0-9-]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
required_proofs = ["implementation", "verification"]
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[graph_native]
dirs = [".graphba"]
change_files = [".graphba/changes/*/change.yaml"]
scope_relation_type = "CONTAINS"
        """.strip(),
        encoding="utf-8",
    )
    change_dir = tmp_path / ".graphba" / "changes" / "CHG-missing"
    change_dir.mkdir(parents=True)
    (change_dir / "change.yaml").write_text(
        "id: CHG-missing\nmode: review\nscope:\n  - AC-ORD-999\n",
        encoding="utf-8",
    )
    (change_dir / "source.md").write_text(
        ':::artifact type="AC" id="AC-ORD-999" state="planned" title="Missing AC"\n:::\n',
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "gate", "CHG-missing", "--mode", "review"],
    )

    assert result.exit_code != 0
    assert '"code": "unimplemented"' in result.output
    assert '"code": "unverified"' in result.output
    assert '"gap_type": "GAP-AC"' in result.output
    assert '"suggested_fix"' in result.output


def test_change_compile_writes_generated_files(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba", "tests"]

[types.CHG]
label = "Changes"
origin = "derived"
ref = '(CHG-[A-Za-z0-9-]+)'
classify = 'CHG-[A-Za-z0-9-]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.REACT_COMPONENT]
label = "React components"
origin = "implementation"
ref = '(REACT_COMPONENT:[A-Za-z0-9_:\\-]+)'
classify = 'REACT_COMPONENT:[A-Za-z0-9_:\\-]+'

[tests]
dirs = ["tests"]
coverage_types = ["AC"]

[graph_native]
dirs = [".graphba"]
change_files = [".graphba/changes/*/change.yaml"]
scope_relation_type = "CONTAINS"
        """.strip(),
        encoding="utf-8",
    )
    change_dir = tmp_path / ".graphba" / "changes" / "CHG-kitchen"
    change_dir.mkdir(parents=True)
    (change_dir / "change.yaml").write_text(
        "id: CHG-kitchen\nmode: dev\nscope:\n  - AC-KIT-001\n",
        encoding="utf-8",
    )
    (change_dir / "source.md").write_text(
        ':::artifact type="AC" id="AC-KIT-001" state="planned" title="Kitchen AC"\n:::\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_kitchen.py").write_text("# AC-KIT-001\n", encoding="utf-8")
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "change", "compile", "CHG-kitchen"],
    )

    assert result.exit_code == 0, result.output
    compiled = change_dir / "compiled"
    assert (compiled / "graph.json").exists()
    assert (compiled / "evidence-plan.json").exists()
    assert (compiled / "evidence-plan.md").exists()
    assert (compiled / "worklist.json").exists()
    assert (compiled / "worklist.md").exists()
    assert (compiled / "gaps.md").exists()
    assert (compiled / "state.yaml").exists()
    assert (compiled / "projection.md").exists()
    assert (compiled / "pack.md").exists()


def test_evidence_plan_classifies_ac_and_reports_missing_required_kind(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba", "tests"]

[types.SCR]
label = "Screens"
origin = "canonical"
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[types.RULE]
label = "Rules"
origin = "canonical"
ref = '(RULE-[A-Z]+)'
classify = 'RULE-[A-Z]+'

[tests]
dirs = ["tests"]
coverage_types = ["AC"]

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba_dir = tmp_path / ".graphba"
    graphba_dir.mkdir()
    (graphba_dir / "source.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" state="accepted" title="Kitchen" contains="AC-KIT-001"\n:::\n'
        ':::artifact type="AC" id="AC-KIT-001" state="accepted" title="Formula rule" traces_to="RULE-LOAD"\n:::\n'
        ':::artifact type="RULE" id="RULE-LOAD" state="accepted" title="Deterministic rule"\n:::\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "trace.test.ts").write_text("// AC-KIT-001\n", encoding="utf-8")
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "evidence-plan", "SCR-KITCHEN"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    item = payload["items"][0]
    assert item["artifact"] == "AC-KIT-001"
    assert "algorithm" in item["kinds"]
    assert "unit" in item["required_evidence"]
    assert item["observed_kinds"] == ["static_source"]
    assert item["missing_required_evidence"] == ["e2e_ui", "unit"]
    assert item["missing_evidence"] == ["e2e_ui", "unit"]
    assert payload["policy"]["evidence_kind_rules"]


def test_evidence_policy_merges_unit_equivalents_from_providers(tmp_path):
    policy_dir = tmp_path / ".graphba"
    policy_dir.mkdir()
    (policy_dir / "evidence-policy.json").write_text(
        json.dumps({"satisfies": {"unit": ["backend_integration"]}}),
        encoding="utf-8",
    )

    policy = _load_evidence_policy(tmp_path)

    assert _evidence_requirement_satisfied("unit", ["frontend_unit"], policy)
    assert _evidence_requirement_satisfied("unit", ["backend_integration"], policy)


def test_evidence_plan_uses_project_evidence_kind_rules(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = [".graphba", "tests"]

[types.SCR]
label = "Screens"
origin = "canonical"
ref = '(SCR-[A-Z]+)'
classify = 'SCR-[A-Z]+'

[types.AC]
label = "Acceptance Criteria"
origin = "canonical"
ref = '(AC-[A-Z]+-\\d{3})'
classify = 'AC-[A-Z]+-\\d{3}'

[tests]
dirs = ["tests"]
coverage_types = ["AC"]

[graph_native]
dirs = [".graphba"]
        """.strip(),
        encoding="utf-8",
    )
    graphba_dir = tmp_path / ".graphba"
    graphba_dir.mkdir()
    (graphba_dir / "evidence-policy.json").write_text(
        json.dumps(
            {
                "schema": "graph-ba.evidence-policy.v1",
                "evidence_kind_rules": [
                    {"kind": "backend_integration", "pattern": "tests/integration/.*contract"},
                    {"kind": "contract", "pattern": "contract"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (graphba_dir / "source.md").write_text(
        ':::artifact type="SCR" id="SCR-KITCHEN" state="accepted" title="Kitchen" contains="AC-KIT-001"\n:::\n'
        ':::artifact type="AC" id="AC-KIT-001" state="accepted" title="Kitchen AC"\n:::\n',
        encoding="utf-8",
    )
    integration_dir = tmp_path / "tests" / "integration"
    integration_dir.mkdir(parents=True)
    (integration_dir / "test_orders_contract.py").write_text("# AC-KIT-001\n", encoding="utf-8")
    db_path = tmp_path / "graph.db"
    db = get_db(db_path)
    do_import(tmp_path, db, quiet=True)
    db.close()

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--db", str(db_path), "evidence-plan", "SCR-KITCHEN"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["items"][0]["observed_kinds"] == ["backend_integration"]

"""Trace the same acceptance criterion across independently exported graphs."""

import json

import pytest
from click.testing import CliRunner

from graph_ba.cli import cli

OLD_COMMIT = "1" * 40
NEW_COMMIT = "2" * 40


def _write_export(root, provider, **data):
    path = root / "providers" / f"{provider}.json"
    path.write_text(json.dumps({
        "schema": "graph-ba.provider.v1", "provider": provider, **data,
    }), encoding="utf-8")
    return path


def _edge(source, relation, target, **data):
    return {"source_id": source, "relation_type": relation, "target_id": target, **data}


def _test_node(kind):
    return {"id": f"test:{kind}", "type": "TEST", "origin": "evidence", "test_level": kind}


def _run(kind, commit, status, date):
    return {
        "test_id": f"test:{kind}", "run_id": f"ci/{date}", "commit": commit, "status": status,
        "finished_at": f"2026-01-{date}T11:00:00+03:00", "evidence": f"reports/{date}.xml",
    }


@pytest.fixture
def project(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(r"""
[scan]
dirs = ["docs"]

[types.AC]
ref = '(AC-[0-9]+)'
classify = 'AC-[0-9]+'
origin = "canonical"

[relations.CALLS]
label = "Calls"

[providers]
graph_files = ["providers/*.json"]

[graph_native]
dirs = ["docs"]

[tests]
dirs = ["tests"]
marker = "@trace"
""", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "contract.md").write_text("""
:::artifact type="REQ" id="REQ-01" origin="canonical" title="Reserve once" contains="AC-01"
The reservation must debit the available amount once.
:::
:::artifact type="AC" id="AC-01" origin="canonical" title="Reject a second reservation"
A second request cannot reserve the exhausted stock.
:::
""", encoding="utf-8")
    (tmp_path / "providers").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "legacy.py").write_text(
        "# Candidate example AC-01\n# AC-99 @trace: AC-01\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "mention.py").write_text("# Candidate AC-01\n", encoding="utf-8")

    _write_export(
        tmp_path, "implementation",
        nodes=[
            {"id": "action:reserve", "type": "ACTION", "origin": "implementation",
             "source_file": "app/actions.py", "line_number": 20},
            {"id": "py:stock.debit", "type": "FUNCTION", "origin": "implementation"},
            {"id": "action:proposal", "type": "ACTION", "origin": "implementation"},
        ],
        edges=[
            _edge("action:reserve", "IMPLEMENTS", "AC-01", source_file="app/actions.py", line_number=25),
            _edge("action:reserve", "IMPLEMENTS", "AC-01", source_file="app/actions.py", line_number=31),
            _edge("action:reserve", "DEPENDS_ON", "AC-01", source_file="app/actions.py", line_number=25),
            _edge("action:reserve", "CALLS", "py:stock.debit"),
            _edge("action:proposal", "CANDIDATE_TRACE", "AC-01"),
        ],
    )
    _write_export(
        tmp_path, "tests",
        nodes=[_test_node(kind) for kind in ("unit", "headless", "ui", "other")],
        edges=[_edge(f"test:{kind}", "VERIFIES", "AC-01") for kind in (
            "unit", "headless", "ui", "other",
        )],
    )
    _write_export(
        tmp_path, "ci",
        edges=[
            _edge("action:reserve", "IMPLEMENTS", "AC-01", source_file="app/actions.py", line_number=25),
        ],
        test_runs=[
            _run("unit", OLD_COMMIT, "passed", "01"),
            _run("unit", NEW_COMMIT, "failed", "02"),
            _run("headless", NEW_COMMIT, "skipped", "02"),
            _run("ui", NEW_COMMIT, "passed", "02"),
        ],
    )
    return tmp_path


def _command(root, *args):
    return CliRunner().invoke(cli, ["--root", str(root), *args])


def _trace(root, start, *options):
    result = _command(root, "trace", start, *options, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_trace_joins_graphs_without_losing_relation_or_provenance(project):
    result = _trace(project, "REQ-01")
    nodes = {node["id"]: node for node in result["nodes"]}
    assert {"AC-01", "action:reserve", "py:stock.debit", "test:unit"} <= nodes.keys()
    assert "action:proposal" not in nodes
    assert "TEST:tests/mention.py" not in nodes
    assert "AC-99" not in nodes
    assert nodes["action:reserve"]["line_number"] == 20
    same_pair = {
        (edge["relation_type"], edge["line_number"], edge["provider"])
        for edge in result["edges"]
        if (edge["source_id"], edge["target_id"]) == ("action:reserve", "AC-01")
    }
    assert same_pair == {
        ("IMPLEMENTS", 25, "implementation"), ("IMPLEMENTS", 31, "implementation"),
        ("DEPENDS_ON", 25, "implementation"), ("IMPLEMENTS", 25, "ci"),
    }

    reverse = _trace(project, "py:stock.debit", "--direction", "both", "--depth", "3",
                     "--relation", "CONTAINS,CALLS,IMPLEMENTS")
    assert {edge["relation_type"] for edge in reverse["edges"]} == {"CALLS", "IMPLEMENTS", "CONTAINS"}
    assert "REQ-01" in {node["id"] for node in reverse["nodes"]}
    incoming = _trace(project, "AC-01", "--direction", "incoming", "--depth", "1",
                      "--relation", "CANDIDATE_TRACE")
    assert [(edge["source_id"], edge["target_id"]) for edge in incoming["edges"]] == [
        ("action:proposal", "AC-01")
    ]
    outgoing = _trace(project, "AC-01", "--direction", "outgoing", "--relation", "IMPLEMENTS")
    assert not outgoing["edges"]


def test_trace_keeps_declared_test_coverage_and_commit_results_separate(project):
    result = _trace(project, "REQ-01", "--commit", NEW_COMMIT)
    tests = {item["test_id"]: item for item in result["tests"]}
    assert tests["test:unit"]["status"] == "failed"
    assert tests["test:unit"]["level"] == "unit"
    assert tests["test:unit"]["verifies"] == ["AC-01"]
    assert tests["test:unit"]["latest_run"]["evidence"] == "reports/02.xml"
    assert tests["test:unit"]["latest_run"]["finished_at"] == "2026-01-02T08:00:00.000000+00:00"
    assert tests["test:headless"]["status"] == "skipped"
    assert tests["test:ui"]["status"] == "passed"
    assert tests["test:other"]["status"] == "not_run"
    assert tests["TEST:tests/legacy.py"]["latest_run"] is None
    assert result["test_status_counts"] == {
        "passed": 1, "failed": 1, "skipped": 1, "error": 0, "not_run": 2,
    }
    older = {item["test_id"]: item["status"] for item in
             _trace(project, "AC-01", "--commit", OLD_COMMIT)["tests"]}
    assert older["test:unit"] == "passed"
    assert older["test:ui"] == "not_run"


def test_matrix_exposes_provider_of_each_parallel_assertion(project):
    result = _command(project, "matrix", "--relation", "IMPLEMENTS")
    assert result.exit_code == 0, result.output
    entries = json.loads(result.stdout)["matrix"]["entries"]
    assert len(entries) == 1
    assert entries[0]["count"] == 3
    assert [(edge["line_number"], edge["provider"]) for edge in entries[0]["evidence"]] == [
        (25, "ci"), (25, "implementation"), (31, "implementation"),
    ]


def test_mcp_trace_uses_the_same_provider_and_run_projection(project):
    pytest.importorskip("mcp")
    from graph_ba.mcp_server import ba_trace

    result = ba_trace("AC-01", root=str(project), relations=["VERIFIES"], commit=NEW_COMMIT)
    assert result["test_status_counts"]["failed"] == 1
    assert {edge["relation_type"] for edge in result["edges"]} == {"VERIFIES"}


def test_auto_import_tracks_provider_replacements_and_removals(project):
    assert _trace(project, "AC-01")["test_status_counts"]["failed"] == 1
    path = _write_export(project, "ci", test_runs=[_run("unit", NEW_COMMIT, "passed", "03")])
    assert _trace(project, "AC-01")["test_status_counts"]["passed"] == 1
    path.unlink()
    assert _trace(project, "AC-01")["test_status_counts"]["not_run"] == 5


def test_invalid_provider_import_keeps_previous_graph(project):
    assert _trace(project, "AC-01")["test_status_counts"]["passed"] == 1
    _write_export(project, "invalid", test_runs=[_run("missing", NEW_COMMIT, "passed", "02")])
    rejected = _command(project, "import")
    assert rejected.exit_code == 1
    assert "undefined TEST node 'test:missing'" in rejected.output

    previous = CliRunner().invoke(cli, [
        "--root", str(project), "--no-auto-import", "trace", "AC-01", "--json",
    ])
    assert previous.exit_code == 0
    assert json.loads(previous.stdout)["test_status_counts"]["passed"] == 1

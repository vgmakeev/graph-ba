import json

from graph_ba.class_matrix import (
    class_direction_conflicts,
    load_class_matrix_policy,
)


def test_matrix_allows_one_cross_class_direction_and_self_or_symmetric_exceptions(tmp_path):
    graphba = tmp_path / ".graphba"
    graphba.mkdir()
    (graphba / "artifact-class-matrix.json").write_text(
        json.dumps(
            {
                "policy": {"enforce": True},
                "entries": [
                    {
                        "source_type": "FLOW",
                        "relation": "CONTAINS",
                        "target_type": "AC",
                    },
                    {
                        "source_type": "AC",
                        "relation": "TRACES_TO",
                        "target_type": "FLOW",
                    },
                    {
                        "source_type": "AC",
                        "relation": "SUPERSEDES",
                        "target_type": "AC",
                    },
                    {
                        "source_type": "RULE",
                        "relation": "CONFLICTS_WITH",
                        "target_type": "AC",
                    },
                    {
                        "source_type": "AC",
                        "relation": "CONFLICTS_WITH",
                        "target_type": "RULE",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    conflicts = class_direction_conflicts(load_class_matrix_policy(tmp_path))

    assert conflicts == [
        {
            "classes": ["AC", "FLOW"],
            "orientations": [
                {
                    "source_type": "AC",
                    "target_type": "FLOW",
                    "relations": ["TRACES_TO"],
                },
                {
                    "source_type": "FLOW",
                    "target_type": "AC",
                    "relations": ["CONTAINS"],
                },
            ],
        }
    ]


def test_preferred_direction_suppresses_adapter_reverse_edge(tmp_path):
    graphba = tmp_path / ".graphba"
    graphba.mkdir()
    (graphba / "artifact-class-matrix.json").write_text(
        json.dumps(
            {
                "policy": {
                    "enforce": True,
                    "class_directions": [
                        {"source_type": "REACT_COMPONENT", "target_type": "SCR"}
                    ],
                },
                "entries": [
                    {
                        "source_type": "SCR",
                        "relation": "CONTAINS",
                        "target_type": "REACT_COMPONENT",
                    },
                    {
                        "source_type": "REACT_COMPONENT",
                        "relation": "RENDERS",
                        "target_type": "SCR",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    policy = load_class_matrix_policy(tmp_path)

    assert policy.suppresses_reverse("SCR", "REACT_COMPONENT") is True
    assert policy.suppresses_reverse("REACT_COMPONENT", "SCR") is False

import os
import sys
import time

from graph_ba.config import load_config
from graph_ba.provider_refresh import refresh_provider_inputs


def test_configured_provider_refresh_runs_only_when_output_is_missing(tmp_path):
    (tmp_path / "graph-ba.toml").write_text(
        """
[scan]
dirs = ["docs"]

[types.AC]
label = "Acceptance"
ref = '(AC-\\d+)'
classify = 'AC-\\d+'

[[providers.refresh]]
name = "observed"
command = ["PYTHON", "-c", "from pathlib import Path; Path('reports/observed.md').parent.mkdir(parents=True, exist_ok=True); Path('reports/observed.md').write_text('ok')"]
inputs = ["docs"]
outputs = ["reports/observed.md"]
        """.replace("PYTHON", sys.executable).strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    config = load_config(tmp_path)

    first = refresh_provider_inputs(tmp_path, config)
    second = refresh_provider_inputs(tmp_path, config)
    future = time.time() + 5
    os.utime(tmp_path / "docs", (future, future))
    (tmp_path / "docs" / "source.md").write_text("changed", encoding="utf-8")
    os.utime(tmp_path / "docs" / "source.md", (future, future))
    third = refresh_provider_inputs(tmp_path, config)

    assert first["pass"] is True
    assert first["providers"][0]["status"] == "refreshed"
    assert second["providers"][0]["status"] == "current"
    assert third["providers"][0]["status"] == "refreshed"
    assert third["providers"][0]["stale"] is True

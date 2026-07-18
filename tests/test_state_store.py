from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_save_state_preserves_updates_from_multiple_processes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    script = """
import os
from app.state_store import save_state

key = os.environ["STATE_TEST_KEY"]
for value in range(20):
    save_state({key: value})
"""
    processes = []
    for index in range(6):
        env = os.environ.copy()
        env["APP_CONFIG_PATH"] = str(config_path)
        env["STATE_TEST_KEY"] = f"worker_{index}"
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
            )
        )

    for process in processes:
        assert process.wait(timeout=30) == 0

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert {state[f"worker_{index}"] for index in range(6)} == {19}
    assert (tmp_path / "state.json.lock").exists()

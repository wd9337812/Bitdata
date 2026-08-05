from __future__ import annotations

from pathlib import Path

from scripts.phase1_daily_status import read_records


def test_read_records(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_text(
        '{"type":"new_listing","symbol":"AUSDT","raw_return_pct":1.0}\n'
        '{"type":"volume_breakout","symbol":"BUSDT","raw_return_pct":-0.5}\n',
        encoding="utf-8",
    )
    records = read_records(path)
    assert len(records) == 2
    assert records[0]["type"] == "new_listing"
    assert read_records(tmp_path / "missing.jsonl") == []

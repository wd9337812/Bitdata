from pathlib import Path

from scripts.build_binance_um_point_in_time_funding import (
    checksum_from_text,
    load_tasks,
)


def test_checksum_from_text() -> None:
    checksum = "a" * 64
    assert checksum_from_text(f"{checksum}  BTCUSDT.zip\n") == checksum


def test_load_tasks_uses_monthly_and_daily_universe(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        """
        {
          "symbols": [
            {
              "symbol": "OLDUSDT",
              "archive_months": ["2026-01", "2026-02"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "daily_extension_manifest.json").write_text(
        """
        {
          "symbols": [
            {
              "symbol": "OLDUSDT",
              "archive_dates": ["2026-07-01"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    tasks = load_tasks(tmp_path, include_daily=True)
    assert [(task.frequency, task.period) for task in tasks] == [
        ("monthly", "2026-01"),
        ("monthly", "2026-02"),
        ("daily", "2026-07-01"),
    ]
    assert all("OLDUSDT" in task.url for task in tasks)

    monthly_only = load_tasks(tmp_path)
    assert all(task.frequency == "monthly" for task in monthly_only)

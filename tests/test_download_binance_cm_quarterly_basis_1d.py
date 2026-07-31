import io
import zipfile

import pandas as pd

from scripts import download_binance_cm_quarterly_basis_1d as subject


def archive(rows: list[list[object]], header: bool) -> bytes:
    payload = io.BytesIO()
    frame = pd.DataFrame(rows, columns=subject.KLINE_COLUMNS)
    with zipfile.ZipFile(payload, "w") as target:
        target.writestr("sample.csv", frame.to_csv(index=False, header=header))
    return payload.getvalue()


def test_task_urls_and_targets(tmp_path) -> None:
    index = subject.Task("index", "BTCUSD", "2022-01")
    delivery = subject.Task("delivery", "BTCUSD_220325", "2022-01")

    assert "/indexPriceKlines/BTCUSD/1d/" in index.url
    assert "/klines/BTCUSD_220325/1d/" in delivery.url
    assert delivery.target(tmp_path) == tmp_path / "delivery" / "BTCUSD_220325" / "2022-01.parquet"


def test_tasks_include_index_and_current_quarter_contracts() -> None:
    result = subject.tasks(["BTC"], "2022-01", "2022-03")

    assert subject.Task("index", "BTCUSD", "2022-01") in result
    assert subject.Task("delivery", "BTCUSD_220325", "2022-01") in result
    assert subject.Task("delivery", "BTCUSD_220325", "2022-03") in result
    assert subject.Task("delivery", "BTCUSD_220624", "2022-01") not in result


def test_read_archive_supports_headerless_binance_history() -> None:
    row = [1640995200000, 100, 110, 90, 105, 1, 1641081599999, 1000, 3, 0.5, 500, 0]
    result = subject.read_archive(archive([row], header=False))

    assert list(result.columns) == ["open_time", "open", "high", "low", "close", "close_time"]
    assert result.iloc[0].close == 105

import hashlib
import io
import zipfile

from scripts import download_binance_um_quarter_hour_5m as subject


def archive(body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr("sample.csv", body)
    return buffer.getvalue()


def test_task_uses_official_monthly_kline_paths(tmp_path) -> None:
    task = subject.Task("BTCUSDT", "2025-01")
    assert task.url.endswith("/BTCUSDT/5m/BTCUSDT-5m-2025-01.zip")
    assert task.checksum_url.endswith(".zip.CHECKSUM")
    assert task.target(tmp_path).name == "2025-01.parquet"


def test_default_symbols_match_published_contract_set() -> None:
    assert subject.DEFAULT_SYMBOLS == (
        "BTCUSDT",
        "ETHUSDT",
        "XRPUSDT",
        "SOLUSDT",
        "DOGEUSDT",
        "ADAUSDT",
    )


def test_parse_checksum_rejects_malformed_payload() -> None:
    digest = hashlib.sha256(b"payload").hexdigest()
    assert subject.parse_checksum(f"{digest}  sample.zip") == digest
    try:
        subject.parse_checksum("not-a-checksum sample.zip")
    except ValueError:
        pass
    else:
        raise AssertionError("Malformed checksums must be rejected")


def test_read_archive_retains_taker_buy_quote_volume() -> None:
    frame = subject.read_archive(
        archive(
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore\n"
            "1000,1,2,0.5,1.5,10,1299,100,60,4,40,0\n"
        )
    )
    assert list(frame.columns) == list(subject.RESEARCH_COLUMNS)
    assert frame.loc[0, "taker_buy_quote_volume"] == 40


def test_read_archive_accepts_headerless_binance_files() -> None:
    frame = subject.read_archive(
        archive("1640995200000,1,2,0.5,1.5,10,1640995499999,100,60,4,40,0\n")
    )
    assert len(frame) == 1
    assert frame.loc[0, "open_time"] == 1640995200000

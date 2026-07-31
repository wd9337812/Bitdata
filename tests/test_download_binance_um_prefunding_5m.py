import io
import zipfile

from scripts import download_binance_um_prefunding_5m as subject


def archive(name: str, body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr(name, body)
    return buffer.getvalue()


def test_task_urls_and_targets(tmp_path):
    premium = subject.Task("premiumIndexKlines", "BTCUSDT", "2025-01")
    funding = subject.Task("fundingRate", "BTCUSDT", "2025-01")
    assert "/premiumIndexKlines/BTCUSDT/5m/" in premium.url
    assert premium.target(tmp_path).name == "2025-01.parquet"
    assert "/fundingRate/BTCUSDT/" in funding.url
    assert "5m" not in funding.filename


def test_read_archive_keeps_only_point_in_time_columns():
    kline = subject.read_archive(
        "premiumIndexKlines",
        archive(
            "sample.csv",
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore\n"
            "1000,0.1,0.2,0.0,0.15,0,1299,0,1,0,0,0\n",
        ),
    )
    assert list(kline.columns) == [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "close_time",
        "quote_volume",
    ]
    funding = subject.read_archive(
        "fundingRate",
        archive(
            "funding.csv",
            "calc_time,funding_interval_hours,last_funding_rate\n1000,8,0.0001\n",
        ),
    )
    assert funding.last_funding_rate.iloc[0] == 0.0001


def test_read_archive_accepts_old_headerless_kline_archives():
    kline = subject.read_archive(
        "premiumIndexKlines",
        archive(
            "sample.csv",
            "1640995200000,1,2,0.5,1.5,10,1640995499999,100,60,4,40,0\n",
        ),
    )

    assert len(kline) == 1
    assert kline.loc[0, "open_time"] == 1640995200000
    assert kline.loc[0, "close"] == 1.5

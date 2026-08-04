from __future__ import annotations

import requests

from scripts.download_s0_cross_year_1m import (
    archive_url,
    download_month,
)


def test_archive_url_has_1m_segment() -> None:
    url = archive_url("BTCUSDT", "2021-06")
    assert url == (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "BTCUSDT/1m/BTCUSDT-1m-2021-06.zip"
    )


def test_download_month_writes_archive_when_ok(
    tmp_path,
    monkeypatch,
) -> None:
    payload = b"fake-zip"

    class FakeResponse:
        content = payload

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        "scripts.download_s0_cross_year_1m.request_with_retry",
        lambda *args, **kwargs: FakeResponse(),
    )
    session = requests.Session()
    status = download_month(
        session,
        "BTCUSDT",
        "2020-01",
        retries=1,
        checksum=False,
        output=tmp_path,
    )
    assert status == ("2020-01", "ok")
    target = tmp_path / "raw" / "BTCUSDT" / "BTCUSDT-1m-2020-01.zip"
    assert target.read_bytes() == payload


def test_download_month_records_missing_on_404(
    tmp_path,
    monkeypatch,
) -> None:
    def raise_404(*args, **kwargs):
        response = requests.Response()
        response.status_code = 404
        raise requests.exceptions.HTTPError(response=response)

    monkeypatch.setattr(
        "scripts.download_s0_cross_year_1m.request_with_retry",
        raise_404,
    )
    session = requests.Session()
    status = download_month(
        session,
        "SOLUSDT",
        "2019-09",
        retries=1,
        checksum=False,
        output=tmp_path,
    )
    assert status == ("2019-09", "missing")

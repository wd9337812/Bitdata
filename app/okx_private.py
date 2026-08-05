from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests


@dataclass(frozen=True)
class OkxPrivateConfig:
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    base_url: str = "https://www.okx.com"
    dry_run: bool = True
    timeout: int = 15

    def validate(self) -> None:
        if not self.api_key or not self.api_secret or not self.passphrase:
            raise ValueError(
                "OKX api_key, api_secret and passphrase are all required"
            )


class OkxPrivateClient:
    """OKX v5 signed client. No live orders unless dry_run=False."""

    def __init__(self, config: OkxPrivateConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def _sign(
        self,
        timestamp: str,
        method: str,
        request_path: str,
        body: str,
    ) -> str:
        prehash = timestamp + method + request_path + body
        digest = hmac.new(
            self.config.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        timestamp = (
            datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z"
        )
        return {
            "OK-ACCESS-KEY": self.config.api_key,
            "OK-ACCESS-SIGN": self._sign(
                timestamp, method, request_path, body
            ),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.config.passphrase,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        body: str = "",
    ) -> dict[str, Any]:
        self.config.validate()
        headers = self._headers(method, path, body)
        url = f"{self.config.base_url}{path}"
        response = self.session.request(
            method,
            url,
            headers=headers,
            data=body or None,
            timeout=self.config.timeout,
        )
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                f"OKX non-JSON response http={response.status_code}: "
                f"{response.text[:200]}"
            )
        if response.status_code != 200 or payload.get("code") != "0":
            raise RuntimeError(
                f"OKX error http={response.status_code} "
                f"code={payload.get('code')} "
                f"msg={payload.get('msg')}"
            )
        return payload

    def balance(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v5/account/balance")
        return payload["data"]

    def positions(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v5/account/positions")
        return payload["data"]

    def set_leverage(
        self,
        symbol: str,
        leverage: int,
        mgn_mode: str = "cross",
    ) -> dict[str, Any]:
        body = (
            '{"instId":"'
            + symbol
            + '","lever":"'
            + str(leverage)
            + '","mgnMode":"'
            + mgn_mode
            + '"}'
        )
        return self._request("POST", "/api/v5/account/set-leverage", body)

    def place_market_order(
        self,
        symbol: str,
        side: str,
        size: float,
        mgn_mode: str = "cross",
    ) -> dict[str, Any]:
        """Place a USDT-SWAP market order. Refuses to run in dry-run mode."""
        if self.config.dry_run:
            raise RuntimeError(
                "dry_run=True: refusing to place a live OKX order"
            )
        body = (
            '{"instId":"'
            + symbol
            + '","tdMode":"'
            + mgn_mode
            + '","side":"'
            + side
            + '","ordType":"market","sz":"'
            + str(size)
            + '"}'
        )
        return self._request("POST", "/api/v5/trade/order", body)


def load_okx_config(
    dry_run: bool = True,
    env: dict[str, str] | None = None,
    credentials_path: str | None = None,
) -> OkxPrivateConfig:
    """Load OKX credentials from environment or a chmod-600 file."""
    env = env if env is not None else dict(os.environ)
    key = env.get("OKX_API_KEY", "")
    secret = env.get("OKX_API_SECRET", "")
    passphrase = env.get("OKX_API_PASSPHRASE", "")
    if (not key or not secret or not passphrase) and credentials_path:
        path = credentials_path if isinstance(credentials_path, str) else str(credentials_path)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    field, value = line.split("=", 1)
                    field = field.strip()
                    value = value.strip().strip("'\"")
                    if field == "OKX_API_KEY" and not key:
                        key = value
                    elif field == "OKX_API_SECRET" and not secret:
                        secret = value
                    elif field == "OKX_API_PASSPHRASE" and not passphrase:
                        passphrase = value
    return OkxPrivateConfig(
        api_key=key,
        api_secret=secret,
        passphrase=passphrase,
        dry_run=dry_run,
    )

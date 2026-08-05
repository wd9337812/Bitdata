from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.okx_private import OkxPrivateClient, OkxPrivateConfig  # noqa: E402


def main() -> None:
    key = os.environ.get("OKX_API_KEY", "")
    secret = os.environ.get("OKX_API_SECRET", "")
    passphrase = os.environ.get("OKX_API_PASSPHRASE", "")
    config = OkxPrivateConfig(
        api_key=key,
        api_secret=secret,
        passphrase=passphrase,
        dry_run=True,
    )
    client = OkxPrivateClient(config)
    try:
        balances = client.balance()
        total_eq = 0.0
        details = []
        for account in balances:
            total_eq = float(account.get("totalEq", 0.0))
            for item in account.get("details", []):
                details.append(
                    {
                        "ccy": item.get("ccy"),
                        "eq": item.get("eq"),
                        "availEq": item.get("availEq"),
                    }
                )
        print(
            json.dumps(
                {
                    "auth": "ok",
                    "totalEq": total_eq,
                    "details": details,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except Exception as exc:
        print(f"AUTH_ERROR: {exc}", flush=True)


if __name__ == "__main__":
    main()

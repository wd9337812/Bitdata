from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.binance_client import BinanceFuturesClient
from app.trading_engine import summarize_account
from app.user_stream import read_user_stream_snapshot, seed_user_account


def _age_seconds(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except ValueError:
        return None


def canonical_account_projection(
    client: BinanceFuturesClient,
    *,
    websocket_max_age_seconds: int = 45,
    force_rest: bool = False,
) -> dict[str, Any]:
    """Return one timestamped account view, preferring the private stream."""
    snapshot = read_user_stream_snapshot()
    stream_age = _age_seconds(snapshot.get("updated_at"))
    stream_live = bool(snapshot.get("connected")) and stream_age is not None and stream_age <= websocket_max_age_seconds
    raw_account = (
        dict(snapshot.get("account") or {})
        if snapshot.get("initialized") and stream_live and not force_rest
        else None
    )
    source = "private_websocket"
    as_of = snapshot.get("account_updated_at")

    if raw_account is None:
        raw_account = client.account_live()
        seed_user_account(raw_account)
        snapshot = read_user_stream_snapshot()
        source = "binance_rest"
        as_of = snapshot.get("account_updated_at")

    account = summarize_account(raw_account)
    age = _age_seconds(as_of)
    positions = list(account.get("positions") or [])
    return {
        "account": account,
        "source": source,
        "as_of": as_of,
        "age_seconds": age,
        "stream_age_seconds": stream_age,
        "stale": source == "private_websocket" and not stream_live,
        "position_count": len(positions),
        "revision": int(snapshot.get("account_update_ms") or 0),
    }

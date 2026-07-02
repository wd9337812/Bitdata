from __future__ import annotations

from app.trading_engine import build_position_rotation_plan


def base_config() -> dict:
    return {
        "position_rotation_enabled": True,
        "tournament_rotation_enabled": True,
        "tournament_rotation_min_new_score": 95,
        "tournament_rotation_min_score_delta": 12,
        "rotation_min_cost_ratio": 8,
        "rotation_unknown_position_score": 75,
        "rotation_keep_winner_profit_pct": 3,
        "rotation_max_current_loss_pct": 6,
    }


def test_rotation_allows_stronger_candidate_to_replace_weak_position():
    candidate = {"symbol": "NEWUSDT", "direction": "SHORT", "score": 104, "cost_ratio": 10}
    scan = {
        "mode": {"mode": "tournament"},
        "candidates": [
            candidate,
            {"symbol": "OLDUSDT", "direction": "LONG", "score": 86, "cost_ratio": 6},
        ],
    }
    account = {
        "positions": [
            {
                "symbol": "OLDUSDT",
                "positionSide": "LONG",
                "positionAmt": "10",
                "positionInitialMargin": "50",
                "unrealizedProfit": "-0.5",
            }
        ]
    }

    plan = build_position_rotation_plan(candidate, scan, base_config(), {}, account)

    assert plan["allowed"] is True
    assert plan["from"]["symbol"] == "OLDUSDT"
    assert plan["to"]["symbol"] == "NEWUSDT"


def test_rotation_keeps_winning_position():
    candidate = {"symbol": "NEWUSDT", "direction": "SHORT", "score": 120, "cost_ratio": 20}
    scan = {
        "mode": {"mode": "tournament"},
        "candidates": [
            candidate,
            {"symbol": "OLDUSDT", "direction": "LONG", "score": 90, "cost_ratio": 6},
        ],
    }
    account = {
        "positions": [
            {
                "symbol": "OLDUSDT",
                "positionSide": "LONG",
                "positionAmt": "10",
                "positionInitialMargin": "50",
                "unrealizedProfit": "2.0",
            }
        ]
    }

    plan = build_position_rotation_plan(candidate, scan, base_config(), {}, account)

    assert plan["allowed"] is False
    assert plan["reason"] == "current_position_is_winner"

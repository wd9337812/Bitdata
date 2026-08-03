from __future__ import annotations

from copy import deepcopy

from scripts.audit_s0_bnb_risk_budget import (
    promotion_accepted,
    select_risk_budget,
)


def _period(
    *,
    return_pct: float,
    profit_factor: float,
    drawdown: float,
    years: tuple[str, ...] = ("2020", "2021", "2022"),
) -> dict:
    metric = {
        "return_pct": return_pct,
        "profit_factor": profit_factor,
        "max_drawdown_pct": drawdown,
        "hard_stopped": False,
    }
    return {
        "overall": dict(metric),
        "annual": {
            year: {
                **metric,
                "return_pct": max(0.1, return_pct / max(1, len(years))),
            }
            for year in years
        },
    }


def test_selection_promotes_20_and_rejects_faster_but_unstable_risks() -> None:
    development = {
        0.15: _period(return_pct=430, profit_factor=1.44, drawdown=-48),
        0.20: _period(return_pct=570, profit_factor=1.35, drawdown=-59),
        0.25: _period(return_pct=615, profit_factor=1.28, drawdown=-58),
        0.30: _period(return_pct=700, profit_factor=1.24, drawdown=-72),
    }
    development[0.20]["annual"]["2020"]["return_pct"] = -19.4
    development[0.20]["annual"]["2020"]["profit_factor"] = 0.81

    assert select_risk_budget(development) == 0.20


def test_promotion_requires_positive_validation_and_all_delay_stress_periods() -> None:
    current_oos = _period(
        return_pct=117,
        profit_factor=1.99,
        drawdown=-25,
        years=("2024", "2025", "2026"),
    )["overall"]
    validation = _period(
        return_pct=58,
        profit_factor=2.08,
        drawdown=-13,
        years=("2023",),
    )
    stress = {
        str(delay): {
            "stress_2x": _period(
                return_pct=180 - delay * 10,
                profit_factor=1.96 - delay * 0.04,
                drawdown=-32 - delay * 0.5,
                years=("2024", "2025", "2026"),
            )
        }
        for delay in (0, 1, 2, 4)
    }

    assert promotion_accepted(
        selected_risk=0.20,
        current_oos=current_oos,
        selected_validation=validation,
        selected_stress=stress,
    )

    failed = deepcopy(stress)
    failed["4"]["stress_2x"]["annual"]["2025"]["return_pct"] = -0.1
    assert not promotion_accepted(
        selected_risk=0.20,
        current_oos=current_oos,
        selected_validation=validation,
        selected_stress=failed,
    )

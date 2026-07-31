from scripts.validate_s0_slow_momentum_minute import qualifies


def _scenario(pf: float, net: float, trades: int = 20) -> dict:
    windows = {
        name: {"metrics": {"trades": trades, "profit_factor": pf, "net_pct_points": net}}
        for name in ("validation_apr_may", "test_june", "final_july")
    }
    return {"windows": windows}


def test_qualification_requires_every_scenario_and_window() -> None:
    assert qualifies({"base": _scenario(1.1, 1.0)})
    assert not qualifies({"base": _scenario(1.1, 1.0, trades=19)})
    assert not qualifies({"base": _scenario(0.99, 1.0)})

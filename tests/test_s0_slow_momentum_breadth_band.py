from scripts.benchmark_s0_slow_momentum_breadth_band import cohort, qualifies


def _metrics(trades=20, pf=1.2, net=1.0, symbols=20):
    return {
        "trades": trades,
        "symbols": symbols,
        "profit_factor": pf,
        "net_pct_points": net,
    }


def _reports():
    base = {
        "overall": _metrics(trades=40, net=1.0),
        "windows": {name: _metrics() for name in ("validation_apr_may", "test_june", "final_july")},
        "cohorts": {"blind": _metrics()},
        "without_top_3_symbols": _metrics(),
    }
    return {"base": base, "stress": {"overall": _metrics()}}


def test_cohort_is_stable():
    assert cohort("BTCUSDT") == cohort("BTCUSDT")


def test_qualification_requires_all_windows_and_stress():
    reports = _reports()
    assert qualifies(reports)
    reports["base"]["windows"]["test_june"] = _metrics(net=-1)
    assert not qualifies(reports)
    reports = _reports()
    reports["stress"]["overall"] = _metrics(net=-1)
    assert not qualifies(reports)


def test_qualification_requires_breadth_of_sample():
    reports = _reports()
    reports["base"]["overall"] = _metrics(trades=39, net=10)
    assert not qualifies(reports)

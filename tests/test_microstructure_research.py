from app.microstructure_research import STRATEGY_FAMILY, STRATEGY_VERSION, build_research_candidates, evaluate_candidate


def _candidate(direction: str = "LONG") -> dict:
    return {
        "symbol": "SOLUSDT",
        "direction": direction,
        "signal": {"signal": direction, "last_price": 100.0},
        "ticker": {"last": 100.0},
    }


def _depth(*, flow_imbalance: float = 0.3, spread_pct: float = 0.02) -> dict:
    return {
        "bids": [[99.99, 20], [99.98, 10]],
        "asks": [[100.01, 8], [100.02, 8]],
        "spread_pct": spread_pct,
        "depth_notional": 1500.0,
        "trade_flow_notional": 20000.0,
        "trade_flow_imbalance": flow_imbalance,
        "microprice_edge_bps": 1.0,
    }


def test_microstructure_candidate_is_paper_only(monkeypatch):
    monkeypatch.setattr("app.microstructure_research.stream_depth", lambda *_args, **_kwargs: _depth())
    candidate, status = evaluate_candidate(_candidate(), {})

    assert status["eligible"] is True
    assert candidate is not None
    assert candidate["passed"] is False
    assert candidate["strategy_family"] == STRATEGY_FAMILY
    assert candidate["strategy_version"] == STRATEGY_VERSION
    assert candidate["strategy_role"] == "research"
    assert candidate["signal"]["stop"] < candidate["signal"]["last_price"] < candidate["signal"]["take_profit"]


def test_microstructure_rejects_opposing_aggressive_flow(monkeypatch):
    monkeypatch.setattr("app.microstructure_research.stream_depth", lambda *_args, **_kwargs: _depth(flow_imbalance=-0.3))
    candidate, status = evaluate_candidate(_candidate(), {})

    assert candidate is None
    assert status["eligible"] is False
    assert "主动成交" in status["reason"]


def test_research_limits_unique_symbol_direction_pairs(monkeypatch):
    monkeypatch.setattr("app.microstructure_research.stream_depth", lambda *_args, **_kwargs: _depth())
    rows, status = build_research_candidates([_candidate(), _candidate(), _candidate("SHORT")], {"microstructure_research_candidate_limit": 1})

    assert len(rows) == 1
    assert status["inspected"] == 1
    assert status["qualified"] == 1

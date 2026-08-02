from scripts.benchmark_s0_market_tsmom_consensus_protection import qualifies_oos


def test_oos_requires_positive_pf_in_every_year() -> None:
    annual = {
        year: {"net_return": 0.1, "profit_factor": 1.2}
        for year in ("2024", "2025", "2026")
    }
    result = {
        "overall": {"profit_factor": 1.2, "max_drawdown": -0.5},
        "annual": annual,
    }

    assert qualifies_oos(result)
    result["annual"]["2025"]["profit_factor"] = 0.9
    assert not qualifies_oos(result)

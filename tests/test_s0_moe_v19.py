from __future__ import annotations

from scripts import train_s0_moe_v1_9 as v19


def test_v19_isolated_model_version_and_vps_history():
    assert v19.MODEL_VERSION == "s0_binance_moe_v1_9"
    assert v19.DEFAULT_VPS_HISTORY.name == "vps_history_20260730_v5"


def test_v19_reuses_audited_v18_pipeline():
    assert v19.v18.train.__module__ == "scripts.train_s0_moe_v1_8"

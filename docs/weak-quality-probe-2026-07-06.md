# Weak Quality Probe Layer

## Background

The extreme sprint mode was still rejecting some very high candidate-score symbols when their symbol quality stayed in the observe pool. That protected the account, but it also meant the bot could miss useful small-account learning samples.

This iteration adds a separate weak-quality probe path. It is not a full-size entry. It is a small live sample layer for cases where the signal is strong, fees are covered, and the symbol is not obviously untradeable.

## Scope

This layer only applies in `extreme_sprint`.

It can activate when:

- The candidate has a real `LONG` or `SHORT` signal.
- The symbol quality pool is `observe` or `observe_hot`.
- Candidate score is at least `weak_quality_probe_min_candidate_score`, default `95`.
- Quality score is at least `weak_quality_probe_min_quality_score`, default `58`.
- Expected profit to cost ratio is at least `weak_quality_probe_min_cost_ratio`, default `12`.
- Recent profit factor is at least `weak_quality_probe_min_profit_factor`, default `0.55`.
- Recent net result is not worse than `weak_quality_probe_min_net_pct`, default `-8%`.
- Spread and depth pass the weak probe limits.

If these conditions fail, the dashboard reason shows `弱质量试探未通过` with the first blockers.

## Position Sizing

Weak-quality probes use a separate multiplier instead of the normal extreme sprint high-score multiplier.

Default multiplier ladder:

- Quality score below `65`: `0.18x`.
- Quality score at least `65`: `0.25x`.
- Quality score at least `72`: `0.35x`.

Extra damping:

- Profit factor below `0.8`: multiply by `0.7`.
- Backtest net result below `0`: multiply by `0.8`.
- Depth less than two times the minimum depth: multiply by `0.7`.

This keeps weak-quality entries as learning samples, not full conviction trades.

## Protection

Weak-quality probes use faster protection parameters:

- Stop loss: `weak_quality_probe_stop_atr`, default `0.55`.
- Take profit: `weak_quality_probe_take_profit_atr`, default `0.75`.
- Max hold bars: `weak_quality_probe_max_hold_bars`, default `3`.

The goal is to learn quickly and avoid letting a weak-quality sample occupy the only live slot for too long.

## Rotation

Position rotation now treats `weak_quality_probe` as a probe entry type. A stronger new signal can replace it with a lower required score delta than a normal standard position, while still respecting the existing cost and protection checks.

## Verification

Added scanner tests for:

- Allowing an observe-pool symbol with strong candidate score and acceptable weak-quality metrics to pass as `weak_quality_probe`.
- Blocking a similar symbol when profit factor is too weak.


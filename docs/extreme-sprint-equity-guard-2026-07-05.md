# Extreme Sprint Equity Guard Baseline

## Why

The previous equity guard used the global `equity_high_watermark` for every mode. That made `extreme_sprint` inherit drawdown from older modes. If the bot had previously reached 86U and extreme mode started near 60U, the guard immediately scaled risk to `0.2x`, even though the new mode had not yet had a fair run.

## Change

`extreme_sprint` now uses its own mode-level equity baseline:

- `extreme_sprint_start_equity`: equity when the current extreme run starts.
- `extreme_sprint_equity_high_watermark`: highest equity reached during the current extreme run.
- `equity_guard_mode`: the mode that owns the current guard baseline.

The global `equity_high_watermark` is still maintained for account history and non-extreme modes.

## Behavior

- When the bot enters `extreme_sprint` and no active extreme baseline exists, the system initializes the extreme baseline from current futures account equity.
- While `extreme_sprint` remains active, the mode high watermark only moves upward.
- The equity guard still scales or pauses risk, but the drawdown is measured from the extreme-mode high watermark instead of the older global high watermark.
- Restarting services does not reset the extreme baseline if `equity_guard_mode` is already `extreme_sprint`.

## Deployment Note

For the live VPS update, initialize the extreme baseline to the current account equity before restarting the runner if there are no active positions or orders. This avoids carrying the previous strategy's high watermark into the new extreme-mode run.

# ReduceOnly 平仓竞态恢复

日期：2026-07-07

## 背景

实盘极限模式运行时出现 Binance 错误：

`{"code":-2022,"msg":"ReduceOnly Order is rejected."}`

旧逻辑会把该错误当作通用致命异常，写入 `last_error` 并把机器人状态切到 `paused`。实际排查发现，这类错误常发生在换仓或保护单触发后的竞态场景：系统准备平旧仓，但币安侧旧仓已经被止盈、止损或前序平仓处理掉。

## 本次改动

1. 新增 `is_reduce_only_rejection`，识别 Binance `-2022` / `ReduceOnly Order is rejected`。
2. 换仓平旧仓前，调用实时账户接口二次确认旧仓仍存在。
3. 如果旧仓已不存在，返回 `position_already_closed`，不再发送平仓单。
4. 如果取消保护单后平仓时遇到 `-2022`，再次刷新账户：
   - 若旧仓已不存在，按 `position_already_closed_after_cancel` 处理，继续运行。
   - 若旧仓仍存在，继续抛错，避免误判已平仓。
5. runner 增加兜底：若 `-2022` 漏出且实时账户无任何持仓，记录 warning 并继续运行，不再暂停。

## 安全边界

这次没有把所有 `-2022` 都吞掉。只有在实时账户确认没有对应持仓，或完全没有持仓时，才认为它是可恢复错误。若账户仍有持仓，系统仍会报错，避免带仓失控。

## 测试

新增覆盖：

- `test_reduce_only_rejection_can_be_recovered_when_position_is_gone`
- `test_close_rotation_skips_when_live_position_is_already_gone`
- `test_close_rotation_recovers_reduce_only_when_position_disappears_after_cancel`


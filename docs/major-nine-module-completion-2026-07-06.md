# 九大模块完成留档

日期：2026-07-06

## 结论

本次将 `extreme-rollover-product-plan-2026-07-06.md` 中的九大模块补齐到可验收状态，并新增 `/api/product/completion` 作为后续自检入口。

需要特别说明：运行时动态保护的“真实平仓执行”仍由独立高风险开关控制，默认不开启。系统会计算并记录保护建议；如果要让它自动平仓，需要开启 `dynamic_protection_runtime_trade_enabled`。

## 模块完成情况

1. 目标进度控制器
   - 文件：`app/target.py`
   - Dashboard：`TargetProgressPanel`
   - API：`/api/status.target_progress`

2. 事件驱动机会队列
   - 文件：`app/opportunity_queue.py`
   - WebSocket 异动进入热队列，并影响扫描优先级。
   - Dashboard：扫描漏斗事件队列。

3. 动态止盈止损引擎
   - 文件：`app/protection.py`
   - 新增：`app/runtime_protection.py`
   - 初始保护单已经接入下单；运行时保护会检查快速失效、时间止损、保本和移动止盈条件。

4. 统一仓位引擎
   - 文件：`app/position_sizing.py`
   - 新增 `unified_position_sizing`，统一输出信号、质量、信用、目标、权益保护、行情状态、流动性等倍率。

5. 阶段化策略模式系统
   - 文件：`app/stage_modes.py`
   - S0-S6 阶段映射到推荐模式、风险档位、基础风险字段和最大持仓建议。

6. 扫描漏斗性能升级
   - 文件：`app/scanner.py`
   - 已有召回、粗排、精排、竞价、候选、事件队列和缓存/频控状态展示。

7. 目标曲线 Dashboard
   - 文件：`frontend/src/main.tsx`
   - 总览显示目标进度、当前阶段、九大模块完成度、模拟终值和学习报告摘要。

8. 阶段模拟与回测
   - 文件：`app/stage_simulation.py`
   - API：`/api/simulation/stage`
   - 用当前配置和历史运行记录估算滚仓路径、交易频率、最大回撤、手续费占比和目标缺口。

9. 每日学习报告
   - 文件：`app/learning_report.py`
   - API：`/api/reports/latest`、`/api/reports/daily`
   - 输出到 `data/reports/daily-learning-YYYY-MM-DD.md`。

## 新增配置

- `daily_learning_report_enabled`
- `simulation_trades_per_day`
- `simulation_fee_slippage_pct`
- `simulation_start_equity`
- `dynamic_protection_runtime_enabled`
- `dynamic_protection_runtime_trade_enabled`
- `runtime_protection_max_hold_bars`

## 风控边界

- 初始止盈止损保护已经接入实盘下单。
- 运行时保护默认只检查和记录。
- 自动运行时平仓需要明确开启 `dynamic_protection_runtime_trade_enabled`。
- 每日学习报告不会暴露 API Secret。
- 阶段模拟不是收益保证，只用于比较不同参数组合。

## 测试

新增 `tests/test_major_completion.py` 覆盖：

- 阶段映射。
- 统一仓位倍率输出。
- 运行时保护动作判断。
- 阶段模拟输出。
- 每日报告保存。
- 九大模块完成度汇总。

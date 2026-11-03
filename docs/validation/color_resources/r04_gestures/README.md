# R04 留档：Effects 实例与参数编辑事务（2026-11-03）

## 实现

- **相位完整化**：effects 浏览器滑条接线 interactionStarted → `start` 相位
  （此前只有 change/commit）；LUT 强度滑条 sliderPressed/Released 界定一次
  拖动，spin 键入为离散提交。取消拾色/未确认颜色不发射（天然不入史）。
- **手势事务机**（ColorResourceController）：
  - `start` → `bridge.begin_transaction`（在途手势先落定）；
  - `change` → `bridge.submit_buffered`（service 在 pending 图上执行，
    transient 与图一致，零 command）；中间帧被 schema 拒绝（越界等）只丢
    该帧，手势保持；
  - `commit` → 末次缓冲 + `flush_transaction` → **一个 command 一次 undo**；
  - `cancel` → `GraphTransactionBuffer.cancel()`（新 API：零 command、零
    revision、零历史）；
  - 切节点/clip/scope 触发的水合先自动落定在途手势（不丢已拖数据）。
- **bridge 通用缓冲提交**：`submit_buffered(operation, arguments, tx)` /
  `cancel_transaction()`——service 经 `color_commands._graph_service()` 工厂
  构造（资源权威注入与命令路径同源）。
- **离散操作**：下拉/bool/确认颜色/键入值各一次 command（无手势直发）。
- **异步守卫**：`issue_async_token`（目标+意图时刻 revision）/`verify_async_token`
  ——会话/代际/栈/节点/实例任一失配即丢弃晚到结果（AI bake 管道 R06 接线）。

## 测试（tests/app/test_color_resource_gestures.py，7 项）

- 100 次 valueChange（start/100×change/commit）= 1 command，一次 undo 回默认；
- cancel = 0 command，图字节不变，值回权威；
- select/bool/color 离散各 1 command（3 操作 3 command）；
- 中间帧越界丢弃、手势继续、commit 落定；
- 切活动节点自动落定（1 command，值保留）；
- 异步令牌：删实例/换代际后晚到结果丢弃，新实例新令牌有效；
- LUT 强度手势合并为 1 command，undo 回 1.0。
- 回归：全套 194 passed（含 R03 控制器 15 项）；原生验证 passed。

## 尚未支持

- AI bake 异步任务创建与资源生命周期（R06 显式协议消费本守卫）；
- 手势期间逐帧像素预览（随 R06 图执行 transient 通道评估）。

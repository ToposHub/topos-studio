# R11 留档：图标、诊断与真实状态投影（2026-11-04）

## 实现

- **标记读权威资源域**：`graph_payload_to_draft` 的 `_indicator_payload`
  把 `params.resources.lut_ref/lut_strength` 并入标记视图（与 R05 投影同
  语义：设置点亮 LUT 标记、移除不复活迁移旧值）；OFX 标记沿用
  legacy_payload.plugins（R03 命令事实源）。仅浏览库/悬停未提交不改变
  载荷 → 天然不点亮。
- **实例状态投影**：`_project_instances` 每实例带 `status/
  disabled_reason`（R06 预检码：resource_unknown/ai_model_missing/
  shader_missing）；缺插件配置保留 + fail-visible；ack 后随权威投影刷新。
- **真实后端横幅**：浏览器 `set_production_mode(True)`（生产装配调用）
  移除"交互预览"横幅；显式 preview 模式恢复。
- **诊断**：拒绝路径带 `duration_ms` + target（node_id/expected revision）
  + 稳定 code，WARNING 日志一条（不逐帧刷屏）；`resource_rejected`
  信号供 UI 翻译码展示。

## 测试（新增 4 项，控制器套件 19 项全绿）

- 权威 LUT 域点亮标记 / 移除即灭（不复活旧值）；
- 旧工程缺插件实例 → status=resource_unknown（配置保留）；
- 拒绝诊断携带 target/revision/时长；
- 生产模式横幅移除 + 显式开发模式恢复。
回归：tool feedback/routing/gestures/menu parity 24 passed；原生验证
passed（per_node 保持）。

## 剩余

等待中（pending bake）状态与能力不足（N14 域）细分随对应批次；诊断码
i18n 表随 N17 可选依赖诊断统一补全。

# R09 留档：保存、版本、still、共享与跨工程资源闭包（2026-11-04）

## 实现

- **闭包收集**（`lut_resource_registry.collect_graph_lut_closure`）：以图为
  权威遍历全部 Corrector（含 compound children 递归）的
  `params.resources.lut_ref` + 迁移期 `legacy_payload.active_lut_path`；
  共享定义 fanout 后各栈图持同 ref → 去重单资源。
- **保存期闭包确保**（`ensure_graph_lut_closure` + edit_project_io 钩子）：
  旧绝对路径且文件存在 → **自动迁移**为工程资源（旧字段保留，读取侧
  R05 投影优先 resources 域）；缺失引用（topos-res 未导入/文件丢失）→
  记 missing + WARNING（**不静默移除、不阻断保存**，条目保留可修复
  状态）；报告存 `_last_resource_closure_report` 供 UI 预检。
- **包内闭包**：保存自动带 `resources/resources.json` + `resources/lut/`
  内容寻址文件（R02 通道）——另存/autosave/版本切换/Gallery still/
  copy-paste 均经同一 save_project 通道携带完整资源；跨工程粘贴同 id
  不同内容不覆盖（uri 内嵌 sha，R02 已锁定）。
- **可用性投影**：会话 catalog 条目按 `status_of` 带 `available/
  disabled_reason`（缺失=可修复，不冒充可用）。
- **undo 恢复**：删除节点的 undo 恢复同 lut_ref；资源文件以包为归宿，
  不提前回收（引用计数回收随 N06 缓存治理统一）。
- 共享 fanout 单事务（B05.4 既有）与栈锁后端校验
  （SetColorGraphLockCommand 既有）复核通过。

## 测试（tests/color/test_lut_resource_closure.py，5 项）

闭包覆盖 params/legacy/compound；旧路径自动迁移（registered）；缺失
引用保留可修复（图内原样）；真实保存带闭包 manifest + 删节点后引用闭包
清空/原图恢复语义；共享双栈同 ref 去重单条目。
回归：registry+closure+unknown_field+save_integrity+archive 63 passed；
原生验证 passed（per_node=evaluated_node_thumbnails 保持）。

## 尚未支持

文件资源按引用计数回收（项目/历史/版本/still 全量计数——N06/R12 治理
批次）；still/剪贴板的独立资源快照通道（当前经保存闭包间接覆盖）。

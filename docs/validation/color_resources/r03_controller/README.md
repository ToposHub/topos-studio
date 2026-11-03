# R03 留档：typed target、资源命令与 ack/reject 主干（2026-11-03）

## 实现

- **service 原子操作**（`color_graph_service.py`，9 个 op）：add/remove/move/
  enable/parameter/reset 效果实例（载体 = node.legacy_payload.plugins，与 legacy
  Layer 反向投影同源）；set/replace LUT + strength + remove（载体 =
  node.params.resources.lut_ref/lut_strength）。全部 op 先验证目标
  Corrector 家族/锁/资源存在/参数 schema 类型与范围/实例归属，失败原图返回。
  资源权威可注入（生产默认 PluginManager + LutResourceSession）。
- **intent 契约**（`color_graph_controller.py` ACTION_CONTRACTS）：9 个资源
  action（add_effect/remove_effect/move_effect/set_effect_enabled/
  set_effect_parameter/reset_effect/set_lut/set_lut_strength/remove_lut），
  不复用 set_grade 任意字典。
- **生产适配器** `color_resource_controller.py`：意图接收瞬间捕获 typed
  target（session/generation/scope/owner/stack/compound_path/node_id +
  expected_graph_revision），双击目标=信号时刻活动 Corrector、拖动=落点
  node_id、多选只操作 active；经 normalizer → bridge.submit →
  ApplyColorGraphCommand → 工程 command 栈（一次操作一次 undo）；reject
  按权威投影恢复（resource_rejected 携带稳定 code），ack 后从
  coordinator.last_ack 权威投影水合浏览器。
- **互斥装配**（main_window）：默认正式 controller；config
  `color_resource_ui_preview_mode`（显式开发模式）才装 ColorResourcePreview
  草稿 presenter——同一组浏览器信号永不双写。验证脚本 config 固定 preview
  模式保住草稿契约断言；正式路径由宿主矩阵测试覆盖。

## 测试（tests/app/test_color_resource_controller.py，15 项）

- service：默认值构造+revision、拒绝矩阵（Source/Mixer/幽灵节点/未知资源/
  未知 LUT/强度越界/实例不属于）、锁定全拒、参数类型/范围/未知键 + reset
  保留未知字段、重复实例与重排身份、compound 内部目标。
- 宿主（真实项目栈+命令栈）：一操作一 command 一 undo（undo 后浏览器随
  ack 投影清空）、不可用资源/无目标拒绝、栈锁拒绝图字节不变且不入史、
  revision 冲突拒绝、删节点后晚到 intent 领域拒绝、跨会话代际拒绝、
  LUT 应用/强度/移除回显、drop 用落点非 active、编辑信号逐条命令路由
  （5 操作 5 command）。
- 回归：图/资源全套 164 passed；原生验证 passed（preview 模式契约 +
  工程不变断言保持）。

## 尚未支持（后续轮次）

- R04 手势事务（begin/flush 合并连续拖动为一次 undo）与 AI 异步绑定；
  R05 LUT 处理顺序规格与渲染消费；R11 真实状态标记/诊断投影。

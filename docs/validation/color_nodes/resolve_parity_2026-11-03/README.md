# Resolve节点逐项对照与补齐记录（2026-11-03）

对照对象：本机正在运行的 DaVinci Resolve Studio 20 / Untitled Project 4 Color页。保留前轮未提交集成修复，本轮未自动提交。

## 真实参考观察

本次在Resolve临时添加Serial节点02，观察中性节点底栏只有编号；将Primaries Temp从0拖到300，底栏新增三根调节杆的小型单色图标；再调整Custom Curve，中间控制点偏离直线后，底栏同时显示初级调色与曲线两个图标。经Edit菜单Undo依次撤销曲线、色温和临时节点，最终截图确认恢复初始单节点、中性色温、直线曲线；未保存参考工程。工具面板切换不计为实际调色，不应产生工具标记。

读取了Resolve Color→Nodes完整菜单：Previous/Next、Serial/Before/Parallel/Layer、Append、Outside、Splitter Combiner、Serial+CPW/LPW/PPW/PCW、Enable Selected/All、Extract/Delete/Label；Color→Reset包含Selected Grade、Keep Nodes、All Grades and Nodes等。以下将**实际视觉观察、项目自动化验证和未实现能力分开标注**，不将本次检查冒充Resolve全部工具引擎的逐像素认证。

## 对照矩阵

| 功能 | Resolve参考/预期 | Topos本轮状态与验证 |
|---|---|---|
| 中性节点工具标记 | 实机观察：只有编号 | 已修：不再因grade字典有默认键就绘制曲线；检测器测试中性Layer与身份LUT/曲线无误报 |
| 初级调色标记 | 实机Temp0→300：三调节杆 | 已补：Primary专属三调节杆；原生素材调整曝光15后断言对应卡片包含primary |
| 曲线标记 | 实机非线性曲线：与Primary并列 | 已补：曲线/HSL/soft clip实际偏离默认时显示，身份曲线不误报 |
| 色彩映射/RGB Mixer | 工具使用应可识别 | 已补工具标记；零通道偏移/零warper网格不误报；未宣称Resolve全部Color Warper算法等价 |
| Qualifier / Window | 工具使用应可识别 | 已补启用限定器、有效窗口标记；无窗口/禁用窗口不误报；活跃Layer编辑信号刷新 |
| Blur/Sharpen / LUT / Effects | 工具使用应可识别 | 已补实际启用参数/已有LUT引用/启用插件标记；已有Layer LUT可识别不等于节点菜单LUT导入已完成 |
| Key / Keyframes | 工具使用应可识别 | 已补不透明度/非Normal混合与已有关键帧元数据标记；完整节点关键帧引擎仍受前轮限制 |
| 多标记/旁路/悬停 | 实机Primary+Curve并列 | 已补多枚单色标记、旁路变暗；底栏溢出用省略标记，悬停列出全部；窄卡片不覆盖锁/共享状态 |
| 图标生命周期 | 跟随参数而非工具面板焦点 | 已补权威图投影+当前实际Layer信号反馈；重置清除；复制/粘贴完整载荷后重新派生；程序水合有防递归守卫 |
| Previous / Next Node | 菜单已确认 | 本轮新增宿主Nodes菜单，按编号选择、无图命令/历史污染；Qt测试通过 |
| Append a Node | 菜单已确认 | 本轮新增：选择之前节点时仍追加到主Output前；测试通过 |
| Serial / Before / Parallel / Layer / Outside / Splitter | 菜单已确认 | 原有功能继续通过UI/service/GL/CPU回归；本轮修复这些操作接在Mixer之前时的下游槽身份保持 |
| Serial + CPW/LPW/PPW/PCW | 菜单已确认 | **尚未补齐一键组合命令**；已有Window工具独立使用，不能标为该组合功能相同 |
| Enable selected / all | 菜单已确认 | 原有命令与回显继续回归；旁路保留调整标记并变暗 |
| Extract / Delete / Label | 菜单已确认 | 已修复下游Mixer槽丢失；原子撤销与Compound子路径编辑继续通过 |
| 断开/重连/多线断开 | 上轮已实机验证基本拖动 | 本轮继续通过；Mixer输入端口别名/槽ID不混用，替换只影响目标槽 |
| 拖节点插入连线 | 节点图常规操作 | 本轮修复缺node_id/edge_id参数导致的拒绝；一次领域事务包含移动+插入 |
| Copy/Paste grade | 上轮已修 | 继续回归；不将节点Gamma字符串混入初级Gamma元组 |
| Paste Nodes / Duplicate | 节点图常规操作 | 本轮补完整工具载荷、资源元数据与子图；递归改写内部节点/边/legacy Layer身份，避免共享错误ID |
| Compound / Decompose / enter/back | 节点图常规操作 | 前轮修复继续回归；本轮复制Compound保留内部完整载荷并改写身份 |
| Shared / Make Local / Lock | 共享节点应可辨认、锁定后禁止参数写入 | 本轮Save Shared默认锁定；服务拒绝锁定节点grade/reset/paste；解锁恢复。跨clip粘贴共享定义、跨引用实时参数联动仍需专项完善 |
| Node Color | 节点色标 | 修正领域颜色名→UI柔和色板的往返；不再丢色/变成非法颜色字符串 |
| Mixer Add Input | 输入端口可扩展 | 本轮Parallel/Layer/Key Mixer均补齐，投影按实际slots生成端口数 |
| Layer Mixer Composite Mode | 混合模式应持续回显 | 本轮修正投影，从mixer域读取勾选值；既有执行器消费该域，继续回归 |
| Invert Key Input | 应真实影响key输入 | **尚无完整渲染消费**；本轮禁用节点菜单并提供说明，避免伪成功。Key输入组合实现不能用图标替代 |
| 节点LUT菜单 | 应导入工程资源并参与节点执行 | **尚缺资源导入/解析闭环**；本轮禁用该菜单。已有侧栏/legacy载荷LUT不因此删除 |
| Node Color Space / Gamma | 应执行节点级颜色变换 | **尚无完整执行器消费**；禁用并说明；本轮另修复该Gamma与primary Gamma共用字典键的冲突 |
| Node Cache Auto/On/Off | 应改变实际缓存策略 | **尚无完整执行器消费**；本轮禁用该菜单，不以绘制蓝条冒充缓存完成 |
| Clip / Group / Timeline scope | 真实scope owner与输出链 | 延续前轮限制：Timeline真实栈尚未接通；不因本轮图标/菜单修复宣称完成 |
| 逐节点处理预览图 | 应显示节点实际输出 | 仍为源素材fallback；报告保留source_thumbnail_only，不能作为Resolve逐节点预览等价证据 |

## 实施文件与数据来源

- `src/features/color/ui/node_tool_indicators.py`：纯数据检测，按Layer实际默认值判断，支持NumPy形态；无GPU调用。未把全局NR/RAW/sizing错误归到某个Corrector。
- `NodeDraft.tool_indicators` 是派生presentation状态，不新增工程schema字段；`backend_payload`供完整节点复制，工程仍由service验证。
- `graph_payload_to_draft`从权威legacy_payload+图参数派生标记；`ColorNodeToolFeedback`接已有侧栏信号，在实际活动Layer身份匹配时立即更新选中卡片。它不提交工程，不把root Layer工具归到Compound子节点。
- 非primary工具当前仍有legacy编辑路径；复杂图、未保存参数及跨scope生命周期应结合前轮报告继续验收。本轮标记只说明已有有效参数，**不证明未接通的渲染能力已经可用**。

## 验证

- 相关图schema/service/commands/CPU/GL/持久化/App/UI回归：**546 passed / 28.01s / EXIT 0**，见 `regression.log`。
- 本轮标记/实时反馈/菜单/真实桥接focused组合：**85 passed / 7.31s**，见 `focused.log`。
- 原生应用使用测试素材01.mov，图渲染开关开启，验证新增、调色、节点图标、项目历史、分支增删、断线重连与`.topos`保存重读：**passed**。报告 `native_report.json` 明确标注源缩略图限制。原生节点面板见 `nodes_with_tool_markers.png`：节点01已调曝光，底栏显示Primary；节点02中性，无工具标记。
- `git diff --check`通过。本轮未覆盖Resolve所有专有OFX/AI/RAW算法、HDR平台认证、实时4K及跨平台数值等价；未支持项上表保留明确状态。

复跑：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/color/test_color_graph* tests/color/test_color_node_menu_parity.py tests/app/test_color_graph* tests/ui/test_color_node* tests/ui/test_node_tool_indicators.py -q
TOPOS_COLOR_GRAPH_RENDER=1 .venv/bin/python scripts/validate_color_node_media.py tests/test_videos/测试素材01.mov --workflow --output /tmp/topos-node-parity
```

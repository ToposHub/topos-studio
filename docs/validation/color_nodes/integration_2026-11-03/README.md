# 前后端集成实测与修复（2026-11-03）

起始版本 `7cb6c8d`，macOS / AMD Radeon RX 6800 / Python 3.12.14。
本次修复保留在工作区，未自动提交。原实施计划是设计基线，当前实现状态应结合实施台账、本报告与发布验收矩阵阅读。

## 实测结果

- 首轮图相关/UI/示波器回归：627 passed。
- 修复后全量 Color + 图 App/UI + Metal：**3595 passed、5 skipped、1 deselected**，105.04秒，正常退出。完整输出见 `regression.log`。
- 之后补齐非Corrector选择的参数面板禁用范围、Compound返回后的活动句柄同步，相关focused重跑：**56 passed**，见 `focused.log`。
- 着色器 `grading_extreme/checker` 位级基线失配：首轮全量仍可复现，与既有台账一致。最终全量明确 deselect 这一项；未更新golden掩盖差异。首次全量的另一个teardown错误来自并发编辑触发repo_stays_clean门禁；停止代码修改后全量重跑无该错误。
- 原生应用，导入 `tests/test_videos/测试素材01.mov`（1080p、25fps、124帧），分别测试 `TOPOS_COLOR_GRAPH_RENDER=1` 和默认路径 `=0`。两种配置均通过工程图提交、串行新增、并行分支新增与撤销、图调色、参数面板回显、项目撤销重做、断线重连、真实`.topos`写盘后重新解析并比较图载荷。
- 原生波形、分量图、矢量、直方图、CIE、假色及双/四/全部布局均有输出。最终全部布局截图包含亮度波形。修复后未出现 `texture_not_shared`、共享上下文fallback或FBO呈现失败诊断。启动无front、最新需求替换、节流、迟到帧拒绝属于记录到的正常调度，不以零丢弃为验收条件。
- `graph_enabled/`、`default_path/` 保存报告与原生截图；`native_graph.log`、`native_default.log` 保存运行日志。临时工程和隔离配置留在系统临时目录，未写入用户现有工程。

## 已修复

1. 真实UI发送ConnectionDraft/list/空参数时，normalizer发生类型异常；补充UI到领域参数转换，拒绝非法输入并回滚权威投影。
2. 图端口名与UI端口别名不一致，包括Mixer分槽、slot ID和Key Mixer输出；分槽替换保留其他连接，多线断开一次事务。
3. 新增节点没有使用选中锚点，投影后选择丢失；使用权威节点ID并保留/更新有效选择。
4. 权威投影清空本地历史后，节点快捷键Undo无效；改走项目历史，菜单可用状态读取项目栈。
5. Compound缺少子图投影，内部编辑错误提交根图；补递归投影、带路径的子图事务、导航恢复和关闭语义。
6. 多字段调色手势反复从旧工程图开始，最后只保存最后一个字段；按待提交图累积，整手势一次命令。
7. 粘贴调色缺少服务处理；Reset只清新参数、旧legacy参数重新出现；补服务及完整legacy参数重置。
8. 没有保存位置的节点全部重叠、显示00；补确定性布局和显示编号，保留显式位置。
9. 参数面板没有订阅选中Corrector句柄；补参数回显与活动层对齐，程序化更新有防递归守卫；非Corrector选择禁用调色/曲线和效果、qualifier、warper、mask、plugin、tracker参数入口。
10. 进入/退出Compound后活动句柄未切换，可能继续修改旧子节点；导航后同步选择，空选择清理参数编辑权限。
11. 示波器模块在import时捕获了启动前的GL默认格式，macOS创建非共享上下文导致黑屏；改为控件构造时获取应用已设置的格式，并保留single-sample FBO。

## 尚不能标记为完整发布

- `TOPOS_COLOR_GRAPH_RENDER` 主画面图执行仍默认关闭；本次明确分别测试两种配置，没有改默认值。复杂图仍不能以legacy主预览代表完整图输出。
- 节点卡片仍显示静态源素材fallback，尚无真实逐节点处理结果缩略图。本次报告明确 `per_node_rendering=source_thumbnail_only`。
- Timeline scope缺少真实grade栈模型；跨页/复杂图的全部非primary参数编辑、逐节点关键帧仍需按台账继续验收。
- 复杂图导出仍受能力门禁，full-resolution路径仍有legacy限制。本次没有宣称复杂图完整导出通过。
- 原生持续播放/4K实时预算、浮动/resize组合压力及其他GPU/平台未由本次短素材静态冒烟覆盖；S07完整发布矩阵仍需完成。
- 启动日志仍有可选ThumbEngine找不到FFmpeg、未安装AI模型提示；本次源图使用已有共享缩略图服务/缓存，不能证明该可选FFmpeg引擎可用。

## 复跑

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/color tests/app/test_color_graph* tests/ui/test_color_node* tests/shared/gpu/metal/test_color_graph_metal.py --deselect=tests/color/test_shader_golden_corpus.py::TestShaderGoldenCorpus::test_bit_identical_to_baseline -q
TOPOS_COLOR_GRAPH_RENDER=1 TOPOS_SCOPE_DIAG=1 .venv/bin/python scripts/validate_color_node_media.py tests/test_videos/测试素材01.mov --workflow --output /tmp/topos-node-workflow
# 用 TOPOS_COLOR_GRAPH_RENDER=0 重复原生命令验证默认路径。
```

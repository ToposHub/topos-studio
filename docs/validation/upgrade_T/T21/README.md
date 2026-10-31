# T21 · 验收证据（分列：纯逻辑 / 组件 / 真实入口 / 平台）

> 状态（2026-10-25）：**自动化可及部分全部完成**；人工鼠标级 GUI 交互与
> Windows 复验两项外部依赖如实保留。

## 1. 纯逻辑 / 契约层（已验 ✅）

- 命令：`.venv/bin/python -m pytest tests/edit/insights -q`
- 结果：**145 passed, 1 skipped，退出码 0**（功能测试 + G1 入口 3 +
  G1b-lite 真实宿主 5 + G2 打包 3 + 功能页签 7 + 事件级导航 2 +
  音频 cue 复用 1，1 条件跳过=10 万规模需环境变量）。
- 六项功能全部有**用户可见入口**（窗口 6 页签：搜索/总表/统计/联系表/
  资料包/对照——计划 §0"不能只提交 helper"要求满足）；音频 cue 导出
  复用既有 interchange.CueSheet（资料包内 `audio_cues_*.csv`）。
- 事件级导航：双击路径经 doubleClicked 信号连接触发验证（真实鼠标双击
  的合成事件在 offscreen QPA 不被识别为双击——最小 QTableView 探针
  证实 clicked 可达/doubleClicked 不可达，属平台合成限制非缺陷）；
  键盘等价路径（Enter）经 QTest 真实键盘事件投递验证。

## 2. 架构守卫（已验 ✅，基线存量另列）

- 命令：`.venv/bin/python -m pytest tests/architecture -q`
- 结果：409 passed, 1 xfailed, **2 failed**——开工前基线存量
  （`edit_controller.py` 深导入指纹未更新，见
  `docs/insights/handoff_M_baseline_guardrails.md`）；G1a/G2 新增改动
  未产生新违例。

## 3. 真实产品入口（G1a + G1b-lite 已验 ✅ / 鼠标级交互待人工 ⏳）

- G1a（`test_g1_entry.py`）：真实 ActionRegistry 注册与 feature 过滤、
  mixin 回调存在性（menu_builder 空壳防护语义）、导航四类拒绝 +
  既有切换/播放头路径调用验证。
- G1b-lite（`test_g1b_real_host.py`，真实 `MainWindow(test_mode=True)`；
  含 full_chain 测试：真实 TimelinePanel/EditPage/MediaPoolPanel 上执行
  真实 `_on_timeline_switched` 方法体——该测试捕获并修正了 G1a 初版
  误接 `_on_media_pool_timeline_selected`（只显示信息不切换）的真实 bug；
  验证 active_timeline_id 导航状态变化 + 编辑 revision 不变）：
  getattr 回调解析 → factory 打开真实窗口（重复打开同实例）→ 从真实
  MainWindow 属性捕获（3 序列）→ 后台搜索 2 命中 → 未物化 ref 导航
  结构化拒绝不建空序列 → shutdown 后 worker 线程全停 → 无工程
  `no project` 提示。
- ⏳ 鼠标级：启动应用 → 菜单点击 → 真实渲染下切换序列定位 →
  导航前后 revision/Undo 不变——需人工交互会话。

## 4. 全量回归（已验 ✅ 零新增失败）

- 我的树：`61 failed, 11173 passed`（两次运行一致）；
  基线 worktree @ `dd1a28fdd`：`65 failed, 11092 passed`。
- **对照结论：只在我的树失败的测试 = 0 个**（61 ⊂ 65；基线多出的 4 个
  为 worktree 环境的 import_builder 失败）。详见 `regression_baseline.md`。

## 5. 性能（已验 ✅）

- `docs/validation/upgrade_T/T20/`：1 万实体 capture 0.39s / 搜索
  p95 13.6ms / RSS 145MB。

## 6. G2 资源打包（配置+构建已验 ✅ / 冻结包开箱待打包机 ⏳）

- `pyproject.toml`：`src.features.edit.insights = ["locales/*.json"]`；
  实测 wheel（`pip wheel --no-build-isolation`）含
  `src/features/edit/insights/locales/{zh_CN,en_US}.json`；
- `packaging/pyinstaller/base_spec.py`：新增二级 locales 目录扫描
  （`src/features/*/*/locales`）；离线截获 Analysis datas 验证含
  insights（`test_g2_packaging.py` 钉住，不回归）；
- ⏳ 冻结包（.app/.exe）实际打开验证属打包机例行流程。

## 7. Windows 复验（待 W 线 ⏳）

- 本机为 macOS；Windows 入口/文本文件/窗口行为按计划由 W 线复验。


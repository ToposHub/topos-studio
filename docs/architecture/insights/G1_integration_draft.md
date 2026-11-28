# G1 集成接缝草案（T02 交付 → Mac 集成者落地）

> 状态：**G1a 已实际落地并修正**（2026-10-25，分支 `codex/project-insights`，
> 真实代码见 `edit_controller.py` 末段与 `module.py`；本文保留为接缝说明
> 与复验清单，方法名以已落地代码为准）。

## 0. 落地范围（三个共享文件，全部薄改）

| 文件 | 改动 | 预计行数 |
|---|---|---|
| `src/features/edit/module.py` | `register_menu_actions` 追加 1 个 MenuActionDescriptor | ~8 |
| `src/app/controllers/edit_controller.py`（或其宿主 mixin 所在文件） | 新增 `on_insights_open` 回调 + `InsightsHostAdapter` 组装（三个注入回调） | ~60 |
| `src/features/edit/commands_facade.py` | （仅当导航/捕获需要经 facade 暴露时）薄转发方法 | ~15 |

不需要改：`.topos` schema、renderer/decoder、Undo/command stack、
dock/workspace、feature_flags、module_loader（`insights/` 是 edit 特性内
子包，无新 feature_id）。

## 1. 菜单补丁（G1a 第一步）

`EditModule.register_menu_actions` 末尾追加（与既有 `edit.timeline.*` 同型）：

```python
action_registry.register(MenuActionDescriptor(
    action_id="edit.insights.open",
    menu_path="Edit/Insights",
    label=I18N.tr("action.insights_open", "&Insights"),
    callback_name="on_insights_open",
    order=500,
    feature_id="edit",
))
```

要点：
- `feature_id="edit"` → Edit 禁用时菜单自动隐藏（既有过滤逻辑，无新增开关）；
- **不要**接 `register_actions`（那是占位，接错不构成产品入口）；
- 词条 `action.insights_open` 加进 `src/features/edit/locales/*.json` 顶层
  `action` 段（共享文件，集成者一次写入；T 线不动）。

## 2. 宿主回调与适配器组装（edit_controller 或 mixin）

```python
def on_insights_open(self) -> None:
    from src.features.edit.insights.factory import open_insights_window
    from src.features.edit.insights.host_adapter import InjectedHostAdapter

    def _capture_state():
        # 复用 get_project_sequences 的读取纪律（ai_edit_host_bridge_impl:338）：
        # 无工程返回 None——不造空序列，不触发懒加载。
        project = getattr(self._mw, "_current_edit_project", None)
        sequences = getattr(self._mw, "_current_timeline_sequences", None)
        if project is None or sequences is None:
            return None
        from src.features.edit.ai_editing.edit_project_service import ProjectSequences
        return ProjectSequences(project=project, sequences=dict(sequences))

    adapter = InjectedHostAdapter(
        capture_state_fn=_capture_state,
        navigate_fn=self._insights_navigate,          # 见 §3
        thumbnail_fn=self._insights_thumbnail,        # 见 §4（G1a 可缺省）
    )
    open_insights_window(adapter, parent=self._mw)
```

重复打开由 factory 去重（同实例激活）；工程关闭/切换时宿主调
`shutdown_insights_window()`（停 worker、关窗口）。

## 3. 导航回调（UI 线程；G1b 联验核心）

```python
def _insights_navigate(self, request) -> NavigateResult:
    project = getattr(self._mw, "_current_edit_project", None)
    if project is None:
        return NavigateResult(ok=False, status="rejected", reason="no_project")
    if request.project_id != project.project_id:
        return NavigateResult(ok=False, status="rejected",
                              reason="project_changed")   # 换工程：提示刷新，不强导航
    sequence = self._current_timeline_sequences.get(request.sequence_id)
    if sequence is None:
        return NavigateResult(ok=False, status="rejected",
                              reason="target_missing")    # 含未加载 ref：不建空序列
    if (request.expected_sequence_revision is not None
            and sequence.revision != request.expected_sequence_revision):
        return NavigateResult(ok=False, status="rejected", reason="sequence_changed")
    # 以下只走既有路径：切活动时间线 → 移动播放头（保持既有导航语义与
    # active_timeline_id/playhead/selection 的正常变化；不新增导航旁路）
    self._on_timeline_switched(request.sequence_id)      # 既有完整切换方法
    self.edit_page.seek_to_frame(max(0, request.target_frame))
    return NavigateResult(ok=True, status="navigated",
                          actual_sequence_id=request.sequence_id,
                          actual_frame=max(0, request.target_frame))
```

> ⚠️ 已落地版本的实际经验（2026-10-25）：曾误接
> `_on_media_pool_timeline_selected`——该方法**只显示时间线信息不切换**
> （名字有迷惑性）。正确方法是 `_on_timeline_switched`（active_timeline
> 更新/媒体池同步/面板加载/Deliver 绑定同步的完整路径）。该错误由
> "真实 MainWindow+真实面板控件"级测试捕获（test_g1b_real_host.py 的
> full_chain 测试）——单元级 stub 测试无法发现。集成者复验时务必跑
> 该测试。

集成者按当时调用链替换 `_switch_active_timeline/_seek_playhead` 的真实
方法名（本草案不冻结行号/私有名）。验收红线：导航允许既有导航状态变化，
但编辑内容（序列内容/revision/Undo 深度）不得变化——G1b 用 T21 口径回归。

## 4. 缩略图回调（G1a 可缺省，T13 前必须接）

接既有 `ThumbnailProvider`（缓存/异步/播放避让都已存在，**不新建解码**）：
请求带 `asset_id + source_frame + 尺寸`；结果包装成
`ThumbnailResult(quality="source_thumbnail", media_type="png", data=<bytes>,
fingerprint=<thumbnail_key@frame>)`；离线/不支持映射返回
`quality="placeholder"`。异步回填同一 result 形态即可（窗口侧
`ThumbnailGate` 在途上限 2 + 播放暂停由 T20 运行时保证）。

## 5. G1a / G1b 验收清单

G1a（菜单+协议+惰性 factory，可先行关闭）：
- [ ] Edit 启用时有 `Edit/Insights/Insights` 菜单项；Edit 禁用无入口；
- [ ] `InjectedHostAdapter` 三方法可被 fixture 驱动（`tests/edit/insights/` 91 测试在集成分支全绿）；
- [ ] 无工程时打开窗口显示"捕获失败：no project"，不建空序列、不崩；
- [ ] 菜单重复点击不重复建窗（factory 去重）。

G1b（真实窗口联验，T03 组件已就绪即可做）：
- [ ] 真实工程 → 菜单 → 窗口 → 搜索 → 双击命中行：非当前序列正确定位（经 §3 路径）；
- [ ] 换工程/删序列后旧窗口操作给出"拒绝/请刷新"，不误导航；
- [ ] 导航前后：project.revision、各 sequence.revision、Undo 深度不变（播放头/选择属正常导航状态，允许变化）；
- [ ] 关闭窗口后再开：worker 线程数回落（无泄漏）；
- [ ] Windows 侧同一清单复验（W 线）。

## 6. 已知边界（如实告知集成者）

- 复杂变速/VFR 的源帧映射在 core 未验收（M04 未收口），快照一律标
  `source_mapping="unknown"`，工具不推导——这不是 G1 缺陷；
- 命名工程版本（M02/M15）未接入：T15 对照只在同工程重建 ID 一致时逐行对齐；
- `edit_controller.py` 当前有 3 处深导入使基线 guardrail 失败（见
  `handoff_M_baseline_guardrails.md`）——与 G1 无关，但合入时请勿顺手再加深导入。

# BUGS-agent-C3：page_controller.py 补测发现的疑似缺陷

- 日期：2026-09-28
- 模块：`src/app/controllers/page_controller.py`
- 来源：Plan-02 补测（`tests/app/test_page_controller_coverage.py`，139 用例）
- 处置原则：**只报告，不修改 src**。问题的"当前行为"已用锁定断言写进测试
  （见各条"锁定用例"），修复后应把断言翻转为正确预期。

严重度定义：high（用户可见数据/状态错误）> medium（状态不一致，特定路径才暴露）>
low（健壮性缺口）。

---

## BUG-C3-01（medium）：时间线传输条槽闭包在每次进入 Composite 页时重复连接累积

- 位置：`PageManagementMixin._configure_composite_timeline_playback_controls`
  （page_controller.py ~L1400-1424 的 `control_connections` 连接循环）。
- 现象：每次进入 Composite 页都会重跑该函数，为 viewer 传输条（VideoControlBar）
  的 13 个信号（seek/scrub×3/play/next/prev/first/last/mark_in/mark_out/clear_io/
  loop）**新建一批闭包槽**并连接。函数顶部的拆卸逻辑只处理
  `frame_changed` / `state_changed` / `_kf_transport_state_slot`（有
  `_composite_controls_*` 属性记录的槽）；对 13 个 bar 信号的拆卸循环是：

  ```python
  for signal_name, slot in control_connections:
      signal = getattr(bar, signal_name, None)
      ...
      while True:
          try:
              signal.disconnect(slot)
          except TypeError:
              break
      signal.connect(slot)
  ```

  这里的 `slot` 是**本次调用新创建**的闭包，从未连接过，`disconnect` 立即抛
  TypeError 而跳出循环——上一次（上一次进入页面时）连接的旧闭包从未被拆除。
- 后果：第 N 次进入 Composite 后，每个 bar 信号上挂着 N 个闭包；旧的闭包捕获
  旧的 `controller` / 旧 `clip_start` / `clip_total`。用户点一次播放/seek 会触发
  N 次槽调用：轻则重复 seek/重复 set_playback_range（表现为主观卡顿、日志刷屏），
  重则旧闭包把播放头拽回旧 clip 的坐标系（时间线重排或 comp clip 范围变化后）。
  对比同文件 `_configure_composite_comp_clip_controls`（~L1583-1599）：那条路径把
  `slots` 存进 `self._comp_clip_bar_slots` 并在下一次调用时先 disconnect 旧槽，
  是正确写法——本条疑似是遗漏了对称的记录/拆卸。
- 复现：见锁定用例。
- 锁定用例：`tests/app/test_page_controller_coverage.py::
  test_timeline_controls_bar_slots_accumulate_across_rewires` —— 当前断言
  `len(bar.play_toggled.slots) == 2`（钉住现状）。修复后应改为 `== 1`，并且
  断言旧槽被移除（frame/state 槽的对称行为可参考同文件
  `test_timeline_controls_rewire_disconnects_previous_frame_state_slots`，
  那两个信号目前是正确去重的）。
- 修复建议：仿照 comp-clip 路径，把每次的 `control_connections` 列表存入
  `self._composite_timeline_bar_slots`，函数开头对旧列表逐个 disconnect。

---

## BUG-C3-02（low）：`_configure_composite_timeline_playback_controls` 对
返回 None 的 `comp_clip_total_frames()` 缺少兜底（防御不一致）

- 位置：page_controller.py ~L1249-1253：

  ```python
  total_frames = getattr(page, "comp_clip_total_frames", None)
  if callable(total_frames):
      clip_total = max(1, int(total_frames()))
  else:
      clip_total = max(1, int(getattr(controller, "timeline_duration", 1) or 1))
  ```

- 现象：属性存在且可调用时直接 `int(total_frames())`，没有 `or 1` 兜底；
  一旦返回 `None` 就抛 `TypeError`，直接打断整个
  `_configure_composite_timeline_playback_controls`，传输条不显示、
  Keyframes transport 也不装配。
- 影响评估：**当前真实 `CompositePage` 不会触发**——`comp_clip_total_frames`
  （composite_page.py L4586，`-> int`）委托 `_total_frames_for_node()`，
  其各分支都 `max(1, ...)` 永不返回 None。该缺口只对轻量页面替身/旧版
  CompositePage 实现（`getattr(self, "comp_clip_total_frames", None)` 的
  duck-typing 探测本身就是在容忍这类对象）构成崩溃风险。
- 对照：else 分支（读 `timeline_duration`）有 `or 1` 兜底，说明作者本意是
  两条路径都该防 0/None，callable 分支漏了。
- 锁定用例：无（补测里 `test_timeline_controls_seed_bar_from_controller_duration`
  用"页面没有 `comp_clip_total_frames` 属性"的替身覆盖 else 分支；callable
  返回 None 的崩溃路径就是缺陷本身，未锁定）。修复后可补一条
  `comp_clip_total_frames = lambda: None` → clip_total 回退 timeline_duration
  的用例。
- 修复建议：`clip_total = max(1, int(total_frames() or 1))`，或对
  `total_frames()` 结果做 try/except 回退到 `timeline_duration`。

---

## 备注（非缺陷，仅记录）

1. `switch_from_edit_to_color(clip_id: str = None)` / `switch_from_edit_to_composite(
   clip_id: str = None, ...)` / `switch_to_deliver(source: str = "color",
   timeline_id: str = None)` 的默认值应为 `Optional[...]`（PEP 484 风格问题，
   运行时无害）。
2. `_show_color_timeline_clip_immediate_thumbnail_preview` 在缩略图路径失败时
   也会递增 `_color_static_preview_token`（作废旧请求），这是有意为之的
   语义（token 即"最新请求 wins"），不是缺陷。

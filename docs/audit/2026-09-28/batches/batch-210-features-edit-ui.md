# 批次审查报告：batch-210 [P1] src/features/edit/ui//

- 任务书：docs/audit/2026-09-28/assignments/batch-210.md
- 编号区间：AUD-21000–AUD-21099
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 汇总：Critical 0 · Major 1 · Suggestion 13

---

## src/features/edit/ui/subtitle_clip_item.py (行数: 540)

### 发现

- [AUD-21001] [Suggestion] mouseDoubleClickEvent 以宽泛 `except Exception: pass` 吞掉对话框全部异常
  - 位置: src/features/edit/ui/subtitle_clip_item.py:529-531
  - 证据:
    ```python
    if ok and new_text != self._text:
        self.subtitle_clip_edit_text_requested.emit(
            self._clip_id, self._track_id, new_text
        )
    except Exception:
        # Dialog failed (e.g. no view) — just keep the selection
        pass
    ```
  - 影响: I18N 导入失败、scene/view 异常、信号槽内错误全部被静默吞掉，违反“不得宽泛 except 掩盖领域错误”铁律；双击编辑失效时无任何日志或提示，难以排查。
  - 建议: 缩小捕获范围（如仅捕获 IndexError/ RuntimeError 的取 view 失败），其余路径记录 logger.exception 并按需向用户提示。
  - 置信度: 高

- [AUD-21002] [Suggestion] paint 中 label_color 的 try/except 为死代码且缺少回退分支
  - 位置: src/features/edit/ui/subtitle_clip_item.py:287-291
  - 证据:
    ```python
    if self._label_color:
        try:
            bg = QColor(self._label_color)
        except Exception:
            pass
    ```
  - 影响: PyQt6 中 `QColor(str)` 对非法颜色串不抛异常而是返回 invalid QColor，except 分支实际不可达；若 label_color 非法，invalid QColor 会进入 QBrush 导致 chip 背景绘制异常（典型为黑色），且静默无日志。
  - 建议: 改为 `c = QColor(self._label_color); if c.isValid(): bg = c`，非法值保留 SUBTITLE_CLIP_BG 并记 debug 日志。
  - 置信度: 高

- [AUD-21003] [Suggestion] keyPressEvent 的 Delete 未检查轨道锁定/编辑锁，与右键菜单路径行为不一致
  - 位置: src/features/edit/ui/subtitle_clip_item.py:534-540
  - 证据:
    ```python
    def keyPressEvent(self, event) -> None:
        """Delete key removes the clip."""
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.subtitle_clip_delete.emit(self._clip_id, self._track_id)
    ```
  - 影响: mousePressEvent（363 行）与右键菜单（timeline_view.py `editable = not track.is_locked and not clip.is_edit_locked()`）均拒绝锁定轨，键盘路径却直接发信号；锁定轨上按 Delete 会走到命令层才被 `_ensure_subtitle_clip_editable` 拒绝（ValueError → 状态栏报错），产生“有时可点、有时报错”的不一致体验（已核实命令层有兜底，无数据风险）。
  - 建议: keyPressEvent 中加 `if self._is_track_locked: return super().keyPressEvent(event)`，与鼠标路径对齐。
  - 置信度: 高

- [AUD-21004] [Suggestion] 构造函数未验证 pixels_per_frame>0，mouseMoveEvent 存在除零路径
  - 位置: src/features/edit/ui/subtitle_clip_item.py:101,421
  - 证据:
    ```python
    pixels_per_frame: float,          # __init__ 无 >0 校验
    ...
    delta_frames = int(round(delta_px / self._pixels_per_frame))
    ```
  - 影响: `set_pixels_per_frame`（169-173 行）有 `ppf > 0` 防护，但构造路径没有；若以 0 构造（调用方未来回归），拖拽首个 move 事件即 ZeroDivisionError，异常逃逸到 Qt 事件派发可能导致程序中止。当前调用点（timeline_view.py `_create_subtitle_item`）传值有效，属防御缺口而非现行缺陷。
  - 建议: `__init__` 中 `self._pixels_per_frame = pixels_per_frame if pixels_per_frame > 0 else 1.0`（或断言）。
  - 置信度: 中

- [AUD-21005] [Suggestion] set_drag_clamp_callback 的回调参数缺少类型注解
  - 位置: src/features/edit/ui/subtitle_clip_item.py:175
  - 证据:
    ```python
    def set_drag_clamp_callback(self, callback) -> None:
    ```
  - 影响: docstring 已给出签名 `(mode, orig_start, orig_dur, proposed_start, proposed_dur) -> (start, dur)`，但参数无 `Optional[Callable[...]]` 注解，IDE/类型检查无法校验 TimelineView 注入的闭包契约（mouseMoveEvent 443-448 行直接解包其返回值）。
  - 建议: `callback: Optional[Callable[[str, int, int, int, int], tuple[int, int]]]`。
  - 置信度: 高

### 通过项
- 几何变更安全：所有改 boundingRect 的路径（set_pixels_per_frame/set_track_height/update_clip_data/restore_geometry/trim）均先 `prepareGeometryChange()`；move 模式改 `_start_frame` 不影响 boundingRect（宽度只依赖 duration），无需调用，正确。
- 线程安全：纯 QGraphicsObject UI 线程交互，无 OpenGL/渲染线程触碰。
- GPU 资源生命周期：无 ModernGL 对象，无泄漏面。
- GLSL/HDR：不涉及。
- 信号防递归：item 仅发信号不回调自身控件；无程序化更新自身控件的递归风险。
- 状态提交一致性：遵循 preview-only 原则——move/trim 期间只发 preview 信号并改自身几何，命令在 release 的 *_finished 信号提交；Alt-copy 源 cue 不移动（`_drag_copy_preview_start` 单独存储，已核实 timeline_view `_on_subtitle_clip_moved` 对 copy 早退）。
- 异常与取消路径：clamp 回调返回值二次校验（`new_start >= 0 and new_dur >= 1` 后再 int 化）；拖拽阈值 `_DRAG_THRESHOLD` 防误触。
- 输入验证：`_preview_text` 折叠空白并截断；tooltip 空文本回退 "(empty subtitle)"。
- 性能：paint 路径无昂贵计算；trim 手柄线仅在宽度 >24px 绘制。

---

## src/features/edit/ui/subtitle_delivery_dialog.py (行数: 114)

### 发现

- [AUD-21006] [Suggestion] _FORMAT_LABELS 硬编码中文文案，未走 I18N
  - 位置: src/features/edit/ui/subtitle_delivery_dialog.py:24-29,55
  - 证据:
    ```python
    _FORMAT_LABELS = {
        "srt": "SRT（通用字幕，毫秒时基）",
        "vtt": "WebVTT（网页/流媒体）",
        ...
    label = _FORMAT_LABELS.get(name, name.upper())
    ```
  - 影响: 同文件其余用户可见文案（标题/格式行/轨道行/提示语）全部经 `I18N.tr`，唯独格式名硬编码中文，英文/多语言环境下该下拉框无法翻译，i18n 覆盖不一致。
  - 建议: 迁移为 `I18N.tr("edit.subtitle_delivery.format.srt", "SRT（通用字幕，毫秒时基）")` 之类的键值查找。
  - 置信度: 高

- [AUD-21007] [Suggestion] `parent` 参数缺少类型注解
  - 位置: src/features/edit/ui/subtitle_delivery_dialog.py:39-43
  - 证据:
    ```python
    def __init__(
        self,
        tracks: Sequence[SubtitleTrackSummary],
        parent=None,
    ) -> None:
    ```
  - 影响: 其余参数与返回值均有注解，`parent: Optional[QWidget] = None` 缺失，弱化类型覆盖完整性（维度 9）。
  - 建议: 补 `parent: Optional[QWidget] = None`。
  - 置信度: 高

### 通过项
- 输入验证：`SubtitleTrackSummary` 字段（track_id/label/language/role/cue_count）与 delivery.py 定义核实一致；"und" 语言码回退为 "—"。
- 状态提交一致性：纯查询对话框，`values()` 只读 checkbox/combo 状态，不触碰模型/命令栈。
- 信号防递归：accepted/rejected 仅连 accept/reject，无递归风险。
- 异常路径：无外部 IO，无需取消路径。
- 线程安全/GPU/GLSL·HDR：不涉及。
- 性能：静态构建，无热路径。

---

## src/features/edit/ui/subtitle_handle_controller.py (行数: 607)

### 发现

- [AUD-21008] [Major] end_drag 的 final（扁平 cue 字段）与 original（resolved style）取值空间不一致，带轨道样式覆盖时可提交伪更新
  - 位置: src/features/edit/ui/subtitle_handle_controller.py:333-342,421-435
  - 证据:
    ```python
    # start_drag: original 取自 resolved style（轨道默认 ⊕ cue 覆盖）
    self._original_params = {
        "translate_x": float(style.get("translate_x", clip.translate_x)),
        ...
    # end_drag: final 只读 cue 扁平字段
    final = {
        "translate_x": float(clip.translate_x),
        ...
    delta = {k: v for k, v in final.items() if original.get(k) != v}
    ```
  - 影响: 当轨道 default_style 覆盖某变换字段（如 translate_x=100）而 cue 扁平值为 0 时，在字幕 body/handle 上“按下-释放”（零位移，已核实 program_viewer mouseReleaseEvent 无条件调 end_drag）也会产生非空 delta；下游 `on_subtitle_interaction_ended` 的 session.before 同样取 resolved style（edit_controller.py:9655-9657 `SubtitleTransformSession.begin` 用 `style.get(name, getattr(clip, name))`），与 transient 扁平值比较后会把 cue 扁平值（0）作为“变化”提交——静默给 cue 写入样式覆盖、抵消轨道默认值，并可能压入无操作 undo 项。
  - 建议: end_drag 的 final 与 original 取同一空间（都从 resolved style 或都从扁平字段读取），或在 delta 比较时排除“两空间基线差”字段；下游 before/after 比较统一空间。
  - 置信度: 中（机制已沿 4 个文件核实；触发前提是轨道级 default_style 覆盖变换字段，属受支持特性）

- [AUD-21009] [Suggestion] 多处宽泛 except 静默吞错（无日志）
  - 位置: src/features/edit/ui/subtitle_handle_controller.py:151-152,158-160,309-310,587-588
  - 证据:
    ```python
    except Exception:
        return (-1.0, -1.0, 1.0, 1.0)      # _get_image_ndc_rect
    except Exception:
        pass                                # _get_device_pixel_ratio
    except Exception:
        return False                        # hit_test_body
    except Exception:
        pass                                # rotate 连接线绘制
    ```
  - 影响: 命中测试/坐标换算持续性故障会让 body 拖拽、手柄命中静默失效且无任何日志（对比同文件 `_get_subtitle_bbox`/`draw_handles` 均有 logger.exception），违反宽泛 except 铁律的排查性要求。
  - 建议: 至少补 `logger.exception(...)`；`_get_image_ndc_rect` 的静默回退 (-1,-1,1,1) 会把手柄画到整幅画布，建议回退时直接返回 None 并跳过绘制。
  - 置信度: 高

- [AUD-21010] [Suggestion] update_drag 中 _apply_preview_delta 位于 try 块外，数据类校验异常可逃逸进 Qt 事件派发
  - 位置: src/features/edit/ui/subtitle_handle_controller.py:407,463-476
  - 证据:
    ```python
        except Exception:
            logger.exception("compute_transform_delta_params failed")
            return
    ...
    self._apply_preview_delta(delta)          # try 之外
    ...
    self._selected_clip = replace(clip, **updates)   # 触发 __post_init__ 校验
    ```
  - 影响: `dataclasses.replace` 会重跑 SubtitleClip.__post_init__ 的枚举/范围校验（timeline_v2.py:1357 起，非法值 raise ValueError）；一旦 delta 值异常，异常从 mouseMoveEvent 逃逸，PyQt6 默认对事件重载中的未处理异常会终止程序。当前 compute 产出值基本合法，属低概率健壮性缺口。
  - 建议: 将 `_apply_preview_delta` 纳入 try/except 并 logger.exception。
  - 置信度: 中

- [AUD-21011] [Suggestion] 死变量 is_body、未使用的 import math、回调属性缺类型注解
  - 位置: src/features/edit/ui/subtitle_handle_controller.py:34,104-106,323
  - 证据:
    ```python
    import math                    # 全文件唯一 "math" 出现，未使用
    ...
    self.on_drag_started = None    # 无 Callable 注解
    ...
    is_body = (handle_id is None)  # 赋值后从未读取
    ```
  - 影响: 冗余导入与死变量降低可读性；三个回调属性无 `Optional[Callable[[str], None]]` 等注解，注入方（program_viewer）无法获得类型契约校验（维度 9）。
  - 建议: 删除 `import math` 与 `is_body`；回调属性补 Callable 注解。
  - 置信度: 高

- [AUD-21012] [Suggestion] 跨模块访问 viewer 私有方法 `_calculate_display_rect`
  - 位置: src/features/edit/ui/subtitle_handle_controller.py:149
  - 证据:
    ```python
    x, y, w, h = self._viewer._calculate_display_rect()
    ```
  - 影响: ProgramViewer 的私有 API 无稳定性承诺，重构（签名/返回约定 (x,y,w,h)）会静默破坏手柄定位；当前靠外层 except 吞掉进一步掩盖破坏。
  - 建议: 在 ProgramViewer 提供公有 `display_rect_ndc()` 访问器，控制器改用之。
  - 置信度: 高

### 通过项
- 线程安全：控制器仅由 UI 线程的 ProgramViewer 事件与 paintGL(QPainter) 驱动，无跨线程共享可变状态。
- GPU 资源生命周期：绘制走 QPainter（paintGL 内由 viewer 调用），未触碰 ModernGL 对象/纹理，无泄漏面。
- GLSL/HDR：不涉及着色器与 HDR 数据。
- 信号防递归：通过回调单向通知 viewer，viewer 经 16ms 合并器回写 set_clip；set_clip 同 id 不清 drag 状态（118-130 行），不会打断进行中的拖拽，无递归环。
- 异常与取消路径：hit_test/_get_subtitle_bbox/_compute_handles/draw_handles 均有 logger.exception 保护；bbox 度量失败回退整画布。
- 性能模式：bbox 缓存键覆盖文本/字体/排版/描边/阴影/画布/样式（188-199 行），正确排除与 glyph bounds 无关的 transform 字段；命中测试过滤 center 死手柄避免与 pivot 抢命中；`_screen_to_image_px` 对退化 NDC 矩形有 1e-6 防护。
- 几何正确性抽核：custom pivot 的 canvas↔bbox 双向换算（214-222、369-377、396-401 行）对称；`_point_in_quad` 凸四边形同号判定正确。

---

## src/features/edit/ui/timeline_interaction.py (行数: 232)

### 发现

- [AUD-21013] [Suggestion] finish() 与 cancel() 状态重置不对称，残留 original_* 脏值
  - 位置: src/features/edit/ui/timeline_interaction.py:201-220
  - 证据:
    ```python
    def cancel(self) -> None:
        ...
        self.original_start = 0
        self.original_duration = 0
        self.original_source_start = 0
        self.original_source_duration = 0
        self.original_value = 0.0
        ...
    def finish(self) -> None:
        self.state = InteractionState.IDLE
        self.primary_clip_id = None
        self.primary_track_id = None
        self.threshold_passed = False
        self.clip_drag_session = None   # original_* 未清
    ```
  - 影响: 交互结束后 original_start/duration/source/value 残留上一次会话的值；若后续会话 `begin()` 未显式传入这些 kwargs（begin 仅覆盖传入项），读取者（timeline_view.py:9383-9387 取消恢复路径）将拿到跨会话脏数据。当前读取点都在活动会话内，属潜伏风险。
  - 建议: 抽取 `_reset_session_fields()` 供 cancel/finish 共用，统一清空。
  - 置信度: 高

- [AUD-21014] [Suggestion] begin() 对未知 kwargs 静默忽略，拼写错误无告警
  - 位置: src/features/edit/ui/timeline_interaction.py:178-188
  - 证据:
    ```python
    for k, v in kwargs.items():
        if hasattr(self, k):
            setattr(self, k, v)
    ```
  - 影响: 调用方把 `origianl_start` 之类的键拼错时被静默丢弃，会话以默认 0 起步，拖拽基线错误且无异常提示，排查成本高。
  - 建议: 对未知键 raise ValueError 或 logger.warning。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程状态机数据类，无可变共享。
- GPU/GLSL/HDR：不涉及。
- 信号防递归：无信号，无递归面。
- 状态提交一致性：严格遵循 P2-3/P2-4（move 仅 preview、release 才提交命令）与 P2-5（cancel 全量复位）；MoveGroup.is_valid 校验非负 start。
- 性能模式：P2-6 拖拽阈值 + last_preview_key/snap_points/drag_track_bounds_by_type 热路径缓存设计合理。
- 类型注解：完整（含 `tuple | None` 现代写法）。
- 数据类设计：值类型 MoveGroupMember/MoveGroup frozen，防意外突变。

---

## 汇总

| 文件 | Critical | Major | Suggestion |
|---|---|---|---|
| subtitle_clip_item.py | 0 | 0 | 5 (AUD-21001~21005) |
| subtitle_delivery_dialog.py | 0 | 0 | 2 (AUD-21006~21007) |
| subtitle_handle_controller.py | 0 | 1 (AUD-21008) | 4 (AUD-21009~21012) |
| timeline_interaction.py | 0 | 0 | 2 (AUD-21013~21014) |
| 合计 | 0 | 1 | 13 |

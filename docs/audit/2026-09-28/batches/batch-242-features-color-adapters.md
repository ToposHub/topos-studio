# 批次报告：batch-242 — src/features/color/adapters//

编号区间：AUD-24200–AUD-24299。审查日期：2026-09-28。
审查方式：通读全部 2 个文件，并交叉核对调用方（page_controller / edit_controller / file_controller）、
面板 setter（color_wheel / curves / hsl / mixer / soft_clip / lut_gallery / denoise）、
`ColorStackData`（src/shared/project/color_stack.py）、`Layer.to_dict/from_dict`
（src/features/color/core/layer.py）、`TrackingStore.from_dict`、`ImageEngine.render_pipeline /
set_video_denoise_state` 的真实实现。

---

## src/features/color/adapters/__init__.py (行数: 7)

### 发现

（无）

### 通过项
- 通过（无逻辑）：文件仅含包级 docstring（说明 Color 与 MainWindow 深度耦合、此 adapters 层为未来模块化剥离提供一致接口模式），无 import、无导出、无可执行代码，不存在任何审查维度风险。

---

## src/features/color/adapters/color_stack_hydrator.py (行数: 422)

### 发现

- [AUD-24200] [Major] 全量恢复对工程文件来源的 layers 快照零校验，单条损坏即中断整个水化并留下半应用状态
  - 位置: src/features/color/adapters/color_stack_hydrator.py:74-85
  - 证据:
    ```python
    layers_snapshot = getattr(stack, "layers", None) or ()
    if layers_snapshot:
        from src.features.color.core.layer import Layer

        restored = [Layer.from_dict(dict(d)) for d in layers_snapshot]
        engine.layers = restored
        idx = int(getattr(stack, "active_layer_index", 0) or 0)
        engine.active_layer_index = idx if 0 <= idx < len(restored) else 0
    ```
  - 影响: `stack.layers` 源自工程文件（外部数据）。`ColorStackData.from_dict`（color_stack.py:316 `layers=tuple(layers) if isinstance(layers, (list, tuple)) else ()`）只校验容器类型，不校验逐条内容；此处 `dict(d)` 对非映射条目抛 TypeError，`Layer.from_dict` 对如 `"blend_mode": null` 会在 layer.py:1014 `bm.title() if bm.lower() != "normal"` 抛 AttributeError。异常发生时视频降噪已在第 60 行被重置、`engine.layers` 仍是上一个 clip 的图层、tracking_store 未替换——半应用状态；file_controller.py:5641-5645 以 `except Exception` + debug 日志吞掉该异常，用户切 clip 后静默看到上一个 clip 的调色（渲染错误状态）。违反"外部数据须验证"铁律。
  - 建议: 恢复循环逐条 try/except，损坏层跳过并 warning（参照 TrackingStore.from_dict 的单条容错模式），并保证 `engine.layers` 替换为原子步骤、失败时整体回退；或在 `ColorStackData.from_dict` 读端做逐条结构校验。
  - 置信度: 高

- [AUD-24201] [Major] collect_stack_from_engine_and_panels 不采集 video_denoise，与 apply 路径不对称，按其文档用途作保存时会丢 per-clip 降噪状态
  - 位置: src/features/color/adapters/color_stack_hydrator.py:265-280
  - 证据:
    ```python
    return ColorStackData(
        stack_id=stack_id,
        asset_id=None,
        name="Default",
        grading_params=grading_params,
        ...
        trackings=trackings_data,
        layers=layers_snapshot,
        active_layer_index=active_layer_index,
        raw_params=raw_params,
    )
    ```
    构造参数中没有 `video_denoise`（默认 None）；而 apply 侧（59-72 行）会恢复该键。
  - 影响: 方法 docstring 自称 "Used when saving"。若在任何保存/切换 flush 边界用它收集，clip 的 per-clip 视频降噪状态（P1-COLOR-13）被静默清为 None——切回该 clip 时 `apply_stack_to_engine` 第 60 行先置 `engine.video_denoise_params = None` 且无状态可恢复，降噪关断。当前生产代码未调用此方法（仅 tests/color/test_color_stack_hydrator.py 等测试使用；生产走 `EditProject.update_color_stack` 逐字段更新），属带真实数据丢失语义的潜伏缺陷——一旦有人按 docstring 接线即触发。
  - 建议: 在 collect 中从 `engine.get_video_denoise_state()`（image_engine.py:845，None 表示未启用）读取并传入 `video_denoise=`；或修正 docstring 明示"不含 video_denoise，勿用于保存边界"。
  - 置信度: 高（不对称为代码事实；生产影响为潜伏）

- [AUD-24202] [Major] 快照往返使用 to_dict(None)/from_dict 无 assets_dir，BITMAP 蒙版（AI 蒙版）与 AI matte 不进快照，切 clip 即丢失
  - 位置: src/features/color/adapters/color_stack_hydrator.py:203、78
  - 证据:
    ```python
    snapshots = [
        snapshot
        for snapshot in (
            layer.to_dict(None) for layer in engine.layers
        )
        if isinstance(snapshot, dict)
    ]
    ```
    ```python
    restored = [Layer.from_dict(dict(d)) for d in layers_snapshot]
    ```
  - 影响: `MaskShape.to_dict`（layer.py:183 注释原文 "If no assets_dir, bitmap_data is not persisted (will be lost)"）与 `Layer.to_dict` 的 ai_matte 分支（layer.py:977 需 `assets_dir and os.path.isdir(assets_dir)`）在 assets_dir=None 时均不序列化位图数据。AI 蒙版（SAM/BiRefNet）正是以 `MaskShape.BITMAP` + `bitmap_data` 存储（image_engine.py:1588-1590），会话内切 clip 走本文件"collect 快照 → apply 全量恢复"往返后 AI 蒙版/ai_matte 丢失；随后保存工程会把这个已丢蒙版的状态固化为权威快照（持久化数据丢失）。注：`to_dict(None)` 是全库系统性约定（edit_controller.py:7397、save_router.py:1055 等采集侧同样如此），磁盘持久化路径（save_router.py:1305）才传 assets_dir，故根因跨文件，但本文件是往返的恢复端与采集端之一。
  - 建议: 为内存快照提供 inline 序列化通道（如 `to_dict` 在 assets_dir=None 时把 bitmap_data 压缩为 base64/npz 内嵌，from_dict 对应识别），或在快照往返处改用携带 assets_dir 的临时目录；短期至少在恢复端检测到 BITMAP shape 丢失 bitmap_data 时打 warning。
  - 置信度: 中（丢失链路代码成立；根因为系统性约定，修复点跨文件）

- [AUD-24203] [Suggestion] collect 构造结果硬编码 name="Default"、asset_id=None，丢弃原栈身份元数据（scope/timeline_id/clip_id/created_at 等）
  - 位置: src/features/color/adapters/color_stack_hydrator.py:265-268
  - 证据:
    ```python
    return ColorStackData(
        stack_id=stack_id,
        asset_id=None,
        name="Default",
        grading_params=grading_params,
    ```
  - 影响: `ColorStackData` 的 `__hash__`/`__eq__` 仅基于 stack_id（color_stack.py:324-332），收集结果可直接替换存储中的同名栈而编译通过；替换后 name/scope/source_kind/timeline_id/clip_id/created_at/modified_at 全部回到默认，栈归属信息（如 clip_local/timeline_clip）丢失。当前仅测试调用，属潜伏风险。
  - 建议: 让 collect 接收原栈（或其元数据）并用 `dataclasses.replace`/`with_layers` 派生新栈，仅刷新参数与快照字段。
  - 置信度: 高（代码事实；影响潜伏）

- [AUD-24204] [Suggestion] 采集的 layer.lut_intensity 未做 0.0–1.0 边界校验，越界外部数据会让 ColorStackData 构造抛 ValueError 打断保存
  - 位置: src/features/color/adapters/color_stack_hydrator.py:229-230、274-275
  - 证据:
    ```python
    lut_path = getattr(src, "active_lut_path", None)
    lut_intensity = getattr(src, "lut_intensity", 1.0)
    ```
    ```python
    lut_path=lut_path,
    lut_intensity=lut_intensity,
    ```
  - 影响: `Layer.from_dict`（layer.py:1035 `layer.lut_intensity = data.get("lut_intensity", 1.0)`）对工程文件值不校验；本方法把它原样传入 `ColorStackData(...)`，而其 `__post_init__`（color_stack.py:111-112）对越界值 `raise ValueError`。含越界强度的旧/手工工程会在保存收集路径以异常中断。当前 collect 仅测试使用，属潜伏。
  - 建议: 采集侧 clamp 到 [0.0, 1.0]（或在 `Layer.from_dict` 读端 clamp，根治外部数据未验证）。
  - 置信度: 中

- [AUD-24205] [Suggestion] _read_soft_clip_params 宽泛 except 返回部分结果，半截参数会被当作权威值保存
  - 位置: src/features/color/adapters/color_stack_hydrator.py:418-422
  - 证据:
    ```python
    for key, ctrl in controls_map.items():
        result[key] = ctrl["spin"].value()
    except Exception:
        logger.debug("Failed to read soft_clip_params from panel", exc_info=True)
    return result
    ```
  - 影响: 循环中途抛错时（如某控件缺 "spin" 子控件），result 已含部分键（如 enabled 但缺 exposure），调用方将其作为完整 soft_clip_params 使用，可能用残缺参数覆盖栈中完整参数（静默数据劣化）；且仅 debug 级日志，生产日志不可见。违反"不得宽泛 except 掩盖领域错误"。
  - 建议: 失败时返回空 dict（或回退栈中现值）并升级为 warning 日志；逐控件读取前校验结构。
  - 置信度: 高

- [AUD-24206] [Suggestion] video_denoise 恢复用 except Exception 以 debug 级吞掉引擎/参数反序列化错误
  - 位置: src/features/color/adapters/color_stack_hydrator.py:59-72
  - 证据:
    ```python
    try:
        engine.video_denoise_params = None
        vd_state = getattr(stack, "video_denoise", None)
        if vd_state and hasattr(engine, "set_video_denoise_state"):
            engine.set_video_denoise_state(vd_state)
        elif vd_state:
            ...
            engine.video_denoise_params = VideoDenoiseParams.from_dict(vd_state)
    except Exception:
        logger.debug("apply_stack_to_engine: video_denoise restore "
                     "failed", exc_info=True)
    ```
  - 影响: try 块覆盖面过宽（含 `engine.video_denoise_params = None` 与引擎 setter），`VideoDenoiseParams.from_dict` 对损坏工程数据抛错时降噪静默保持关闭且生产日志不可见（debug 级），用户无法察觉回显失败原因。
  - 建议: 缩小 try 范围到 `from_dict` 调用，日志升级为 warning；对 `vd_state` 先做 isinstance(dict) 校验。
  - 置信度: 高

- [AUD-24207] [Suggestion] _apply_video_denoise_to_panel 缺少参数类型注解
  - 位置: src/features/color/adapters/color_stack_hydrator.py:154
  - 证据:
    ```python
    def _apply_video_denoise_to_panel(self, stack, panel) -> None:
        """P1-COLOR-13: 栈的视频降噪参数回显到降噪面板（视频模式页）。"""
    ```
  - 影响: 全类唯一无注解的方法（同类 helper 均为 `stack: ColorStackData, panel: Any`），降低一致性并使静态检查失效。
  - 建议: 改为 `def _apply_video_denoise_to_panel(self, stack: ColorStackData, panel: Any) -> None:`。
  - 置信度: 高

### 通过项
- 线程安全：本类仅在 UI 线程被调用（page_controller/_load_color_stack_to_grading_panel、edit_controller 粘贴路径）；自身无 OpenGL/ModernGL 调用；`hydrate` 末尾的 `engine.render_pipeline()` 是全库标准入口，引擎内部经 `gl_manager.mutex` 向 AsyncRenderer 提交（image_engine.py:2743），未违反"UI 线程不得操作 OpenGL"。
- GPU 资源生命周期：不创建/持有纹理、缓冲等 GPU 对象；Layer 重建为纯 Python 数据，`engine.layers` 整表替换（引用交换，GIL 下原子），旧层对象由 GC 处理，无泄漏路径。
- 信号防递归：委托的面板 setter 均自带防护——GradingPanel.set_params（color_wheel.py:1092 `_syncing_ui` + blockSignals + emit=False）、CurvesWidget.set_curves_data(emit_changed=False)、HSL 面板内部转发到 widget 默认 emit_changed=False（hsl_curves_widget.py:762）、MixerPanel.set_params（mixer_panel.py:230 `_syncing_ui`）、SoftClipPanel.set_params(emit=False)（soft_clip_panel.py:439-463 blockSignals）、LutGallery.set_selected_lut/set_intensity（blockSignals + `_intensity_updating`）、denoise `set_video_params_dict`（`_video_updating`）；两处生产调用方均以 `_color_stack_hydration_in_progress` 包裹（page_controller.py:2220/2756、file_controller.py:5638、edit_controller.py:7317/7433），无递归写回风险。
- GLSL/HDR 正确性：本文件不生成着色器代码、不做像素值 clamp；参数以原值透传引擎，HDR 数据路径不受影响。
- 状态提交一致性（apply 侧）：v2 完整恢复按 `stack.layers` 重建并对齐 `active_layer_index`（越界回退 0）；v1 回退按设计广播平铺键；tracking_store 整体替换防跨 clip 泄漏，且 `TrackingStore.from_dict` 对旧工程/单条损坏有容错（tracking_store.py:86-100）；video_denoise 先重置再恢复防串扰（P1-COLOR-02/13 契约落实）。
- 性能模式：`hydrate` 聚合为单次 `render_pipeline()`（render_reason='full'，正确触发蒙版纹理更新与 ColorOutput 标脏），无逐面板重复渲染；`_grade_clip_layers` 等重活不在本文件；无 4K 热路径上的冗余拷贝（快照 dict 拷贝仅发生在持久化边界）。
- 取消路径：本类不发起异步任务/定时器，无需要求取消清理。
- 输入验证（部分通过）：面板侧容错良好（panels.get 缺面板跳过、`_apply_video_denoise_to_panel` 检查 callable、collect 对缺 to_dict 的旧引擎/替身回退面板收集）；layers 快照条目与 lut_intensity 的校验缺口已列为 AUD-24200/24204。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-24200、AUD-24201、AUD-24202 |
| Suggestion | 5 | AUD-24203–AUD-24207 |

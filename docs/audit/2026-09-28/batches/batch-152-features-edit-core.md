# batch-152 审查报告 — src/features/edit/core（4 文件，651 行）

编号区间：AUD-15200–AUD-15299（实际使用 AUD-15200–AUD-15204）
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/edit/core/__init__.py (行数: 162)

### 发现

- [AUD-15200] [Suggestion] 模块 docstring 职责清单与实际导出漂移
  - 位置: src/features/edit/core/__init__.py:6-14
  - 证据:
    ```python
    ## 职责

    - project: EditProject, MediaAsset, ProjectBin, TimelineSequenceRef (P2)
    - clip_params: TransformParams, CropParams, SpeedParams (P3)
    - timeline_v2: TimelineSequence, EditTrack, ClipInstance, TransitionInstance (P3)
    - timeline_ops: 纯函数操作 (P3)
    - commands: 编辑命令和 Undo (P4)
    - selection: 选择模型 (P4)
    - serialization: JSON 序列化/反序列化 (P2)
    - audio_waveform: 音频波形提取和缓存 (P8)
    ```
  - 影响: docstring 声称包含 `timeline_ops`，但该模块未被导入/导出；实际导出的 `timeline_render_plan`（48-59 行）和 `edit_project_io`（30-34 行）反而不在清单中。按 docstring 查找 API 的使用者会漏掉 render-plan 系列函数或误以为 timeline_ops 可从本包导入。
  - 建议: 更新 docstring 职责清单，加入 `timeline_render_plan` 与 `edit_project_io`，并注明 `timeline_ops` 需单独导入（或补充导出）。
  - 置信度: 高

### 通过项

- 纯 re-export 模块，无逻辑：已用 AST 逐一核对，全部 58 个导入名在其源模块（project/serialization/edit_project_io/clip_params/timeline_v2/timeline_render_plan/commands/selection/audio_waveform）中真实存在，且 `__all__` 与导入集合完全一致（无遗漏、无多余、无拼写错误）。
- 经由 audio_waveform 引入 PyQt6（QThread）为 docstring 明示的设计（第 19 行），非隐藏耦合。
- 无线程/GPU/信号/异常路径，不适用其余维度。

---

## src/features/edit/core/adjustment_composite.py (行数: 130)

### 发现

- [AUD-15201] [Suggestion] transform_is_identity 将 pivot_x/pivot_y 计入恒等判定，center 模式下残留 pivot 值触发逐帧全帧 warp
  - 位置: src/features/edit/core/adjustment_composite.py:22-26
  - 证据:
    ```python
        if str(transform[5]) != "center":
            return False
        values = [float(transform[index]) for index in (0, 1, 2, 3, 4, 6, 7)]
    except (TypeError, ValueError, IndexError):
        return False
    identity = (0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0)
    return all(abs(value - expected) <= 1e-4 for value, expected in zip(values, identity))
    ```
  - 影响: `pivot_mode == "center"` 时 pivot_x/pivot_y 不参与仿射计算（apply_transform_cpu:60-63 仅在 "custom" 分支读取），但恒等判定仍要求它们为 0。用户先设 custom pivot 再切回 center 后残留的非零 pivot 会使本应恒等的变换被判为非恒等：预览 CPU 回退（timeline_preview_renderer.py:2860-2867）与导出路径（timeline_export.py:6901-6907）每帧对每个 adjustment 层执行全帧 warpAffine，且 opacity<1 时额外执行 AUD-15202 的 coverage 全帧 warp。结果仍正确（矩阵数学恒等），纯性能浪费。
  - 建议: `pivot_mode != "custom"` 时只比较索引 (0,1,2,3,4)；或在 UI/序列化层把 pivot 归位。
  - 置信度: 高（行为确定；触发依赖残留 pivot 状态）

- [AUD-15202] [Suggestion] blend_adjustment_cpu coverage 掩码分配并变换整个多通道 float32 帧，仅消费 1 个通道
  - 位置: src/features/edit/core/adjustment_composite.py:119-125
  - 证据:
    ```python
    if has_transform:
        coverage_source = np.ones_like(adjusted, dtype=np.float32)
        coverage = apply_transform_cpu(
            coverage_source, transform, base.shape[1], base.shape[0],
            coordinate_w, coordinate_h,
        )
        coverage = np.clip(coverage[..., :1], 0.0, 1.0)
        weight = coverage * alpha
    ```
  - 影响: 4K RGBA 下 `np.ones_like` 每次分配约 33 MB float32，warpAffine 对 3–4 个通道全部做双线性插值后仅取 `[..., :1]`，内存流量约为必要值的 3–4 倍。该函数位于实时 CPU 回退与导出热路径（每个带变换的 adjustment 层每帧调用一次），直接影响 4K CPU 合成吞吐。
  - 建议: 改为构造单通道掩码 `np.ones((h, w, 1), dtype=np.float32)` 传入 apply_transform_cpu（warpAffine 支持单通道），跳过多通道分配与插值。
  - 置信度: 高

### 通过项

- 线程安全：三个函数均为纯函数，无共享可变状态。
- GPU 资源生命周期：纯 CPU（numpy/cv2），不涉及 ModernGL 对象。
- HDR 正确性：全程 float32 线性混合后 `astype(base.dtype)`，无 clamp(0.0,1.0)，HDR 值域保留；warpAffine 对 float 输入不做范围压缩。
- 输入验证：transform 解析包裹 TypeError/ValueError/IndexError，畸形输入按恒等处理；opacity 经 `max(0.0, min(1.0, ...))` 钳制；`coordinate_w/h` 有 `max(1, ...)` 防除零。
- 类型注解：完整；`transform: Any` 为有意的鸭子类型（TransformParams 元组），非缺失。
- 无信号/undo 持久化路径，不适用。

---

## src/features/edit/core/asset_usage_index.py (行数: 137)

### 发现

（无）

### 通过项

- 通过（无逻辑问题）：纯标准库只读模型，与真实数据模型语义逐项核对一致——
  - `end = start + timeline_duration`（110-111 行）与 `ClipInstance.timeline_end` 属性定义完全一致（timeline_v2.py:336-337），非猜测值；
  - `all_tracks` 为 property 返回 tuple（timeline_v2.py:3029-3031），`getattr(sequence, "all_tracks", ())` 鸭子类型访问安全；字幕轨 `clips` 恒为空元组（timeline_v2.py:2241-2243 校验禁止），自然跳过无 asset_id 的字幕片段；
  - `project.assets` 确为 `dict[str, MediaAsset]`（project.py:983），`isinstance(assets, dict)` 分支与真实类型匹配；
  - 调用方（media_pool_panel.py:9842、export_manifest.py:382、consolidate.py:423、project_archive.py:160）均按 `(sequences, project)` 签名传参。
- 输入验证：所有 getattr 带默认值，空 asset_id 跳过，空字符串回退 id 显示（format()）。
- 线程安全/GPU/信号/异常：不可变 frozen dataclass，构建 O(clips)，无共享状态，无适用风险。
- 类型注解完整。

---

## src/features/edit/core/audio_capabilities.py (行数: 222)

### 发现

- [AUD-15203] [Major] 缓存命中时静默忽略 check_output_device=True，跳过的设备探测被缓存为 available=True（未知被当作可用）
  - 位置: src/features/edit/core/audio_capabilities.py:167,184-203
  - 证据:
    ```python
    output_device = _probe_output_device() if check_output_device else CapabilityResult(True, "跳过探测")
    ...
    def get_audio_capabilities(check_output_device: bool = False) -> AudioCapabilities:
        ...
        Args:
            check_output_device: 仅在首次探测时生效；默认 False 以避免 DSP 路径触发 Qt 依赖。
    ...
        global _cached
        if _cached is None:
            with _cache_lock:
                if _cached is None:
                    _cached = probe_audio_capabilities(check_output_device=check_output_device)
        return _cached
    ```
  - 影响: DSP/导出路径默认以 `check_output_device=False` 首触缓存（如 timeline_export.py:736），此时 `CapabilityResult(True, "跳过探测")` 把"未探测"记为"可用"。之后 audio_health.py:268 以 `get_audio_capabilities(check_output_device=True)` 构建音频健康报告，参数在缓存命中时被静默忽略，报告会把从未探测过的输出设备显示为可用——这正是 AM-01 要消除的"UI 无法区分可用/旁路"问题在设备维度的重现。main_window.py:804-805 需要先手工 `reset_audio_capabilities_cache()` 再探测，说明该陷阱已被迫在调用方打补丁，但 audio_health 未打。
  - 建议: 缓存中记录"是否含设备探测"的标记，`check_output_device=True` 且缓存不含设备结果时重探测；或将跳过态改为独立的三态（unknown）而非 available=True，并让 disabled_components 区分 unknown/unavailable。
  - 置信度: 中（机制与调用链确定；实际触发取决于进程内首次探测发生在 DSP/导出路径而非 main_window 启动诊断）

- [AUD-15204] [Suggestion] get/reset 双检锁竞态可向调用方返回 None
  - 位置: src/features/edit/core/audio_capabilities.py:198-203
  - 证据:
    ```python
        global _cached
        if _cached is None:
            with _cache_lock:
                if _cached is None:
                    _cached = probe_audio_capabilities(check_output_device=check_output_device)
        return _cached
    ```
  - 影响: 线程 A 通过 `if _cached is None` 检查后、`return _cached` 前，若线程 B 调用 `reset_audio_capabilities_cache()`（211-213 行置 None），A 返回 None，DSP 热路径 `get_audio_capabilities().eq_lufs_available`（222 行）抛 AttributeError。reset 目前仅测试/启动诊断使用，实际概率低，但 `is_scipy_available_for_dsp` 被音频实时线程调用，属确定性风险模式。
  - 建议: 读入局部变量后返回：`caps = _cached; if caps is None: with lock: caps = _cached or probe...; _cached = caps; return caps`，保证不把 None 泄漏给调用方。
  - 置信度: 高（机制确定，实际触发概率低）

### 通过项

- 探测函数异常路径：各 _probe_* 的 `except Exception` 兜底均将异常摘要写入 reason 返回，不静默吞错，符合"不得宽泛 except 掩盖领域错误"的意图；scipy 探测用 `from scipy.signal import lfilter` 与 DSP 实际依赖点同路径，避免子模块损坏漏检（125-136 行）。
- 惰性缓存主体（锁内二次检查、探测幂等无共享可变状态）线程安全。
- `AudioCapabilities`/`CapabilityResult` frozen 不可变，DSP/UI 共享只读安全。
- 类型注解完整；`check_output_device` 的 headless 逃生门（probe 的参数与 get 的默认分离）有文档说明。
- 无 GPU 资源、无 GLSL/HDR、无信号递归、无 undo/持久化路径，不适用。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 1 | AUD-15203 |
| Suggestion | 4 | AUD-15200, AUD-15201, AUD-15202, AUD-15204 |

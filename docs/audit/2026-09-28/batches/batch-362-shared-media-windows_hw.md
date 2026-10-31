# 批次报告：batch-362 — src/shared/media/windows_hw/

- 编号区间：AUD-36200–AUD-36299
- 审查文件：6 个（1,116 行）
- 交叉验证说明：本批结论核对了对同包 `d3d11_device.py` / `d3d11_texture_pool.py` / `d3d11_frame.py` / `d3d11_format_contract.py` / `d3d11_decoder.py`、父类 `src/shared/media/ffmpeg_source.py`、`src/shared/media/frame.py`、消费方 `src/features/edit/core/edit_gpu_decode_manager.py`、`src/features/edit/core/edit_gpu_preview_renderer.py`、`src/shared/media/factory.py` 的真实代码；DXGI 枚举真值对照 Microsoft Learn 官方文档（dxgiformat.h）。
- 跨批注记：`d3d11_device.py:51-52` 钉死的 `DXGI_FORMAT_NV12 = 65` / `DXGI_FORMAT_P010 = 100` 与 SDK 真值（NV12=103、P010=104；65=A8_UNORM、100=AYUV）不符，根因在该文件（属其他批次），本批两个文件是消费点，见 AUD-36203 / AUD-36206。

## src/shared/media/windows_hw/gpu_encode_surface.py (行数: 298)

### 发现

- [AUD-36206] [Major] DXGI 格式码沿用包内错误常量（NV12=65/P010=100），将错值传过原生桥 ABI 边界
  - 位置: src/shared/media/windows_hw/gpu_encode_surface.py:94,155,259-267（根因 src/shared/media/windows_hw/d3d11_device.py:51-52）
  - 证据:
    ```python
    # d3d11_device.py:50-53（注释声称 "SDK dxgiformat.h 钉死码值"）
    DXGI_FORMAT_NV12 = 65
    DXGI_FORMAT_P010 = 100
    DXGI_FORMAT_B8G8R8A8_UNORM = 87
    ```
    ```python
    # gpu_encode_surface.py:154-156
    pixel_format = (
        "p010" if self._dxgi_format == 100
        else "nv12" if self._dxgi_format == DXGI_FORMAT_NV12
    ```
    ```python
    # gpu_encode_surface.py:260-262 —— dxgi_format 原样进入原生桥
    raw = self._native_bridge.submit_encode(
        surface.texture_ptr,
        surface.dxgi_format,
    ```
  - 影响: 包内 65/100 互相一致（`d3d11_decoder.decode_next_gpu_frame` 同样映射 p010→100/nv12→65），Python 侧校验链自洽；但 `submit_to_encoder` 把该值作为 DXGI 格式码传给 `topos_hwdecode_windows` 原生扩展（NVENC/AMF/oneVPL 期待 SDK 真值），真值 65=A8_UNORM、100=AYUV（SDK NV12=103、P010=104，见 Microsoft Learn DXGI_FORMAT）。原生桥一旦落地，编码器会按错误格式解释 surface，导致提交失败或编码错乱。同批 `windows_zero_copy_probe.py` 的 CheckFormatSupport/纹理池层也被同一错误污染（AUD-36203）。
  - 建议: 在 d3d11_device.py 修正两个常量为 SDK 真值（103/104）并在真机重跑 probe 门禁；本文件只需把魔法数 100 换成 `DXGI_FORMAT_P010`（见 AUD-36207）。若包内曾按 65/100 做过"真机验证"，需复核那些验证实际测的是 A8_UNORM/AYUV。
  - 置信度: 高（常量差值已对照官方文档核实；运行时影响评级保守为 Major，因原生桥尚未落地）

- [AUD-36207] [Suggestion] P010 判定用魔法数字面量 100，未用已导入的命名常量 DXGI_FORMAT_P010
  - 位置: src/shared/media/windows_hw/gpu_encode_surface.py:155,209,213
  - 证据:
    ```python
    pixel_format = (
        "p010" if self._dxgi_format == 100
        else "nv12" if self._dxgi_format == DXGI_FORMAT_NV12
        else "unknown"
    )
    ```
    ```python
    frame_format = (
        "p010" if frame.dxgi_format == 100
        else "nv12" if frame.dxgi_format == DXGI_FORMAT_NV12
        else "unknown"
    )
    ```
  - 影响: 同一表达式里 NV12 用命名常量、P010 用字面量，风格割裂；若常量值被修正（AUD-36206），字面量会被漏改，形成隐性格式判定错位。
  - 建议: 统一 `from .d3d11_device import DXGI_FORMAT_P010` 并替换三处字面量。
  - 置信度: 高

### 通过项
- 线程安全：无跨线程共享可变状态；`_last_completion_fence`/`_last_gpu_submit_ms` 每次提交先清零，无陈旧值泄漏
- GPU 资源生命周期：lease 语义正确——`release`/`release_after` 委托 `TextureLease` 协议（已验证 `d3d11_texture_pool.py:71-86` 定义了 `release_after(fence)`），"恰一次归还"由 lease 层强制；`surface_from_frame` 失败路径不构造 surface、不消耗 lease（与 docstring 承诺一致）
- 信号防递归：不涉及 Qt 控件/信号
- GLSL·HDR：不涉及 shader；fail-closed 语义明确（`submit_to_encoder` 无原生桥返回 `not_yet_implemented`，绝不做 CPU 伪装）
- 异常与取消路径：`submit_to_encoder` 只捕获 `(OSError, RuntimeError, TypeError, ValueError)`（窄化，符合规范）；`_normalize_submit_result` 对 mapping 的类型错误同样收敛为 reason 而非崩溃
- 输入验证：encoder_kind 词表校验（构造 + `GpuEncodeSurface.__post_init__` 双重）、LUID 同卡校验、格式/尺寸匹配、`D3D11FormatContract` 二次校验链完整
- 状态提交一致性：无 undo/持久化职责
- 类型注解：完整

## src/shared/media/windows_hw/hw_planar_source.py (行数: 292)

### 发现

- [AUD-36200] [Critical] 每帧硬编码 bt709/limited 色彩标签并写入动态属性，屏蔽真实流色彩元数据，非 BT.709/full-range/HDR10 素材渲染颜色错误
  - 位置: src/shared/media/windows_hw/hw_planar_source.py:268-272
  - 证据:
    ```python
    # 与 CPU planar 路径一致的颜色缺省（渲染器侧同样默认 bt709/limited）
    decoded.color_range = 'limited'
    decoded.color_matrix = 'bt709'
    decoded.color_primaries = 'bt709'
    decoded.color_transfer = 'bt709'
    ```
    渲染端消费（src/features/edit/core/edit_gpu_preview_renderer.py:95-99，`_decoded_frame_color_tag` 动态属性优先于 metadata）：
    ```python
    value = getattr(decoded_frame, name, None)
    if value in (None, ""):
        metadata = getattr(decoded_frame, "metadata", None)
        value = getattr(metadata, name, None) if metadata is not None else None
    return str(value or default).strip().lower()
    ```
    而父类 probe 已把真实色彩填入 `self._metadata`（ffmpeg_source.py:657-662 读取 stream 的 color_primaries/color_trc/colorspace/color_range）。
  - 影响: 本源输出的 planar `DecodedFrame` 进入预览渲染的 `_upload_and_render_planar`（edit_gpu_decode_manager.py:2627 以 `source_type='vt_planar'` 接入同一链路）。由于动态属性非空，`_decoded_frame_color_tag` 永远取到硬编码值，`metadata.color_matrix`（可能为 bt2020nc/smpte2084/full）被永久屏蔽：10-bit HEVC HDR10（P010 路径正是为此保留）会被按 BT.709 limited 做 YUV→RGB，full-range 素材被按 limited 压缩——均为可见渲染错误。渲染端 helper 的 docstring 明确记载过同类历史缺陷（"silently selected BT.709/limited ... for any full-range or non-BT.709 source"），此文件以覆盖动态属性的方式将缺陷重新引入 HW 路径；注释声称"与 CPU planar 路径一致"，但父类 `_convert_frame_planar` 实际不设置这些动态属性（靠 metadata 兜底），macOS VT 路径（macos_vt/source.py:274-279,684-700）则读取真实流值。
  - 建议: 删除硬编码，改从 `self._metadata` 读取（`getattr(self._metadata, 'color_matrix', 'bt709')` 等），缺省才落 bt709/limited；与 VT 路径对齐。
  - 置信度: 高（写入端、优先级逻辑、metadata 来源、渲染链路均已逐行核实）

- [AUD-36201] [Major] open() 失败路径不清理已打开的 hwaccel 容器，`_is_open` 状态失真，消费方捕获异常后泄漏容器句柄
  - 位置: src/shared/media/windows_hw/hw_planar_source.py:102-104,143-149,159
  - 证据:
    ```python
    self._reopen_with_hwaccel(decoder)
    self._verify_hw_output()
    ```
    ```python
    try:
        self._container = av.open(self._path, hwaccel=accel)
    except Exception as e:
        raise MediaOpenError(
            f"hwaccel open failed ({decoder.value}): {e}",
    ```
    ```python
    self._is_open = True   # _reopen_with_hwaccel 末尾已置位
    ```
  - 影响: (1) `_reopen_with_hwaccel` 成功、`_verify_hw_output` 抛 `MediaOpenError`（流不可硬解时的常态路径）时，新 hwaccel 容器保持打开、`_is_open=True`、`_hw_active=False`，open() 失败却留下半初始化对象；(2) `av.open(hwaccel=...)` 本身失败时，`_is_open` 仍残留父类 `super().open()` 置的 True 而 `_container=None`。父类失败路径的既定惯例是显式 `self.close()`（ffmpeg_source.py:1101-1103 注释："失败路径也必须释放句柄，不能等对象析构"）。两个消费方（edit_gpu_decode_manager.py:2628-2633、factory.py:1171-1178）在 except 分支均未调用 `close()`，失败即泄漏打开的 av 容器（依赖 GC 兜底，违反项目自身资源治理规则）。
  - 建议: `open()` 内将 `_reopen_with_hwaccel`/`_verify_hw_output` 包入 try/except，失败时调用 `self.close()` 再 raise `MediaOpenError`（对齐父类模式）。
  - 置信度: 高（三条路径与消费方 except 分支均已核实）

- [AUD-36202] [Suggestion] 重开容器时用裸 `except Exception: pass` 吞掉 close 错误
  - 位置: src/shared/media/windows_hw/hw_planar_source.py:135-139
  - 证据:
    ```python
    if self._container is not None:
        try:
            self._container.close()
        except Exception:
            pass
        self._container = None
        self._video_stream = None
    ```
  - 影响: 违反 .clinerules "不得宽泛 except 掩盖领域错误"（父类 close() 同场景至少 `logger.warning`，ffmpeg_source.py:1128-1131）；容器 close 失败被静默，诊断链无痕迹。
  - 建议: 改为 `except Exception as e: logger.warning(...)`，与父类 close() 一致。
  - 置信度: 高

### 通过项
- GLSL·HDR 正确性：P010 以 MSB 对齐按 p016le/16bit 契约原样上传（0.096% 偏差低于 10bit 量化精度），不做无理由 clamp/移位，注释论证充分；PlaneInfo/DecodedFrame 契约与 macos VT planar 源对齐（data=None、双紧凑平面、dtype/stride 正确，`_extract_visible_plane_data` 已核存在且处理 stride/对齐）
- 线程安全：与父类同样按单解码线程使用，无共享可变状态；正确保留"hwaccel 与 frame 线程互斥"约束（不设 thread_type='AUTO'）
- 信号防递归：不涉及
- 输入验证：decoder 词表 + `_DEVICE_TYPE_NAMES` 设备名校验；首帧输出格式门控（非 nv12/p010le/p016le 即抛错回退 CPU），杜绝"看似成功实际软解"
- 性能模式：避免每帧 ~5ms CPU 移位遍历，缩放留给 GPU pass，符合 4K 实时基准取向
- 类型注解：完整

## src/shared/media/windows_hw/native_bridge.py (行数: 200)

### 发现

- [AUD-36208] [Major] `load_windows_native_bridge` 对 `factory()`/`capabilities()` 的异常不做防护，异常逃逸并可使 probe 崩溃，与自身 docstring 承诺矛盾
  - 位置: src/shared/media/windows_hw/native_bridge.py:188-194
  - 证据:
    ```python
    bridge = factory()
    if not isinstance(bridge, WindowsNativeBridge):
        return NativeBridgeLoadResult(
            None, None,
            param(FallbackReason.GPU_PROBE_FAILED, "native_bridge_protocol_mismatch"),
        )
    capabilities = bridge.capabilities()
    ```
    docstring（167-172 行）声称 "Only import/load errors are converted to a fallback result; malformed APIs are reported as probe failures instead of being swallowed"。
  - 影响: import 段只捕获 `(ImportError, OSError)`；`create_bridge()`（编译扩展初始化失败抛 RuntimeError 十分常见）或 `capabilities()` 抛出的任何异常都会原样传播。下游 `D3D11Decoder.open`（d3d11_decoder.py:172-183）在无 try 包裹处调用本函数，`windows_zero_copy_probe.run_probe`（windows_zero_copy_probe.py:131）又直接调用 `decoder.open(file_path)`——原生模块半损坏时整条 probe 以异常崩溃收场，而非返回带 reason 的结构化结果，违背模块"可选组件缺失绝不拖垮宿主"的设计目标。
  - 建议: 将 `factory()` 与 `capabilities()` 包入 try/except（窄化或至少 `Exception` + `param(GPU_PROBE_FAILED, f"create_bridge:{exc}")`），返回 load-failure 结果。
  - 置信度: 高（代码路径无歧义；实际触发依赖原生模块抛错行为，故影响面标中）

### 通过项
- 输入验证：`NativeDecodedTexture.__post_init__`（非空 handle/正尺寸/格式词表/位深一致性/非负耗时）与 `NativeEncodeSubmitResult.__post_init__`（accepted 必带 completion_fence、非负耗时）严格 fail-closed；`gpu_decode_ms` 等缺失保持 None 防止"未测量被当 0"
- GPU 资源生命周期：fence 语义文档明确（lease 归还必须晚于 fence）
- 线程安全：无状态模块
- 异常路径：import 段 `(ImportError, OSError)` 窄化捕获并转 fallback reason
- 可移植性：非 Windows 主机可导入（依赖 Protocol/dataclass，无 Win 专属调用）
- 类型注解：完整

## src/shared/media/windows_hw/reasons.py (行数: 71)

### 发现

（无）

### 通过项
- 通过（无逻辑问题）：纯 reason 词表 + 工具函数。导出 `FallbackReason`（str Enum，值即落盘字符串）、`param()`（参数化 reason 拼接）、`codec_unsupported_reason()`、`reason_prefix()`；三者语义互恰（`param` 产物可被 `reason_prefix` 还原）。注意其硬规则（"每个失败点一个显式 reason 常量，禁止裸字符串散落"）在本批 vendor_policy.py / windows_zero_copy_probe.py 的消费侧存在偏离，见 AUD-36204 / AUD-36209。

## src/shared/media/windows_hw/vendor_policy.py (行数: 105)

### 发现

- [AUD-36209] [Suggestion] 多个失败 reason 为词表外裸字符串，违反 reasons.py 宣称的"禁止裸字符串散落"硬规则
  - 位置: src/shared/media/windows_hw/vendor_policy.py:59,64,69,77,93,104
  - 证据:
    ```python
    if has_staging_copy or has_cross_adapter_copy:
        return VendorABDecision(
            normalized_vendor, normalized_feature, False, 0.0,
            "zero_copy_violation",
        )
    ```
    ```python
    return MultiGpuDecision("", False, "adapter_luid_unavailable", unique)
    ```
    ```python
    return MultiGpuDecision(selected, False, "multi_gpu_gate_not_met", unique)
    ```
    以及 59 行将 vendor 特性未声明复用为 `param(FallbackReason.CODEC_UNSUPPORTED_BY_HW, "vendor_feature_not_declared")`（vendor 特性门控非 codec 能力问题）。
  - 影响: "zero_copy_violation"/"ab_gain_below_15_percent"/"adapter_luid_unavailable"/"multi_gpu_gate_not_met" 均不在 FallbackReason 词表，`reason_prefix()` 归类后无法映射到原子 reason；`codec_unsupported_by_hw:vendor_feature_not_declared` 会污染 codec 归类统计。当前无生产消费方（仅 tests/media/test_windows_hw_pipeline_policy.py 使用），故降为 Suggestion。
  - 建议: 为 vendor/多卡门控补充专用 FallbackReason 成员（如 VENDOR_AB_GAIN_BELOW_GATE、MULTI_GPU_GATE_NOT_MET），或统一走 `param()` 带词表前缀。
  - 置信度: 高

### 通过项
- 输入验证：vendor/feature `strip().lower()` 归一化；A/B FPS 显式 `ValueError`（baseline>0、candidate≥0）；adapter LUID 去重去空
- 状态提交一致性：纯函数决策（frozen dataclass），无副作用
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：不涉及
- 性能模式：15% 增益阈值 + 1e-9 容差与 zero-copy 侵害短路（staging/cross-adapter copy 直接禁用）逻辑正确
- 类型注解：完整

## src/shared/media/windows_hw/windows_zero_copy_probe.py (行数: 150)

### 发现

- [AUD-36203] [Major] "格式支持层/纹理池层"实际检测与创建的是 A8_UNORM(65) 而非 NV12，分层证据失真且可能误放行
  - 位置: src/shared/media/windows_hw/windows_zero_copy_probe.py:82,91-94（根因 src/shared/media/windows_hw/d3d11_device.py:51）
  - 证据:
    ```python
    ok, bits, why = check_format_support(device, DXGI_FORMAT_NV12)
    result.layers["format_support_nv12"] = {
        "ok": ok, "support_bits": hex(bits), "reason": why,
    }
    ```
    ```python
    pool, pool_reason = create_d3d11_texture_pool(
        device.device_ptr, depth=4, width=1920, height=1080,
        dxgi_format=DXGI_FORMAT_NV12, label="zero_copy_probe",
    )
    ```
    而 `d3d11_device.py:51` 定义 `DXGI_FORMAT_NV12 = 65`；SDK 真值 NV12=103，65=A8_UNORM（Microsoft Learn，dxgiformat.h）。
  - 影响: A8_UNORM 是普受支持、可自由创建的格式：即使驱动实际不支持 NV12，格式支持层也会误报 ok；纹理池层创建/lease 循环验证的是 alpha-only 纹理而非半平面 NV12，`layers["texture_pool"]`/`layers["format_support_nv12"]` 写入的诊断证据名不符实——这与模块"分层真实探测、绝不伪报"的核心承诺直接冲突（该结果将进入 ExportGpuTrail 类诊断链）。注释中"P010 在 AMD 上 E_FAIL（RX 6800 实测）"的结论同样可疑（当时实际探测的是 100=AYUV，AYUV 支持本就罕见）。
  - 建议: 修正 `d3d11_device.py` 常量后在真机重跑分层 probe；本文件无需改动（用的是命名常量），但需复核既有真机 probe 脚本的结论有效性。
  - 置信度: 高（常量差值与 API 语义均已核实；对"AMD P010 E_FAIL"归因的修正为推断，标中）

- [AUD-36204] [Suggestion] 分层 reason 以 "xxx_layer:" 作前缀，破坏 reasons.py "前缀即词表原子 reason" 的归类约定
  - 位置: src/shared/media/windows_hw/windows_zero_copy_probe.py:70,87,96,120,134,142
  - 证据:
    ```python
    if device is None:
        result.fallback_reason = f"device_layer:{reason}"
    ```
    而 reasons.py:69-71 约定 `reason_prefix` 以第一段归类：
    ```python
    def reason_prefix(reason: str) -> str:
        """取参数化 reason 的原子前缀（诊断归类用）。"""
        return reason.split(":", 1)[0]
    ```
  - 影响: `reason_prefix("device_layer:gpu_probe_failed:...")` 返回 "device_layer"，不是任何 FallbackReason 成员；诊断归类（backend_selector/UI 按前缀归类）将来接入时会落空。当前 `reason_prefix` 尚无生产调用方，故为 Suggestion。
  - 建议: 保持原子 reason 在首段（如 `gpu_probe_failed:device_layer:<detail>`），layer 信息放 detail 或 `layers` 字段。
  - 置信度: 高

- [AUD-36205] [Suggestion] 未使用的导入 `asdict`
  - 位置: src/shared/media/windows_hw/windows_zero_copy_probe.py:19
  - 证据:
    ```python
    from dataclasses import dataclass, field, asdict
    ```
  - 影响: 全文件无 `asdict` 调用（`layers` 直接用 dict），死导入增加阅读噪音。
  - 建议: 删除 `asdict`。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：device 在外层 `finally: device.release()`、pool 在内层 `finally: pool.close()`、decoder 成功打开后即 `close()`，逐层归还无泄漏路径；lease 循环 acquire/release 严格配对
- 异常与取消路径：device/format/pool 层全部 fail-closed 返回结构化 reason（decode 层的异常逃逸风险源自 native_bridge.py，见 AUD-36208）
- 线程安全：probe 为一次性调用流程，无共享状态；不触碰 OpenGL（不受 UI 线程 GL 铁律约束，但调用方仍应避免在 UI 线程执行重量探测）
- 输入验证：非 Windows 早退返回 `not_windows`；`frames` 经 `max(1, min(frames, 120))` 夹紧，循环上限有界
- 状态提交一致性 / 信号防递归 / GLSL·HDR：不涉及
- 类型注解：完整（`str | None`/`Dict[str, object]` 等）

---

## 汇总

| 严重度 | 数量 | 编号 |
| --- | --- | --- |
| Critical | 1 | AUD-36200 |
| Major | 4 | AUD-36201, AUD-36203, AUD-36206, AUD-36208 |
| Suggestion | 5 | AUD-36202, AUD-36204, AUD-36205, AUD-36207, AUD-36209 |

Critical：hw_planar_source.py 每帧硬编码 bt709/limited 色彩标签，屏蔽真实流色彩元数据（HDR10/full-range 素材渲染颜色错误）。

跨批协查请求：d3d11_device.py（DXGI 常量 65/100 与 SDK 不符的根因，波及本批 AUD-36203/AUD-36206）建议在其所属批次立案。

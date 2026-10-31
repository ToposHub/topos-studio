# batch-360 审查报告：src/shared/media/windows_hw//

- 批次：batch-360 [P2]
- 编号区间：AUD-36000–AUD-36099（实际使用 AUD-36000–AUD-36009）
- 审查日期：2026-09-28
- 审查方式：全量 Read 通读 8 个文件；对相邻模块（reasons.py、native_bridge.py、d3d11_texture_pool.py、gpu_capability.py）与调用方（timeline_export.py、topos_surface_capability.py、tests/）做只读交叉验证；对 ID3D11Device vtable 槽位做了外部权威来源核对（Microsoft Learn、docs.hsp.moe vtable 表）。
- 汇总：Critical 0 / Major 4 / Suggestion 6

---

## src/shared/media/windows_hw/__init__.py (行数: 87)

### 发现
（无）

### 通过项
- 通过（无逻辑）：纯 re-export 包入口，导出 capability / d3d11_device / d3d11_frame / d3d11_texture_pool / native_bridge / d3d11_sync / d3d11_format_contract / d3d11_export_capability / d3d11_render_graph / d3d11_pipeline / d3d11_effects / vendor_policy / gpu_encode_surface / d3d11_passthrough_chain / reasons 的公共符号。
- 线程安全：无共享可变状态。
- GPU 资源生命周期：导入期不加载任何 DLL、不创建原生对象（各子模块均在调用时才 CDLL/probe），非 Windows 平台导入安全。
- 信号防递归 / 状态提交一致性 / GLSL·HDR：无 UI 与 GLSL 逻辑，不适用。

---

## src/shared/media/windows_hw/capability.py (行数: 178)

### 发现

- [AUD-36005] [Suggestion] supported_codecs/supported_pixel_formats 为硬编码声明而非探测结果，且与解码器支持集不一致（缺 av1）
  - 位置: src/shared/media/windows_hw/capability.py:40-41
  - 证据:
    ```python
    supported_codecs: set = field(default_factory=lambda: {'h264', 'hevc', 'h265', 'vc1', 'vp9'})
    supported_pixel_formats: set = field(default_factory=lambda: {'nv12', 'p010'})
    ```
    对比 d3d11_decoder.py:130 `_SUPPORTED_CODECS = {"h264", "hevc", "h265", "vc1", "vp9", "av1"}`。
  - 影响: `is_zero_copy_available(codec)` 的答案来自硬编码集合而非任何真实探测（native bridge 的 decode_codecs 从未被查询）。零拷贝可用时 av1 源会被此 gate 误拒（保守方向）；反之 vp9/vc1 在实际硬件/驱动不支持时 gate 仍放行，只能在解码器 open 阶段才失败。与本模块「诚实探测、绝不猜」的设计原则相悖。
  - 建议: probe 成功后用 `load_windows_native_bridge().capabilities.decode_codecs` 归一化填充 supported_codecs；至少与 d3d11_decoder._SUPPORTED_CODECS 共享单一来源，消除两处漂移。
  - 置信度: 高（不一致与硬编码事实确定；实际调用方影响有限，windows 版 is_zero_copy_available 目前无 src 内调用点）

- [AUD-36006] [Suggestion] 文档声明 TOPOS_VIDEO_ZERO_COPY=auto|off|force，但 force 既未实现也未拒绝，未知值静默按 auto 处理
  - 位置: src/shared/media/windows_hw/capability.py:5-8, 47, 121, 165
  - 证据:
    ```python
    ## 环境变量
    - TOPOS_VIDEO_ZERO_COPY=auto|off|force
    ...
    _zero_copy_env_var = os.environ.get('TOPOS_VIDEO_ZERO_COPY', 'auto').lower()
    ...
    if _zero_copy_env_var == 'off':
    ...
    if _zero_copy_env_var == 'off' or _force_cpu_upload:
    ```
  - 影响: 用户在 Windows 设 force 期望强制零拷贝，实际静默等同 auto（与 macos_vt/capability.py:493 已实现 force 的行为不一致）；拼错的值（如 "of"）也静默按 auto，违反「外部数据须验证」铁律的精神。
  - 建议: 要么显式校验取值 ∈ {auto, off, force} 并对未知值记录告警；要么修正文档移除 force，说明 Windows gate 不接受环境变量强制成功（与 d3d11_export_capability 的 no-force 哲学一致）。
  - 置信度: 高

- [AUD-36007] [Suggestion] _detect_wgl_nv_dx_interop 宽泛 except 静默返回 False 且无任何日志
  - 位置: src/shared/media/windows_hw/capability.py:84-85
  - 证据:
    ```python
        exts = (get_ext() or b"").decode("ascii", errors="replace")
        return "WGL_NV_DX_interop" in exts
    except Exception:
        return False
    ```
  - 影响: ctypes 异常（CDLL 属性错误、WINFUNCTYPE 转型失败等）与「扩展缺失」「无上下文」不可区分；用户机器上零拷贝不可用时 reason 永远是笼统的 wgl_nv_dx_interop_unavailable，无法诊断（docstring 自己记录过这类 bug 曾导致全线误报）。
  - 建议: `except Exception as e: logger.debug("wgl_nv_dx_interop probe failed: %s", e); return False`，保留诚实降级语义同时留下诊断痕迹。
  - 置信度: 高（存在性确定；严重度低）

### 通过项
- 线程安全：`_cached_capability` 全局缓存的并发写入是良性竞争（双方均写入完整有效对象），无撕裂风险。
- 缓存时序正确性：WGL 不可见的负结果不写入永久缓存（line 153 提前 return，跳过 line 159 的缓存写），避免把「本次无上下文」固化成机器事实——与 docstring 声明一致。
- 异常路径：probe 主体异常转为 PROBE_ERROR reason 并 logger.error，fail-closed，不吞错。
- GPU 资源生命周期：本模块不持有任何原生资源。
- 输入验证：codec/pixel_format 大小写归一化后判断；NVIDIA 驱动版本缺失时 gate 拒绝（fail-closed）正确。

---

## src/shared/media/windows_hw/d3d11_decoder.py (行数: 274)

### 发现

- [AUD-36002] [Major] from_native 对 Mapping 直接下标取必需键，缺键抛 KeyError，decode_next 的 except (TypeError, ValueError) 捕不住，异常逃逸到调用方
  - 位置: src/shared/media/windows_hw/d3d11_decoder.py:84-86（异常源）与 224-228（捕获缺口）
  - 证据:
    ```python
    native = NativeDecodedTexture(
        texture_handle=int(value["texture_handle"]),
        width=int(value["width"]),
        height=int(value["height"]),
    ```
    ```python
    try:
        decoded = D3D11DecodedTexture.from_native(raw)
    except (TypeError, ValueError) as exc:
        self._last_fallback_reason = param(FallbackReason.GPU_PROBE_FAILED, f"decode_contract:{exc}")
        return None
    ```
  - 影响: native bridge 是外部 ABI 边界，返回缺 texture_handle/width/height 键的 mapping 时抛 KeyError，绕过「open/decode 失败返回 None + fallback reason」的 fail-closed 契约，直接炸到解码循环调用方（windows_zero_copy_probe 等），违反「外部数据须验证」铁律。
  - 建议: from_native 内对必需键先做存在性检查并抛 ValueError（或 KeyError 一并转为 ValueError）；或 decode_next 的捕获元组加入 KeyError。
  - 置信度: 高

- [AUD-36003] [Major] close() 对 close_decoder 无异常保护且状态清理不在 finally，异常后残留半开状态并污染 open() 复用路径
  - 位置: src/shared/media/windows_hw/d3d11_decoder.py:269-274（及 open() 首行 163 的 self.close()）
  - 证据:
    ```python
    def close(self) -> None:
        if self._session is not None and self._bridge is not None:
            self._bridge.close_decoder(self._session)
        self._session = None
        self._is_open = False
        self._last_native_texture = None
    ```
  - 影响: close_decoder 抛 OSError/RuntimeError（本模块其余 bridge 调用都按此假设防护）时：`_session` 残留、`_is_open` 仍为 True；后续 decode_next 会向状态不明的原生 session 继续解码。且 open() 首行调用 self.close()，异常直接从本应「返回 bool + reason」的 open 抛出，破坏 fail-closed 契约。
  - 建议: try/except 包裹 close_decoder（同 open 的异常元组，记入 _last_fallback_reason），并把 `_session=None / _is_open=False` 放 finally 确保状态一致。
  - 置信度: 中（依赖原生 bridge 是否真的抛异常；但模块自身其余调用点均按会抛防护，此处是唯一缺口）

- [AUD-36004] [Suggestion] dxgi_format 用魔数 100/65 硬编码，绕开 d3d11_device 已导出的格式常量
  - 位置: src/shared/media/windows_hw/d3d11_decoder.py:249
  - 证据:
    ```python
    dxgi_format = 100 if decoded.pixel_format.lower() == "p010" else 65
    ```
  - 影响: 与 `DXGI_FORMAT_P010=100` / `DXGI_FORMAT_NV12=65`（d3d11_device.py:51-52，注释明确「钉死码值」）重复定义；常量修订时此处会静默漂移，且 D3D11GpuFrame.__post_init__ 只校验值域 (65,100)，漂移即契约错误。
  - 建议: `from .d3d11_device import DXGI_FORMAT_NV12, DXGI_FORMAT_P010` 并使用常量。
  - 置信度: 高

### 通过项
- 线程安全：无共享可变状态跨线程暴露（会话亲和由调用方管理，与 d3d11_device 的线程约束声明一致）。
- GPU 资源生命周期：decode_next_gpu_frame 正确将原生句柄包装为 D3D11ExternalTextureLease，释放责任移交帧消费方；close 置空 _last_native_texture 防悬垂引用。
- 输入验证：from_native 拒绝未知字段（fail-closed）；__post_init__ 校验 handle/尺寸/格式/bit_depth 一致性；open 校验文件存在性与 bridge 能力集；尺寸变化 fail-closed（decode_size_changed）；open() 前重置 _width/_height 防上一会话残留误判。
- 异常路径：open/decode_next 对 bridge 调用均有 (OSError, RuntimeError, TypeError, ValueError) 防护并转为 reason（除 AUD-36003 指出的 close 缺口）。
- GLSL·HDR：无 GLSL 代码；HDR 元数据（matrix/transfer/primaries/range）原样透传，未做无理由 clamp。
- 诚实回退：bridge 缺失时 not_yet_implemented fail-closed，绝不静默走 CPU 解码——符合模块契约。

---

## src/shared/media/windows_hw/d3d11_device.py (行数: 396)

### 发现

- [AUD-36000] [Major] VTABLE_SLOT_GET_IMMEDIATE_CONTEXT=27 指向 CreateDeferredContext；GetImmediateContext 实际槽位为 40，注释声称的「真机同源验证」并未覆盖此槽
  - 位置: src/shared/media/windows_hw/d3d11_device.py:59-61, 68-71
  - 证据:
    ```python
    VTABLE_SLOT_CREATE_TEXTURE2D = 5
    VTABLE_SLOT_GET_IMMEDIATE_CONTEXT = 27
    VTABLE_SLOT_GET_FEATURE_LEVEL = 37
    ...
    # ID3D11Device::CheckFormatSupport。实序（真机钉死）：
    # GetImmediateContext=[27]、OpenSharedResource=[28]、CheckFormatSupport=[29]、
    # GetFeatureLevel=[37]（vtable 健全性检查同源验证）。
    ```
  - 影响: 按 SDK d3d11.h 声明序（Microsoft Learn + docs.hsp.moe vtable 表双源一致，且与本文件自证正确的 5/29/37 槽自洽）：27=CreateDeferredContext、28=OpenSharedResource、29=CheckFormatSupport、37=GetFeatureLevel、40=GetImmediateContext。该常量在 `__all__` 中作为「真机钉死」公共契约导出（tests/media/test_windows_hw_d3d11_device_pool.py:432 仅自引用断言 ==27，未实测）：任何消费方经此槽以 GetImmediateContext 原型调用，实际会执行 CreateDeferredContext(UINT, ID3D11DeviceContext**) 拿到 deferred context 却当 immediate context 用——命令永不提交，渲染全错；IMMEDIATE context 的 release 语义也会错配。当前生产代码未实际调用此槽，属埋雷而非现行故障，故定 Major 而非 Critical。
  - 建议: 改为 40，并在真机上按 GetFeatureLevel 同款健全性检查验证（GetImmediateContext 返回的 context 指针非空且 GetDevice 一致）；同注释一并修正「同源验证」的表述，明确哪些槽经过读回验证。
  - 置信度: 高（两个独立外部来源 + 与代码自身已验证槽位 5/29/37 的布局自洽性交叉确认）

### 通过项
- 线程安全：D3D11Device.release() 加锁、先置 _released 再置零指针，重入与重复 release 均 fail-closed（ValueError）；属性访问有 _check_alive 守卫。
- GPU 资源生命周期：release 顺序正确（先 context 后 device，注释阐明引用计数理由）；create_d3d11_device 各失败分支（hr!=0、无 context、vtable 健全性不符）均正确释放已得 COM 引用，无泄漏；_device_adapter_identity 的 QueryInterface/GetAdapter AddRef 与 finally Release 配对完整。
- COM 纪律：GUID 用 Structure+byref（真机 E_POINTER 教训已固化）；DXGI_ADAPTER_DESC_SIZE=304 与 parse 偏移（VendorId=256…LUID=296-304）按 x64 布局（含 SIZE_T 内存三字段）一致。
- vtable 健全性检查：GetFeatureLevel 读回 != 创建返回值即 fail-closed 并释放，防错位崩溃——设计正确（但覆盖面见 AUD-36000）。
- 跨平台导入：WINFUNCTYPE 缺失时回退 CFUNCTYPE，macOS/Linux 可导入、调用期 fail-closed（NOT_WINDOWS）。
- 异常路径：诊断走查（LUID 获取）异常降级为空 LUID + warning，不拖垮 device 创建；主创建路径异常均转 reason 元组返回。
- 类型注解：完整（Tuple/Optional 显式标注）。

---

## src/shared/media/windows_hw/d3d11_effects.py (行数: 60)

### 发现
（无）

### 通过项
- 通过（声明层无副作用）：纯 frozen dataclass 声明表 + 查询函数；不声称机器已加载 kernel，runtime_gate 字段显式标注「native_d3d11_renderer_and_golden_test」，无假成功。
- 输入验证：effect_id/variant strip+lower 归一化；未声明/变体未声明均返回 supported=False + 明确 reason。
- 线程安全 / GPU 生命周期 / GLSL·HDR：无共享可变状态、无原生资源、无 GLSL 代码，不适用。

---

## src/shared/media/windows_hw/d3d11_export_capability.py (行数: 197)

### 发现

- [AUD-36001] [Major] probe_d3d11_export_capability 自动创建的 D3D11Device 在所有返回路径均未 release，每次探测泄漏一份 device+immediate context 的 COM 引用
  - 位置: src/shared/media/windows_hw/d3d11_export_capability.py:121-124（创建点；函数内 8 个 return 均未触碰 device）
  - 证据:
    ```python
    if device is None:
        device, reason = create_d3d11_device()
        if device is None:
            return D3D11ExportCapability(False, f"device:{reason}")
    ```
    其后所有 `return D3D11ExportCapability(...)`（ready=True 的 line 160-176 与 not-ready 的 177-193 等）均不再调用 `device.release()`。
  - 影响: D3D11Device 无 `__del__`，仅持引用计数；Python 对象被 GC 后底层 ID3D11Device 与 immediate context 的 COM 引用仍存活直至进程退出。timeline_export.py:8064/8085 每次后端选择（每次导出请求）都调用本 probe——Windows 真机上每次导出/诊断累积一份原生 device 泄漏；与全库 deferred-release 资源纪律相悖。
  - 建议: 自建 device 时用 try/finally（或 context manager，D3D11Device 已实现 __enter__/__exit__）在返回前 release；ready=True 路径同样需要（capability 只携带 LUID/feature_level 字符串，无需持有 device）。
  - 置信度: 高

### 通过项
- 诚实门控：环境变量无法强制成功；bridge 声明 + T0 实证（validation_reason 校验 LUID 一致、输出可播、CPU 字节数为 0、staging/map/copy 计数为 0、四段 GPU 时间戳齐全非负）双重要求，缺一即 ready=False——与「绝不伪装零拷贝」的模块哲学一致。
- 输入验证：t0_evidence 全字段校验且 reason 精确到字段名；注入参数（device/bridge_module）用 getattr 鸭子探测并显式处理空 LUID。
- 线程安全：无共享可变状态（probe 为纯函数语义，除 AUD-36001 的资源问题）。
- 异常路径：非 Windows / device 失败 / bridge 缺失均显式 reason fail-closed。
- 类型注解：完整（返回 dataclass，to_dict 显式）。

---

## src/shared/media/windows_hw/d3d11_format_contract.py (行数: 141)

### 发现

- [AUD-36008] [Suggestion] color_matrix/color_transfer/color_primaries 为自由字符串，未校验已知词表；拼错的 HDR transfer 会静默落入 SDR bt709 路径
  - 位置: src/shared/media/windows_hw/d3d11_format_contract.py:42-44, 71-73
  - 证据:
    ```python
    color_matrix: str = "bt709"
    color_transfer: str = "bt709"
    color_primaries: str = "bt709"
    ...
    @property
    def is_hdr(self) -> bool:
        return self.color_transfer.strip().lower() in _HDR_TRANSFERS
    ```
  - 影响: transfer 写成 "smpte2084pq"、"SMPTE-2084" 之类笔误时 is_hdr=False，HDR 内容按 SDR bt709 走下游转换（可能被当 SDR 范围处理），产出颜色错误而非显式报错；video_range 有词表校验而三个颜色字段没有，校验力度不一致。
  - 建议: __post_init__ 中对三字段校验 H.273 词表（bt709/bt601/bt2020nc、bt709/smpte2084/arib-std-b67、bt709/bt2020），未知值 raise ValueError，与本文件其余字段的 fail-closed 风格一致。
  - 置信度: 中（风险路径真实，但是否有上游构造点会传入笔误值未在本批次范围内证实）

### 通过项
- 输入验证：格式/bit_depth 映射、video_range 词表、surface_owner 词表、alpha 不允许出现在 YUV surface、HDR 必须 p010+bt2020+bt2020nc、编码必须半平面、P010 编码须原生持有 surface、encoder_kind 词表——策略层校验完整且不触碰驱动资源（与模块定位一致）。
- 线程安全 / GPU 生命周期 / GLSL·HDR：frozen dataclass 纯值对象，无状态无资源；HDR 判定不涉及 clamp。
- 类型注解：完整。

---

## src/shared/media/windows_hw/d3d11_frame.py (行数: 115)

### 发现

- [AUD-36009] [Suggestion] __post_init__ 校验高为偶数但未校验宽为偶数，奇数宽度的 4:2:0 帧可通过 Python 契约层
  - 位置: src/shared/media/windows_hw/d3d11_frame.py:68-69
  - 证据:
    ```python
    if self.width <= 0 or self.height <= 0 or self.height % 2 != 0:
        raise ValueError("宽高必须为正且高为偶数（4:2:0）")
    ```
  - 影响: NV12/P010 的 UV 平面宽度为宽的一半，D3D11 对半平面格式要求宽高均为偶数；奇数宽（如裁切产生的 1919）通过本契约校验后会在原生纹理操作层才失败，违背「契约层尽早拒绝」的本意，错误信息也不如此处清晰。
  - 建议: 校验改为 `width % 2 != 0 or height % 2 != 0`（错误消息同步更新）。
  - 置信度: 中（D3D11 对奇数宽 NV12 的确切行为依驱动而定；「契约不完整」本身确定）

### 通过项
- GPU 资源生命周期：release 单一委托 lease（恰一次语义由 lease 保证）；frozen 数据类不可变；adapter_luid 必填强制（空 LUID 帧不得进入全 GPU 导出判定）；不持有任何 CPU planes（无 planes/data 字段，符合模块硬规则）。
- 输入验证：texture_ptr 非空、DXGI 格式 ∈ 半平面集合、bit_depth 与格式配对、subresource_count 恒 2、GPU 时间戳非负——完整。
- 10-bit HDR 语义：P010 保持 MSB 对齐原样（无 CPU 移位/clamp），与 hw_planar_source 约定一致。
- to_trail_dict 不泄漏指针值，仅含诊断安全字段。
- 线程安全：值对象，无共享状态。类型注解完整。

---

## 附注

- 本批次无 GLSL 代码与 Qt 信号逻辑，「GLSL·HDR 正确性」「信号防递归」「状态提交一致性（undo/redo/revision/持久化）」三个维度在全部 8 个文件中均不适用（无对应代码路径），已并入各文件通过项或不适用说明。
- AUD-36000 的外部核对来源：Microsoft Learn ID3D11Device 接口文档与 docs.hsp.moe 的 ID3D11Device vtable 索引表（27=CreateDeferredContext、40=GetImmediateContext），并与本文件自证正确的 CreateTexture2D=5 / CheckFormatSupport=29 / GetFeatureLevel=37 槽位布局自洽。

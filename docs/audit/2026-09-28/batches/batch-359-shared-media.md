# 批次报告：batch-359 [P2] src/shared/media//

- 编号区间：AUD-35900–AUD-35999
- 审查文件：`src/shared/media/topos_source.py` (2,240 行)、`src/shared/media/topos_surface_capability.py` (227 行)
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 交叉验证（只读）：`src/shared/codec/topos_binding.py`（`make_views`、`_make_decode_request`、`_frame_info_from_c`）、`src/shared/codec/decode_worker_budget.py`、`src/shared/media/frame_path.py`（`DecodeSurfaceLease` 状态机）、`src/shared/media/macos_vt/topos_surface.py`（`ToposIOSurfacePool`）、`src/shared/media/frame.py`（`PIXEL_FORMATS` 注册表）、`src/shared/media/metadata.py`（`Rational` 校验）。另用本机 Python 实测 `sys.getrefcount` 基线（局部变量/attr 与 list 索引均为 2）。

## src/shared/media/topos_source.py (行数: 2240)

### 发现

- [AUD-35900] [Critical] 池化路径 alpha 移位原地替换 `entry.arrays[3]`，原始 alpha 平面被释放而 `entry.views` 仍持其裸地址——下一帧复用池条目时原生解码写入已释放内存（use-after-free 堆损坏）
  - 位置: src/shared/media/topos_source.py:1309（调用点）与 :1996（替换点）
  - 证据:
    ```python
    # read_next_frame 单帧路径——直接传入 entry.arrays 原列表（非拷贝）：
    self._current_frame = index + 1
    return self._assemble_decoded_frame(index, info, entry.arrays, pts,
                                        pool_entry=entry)
    # _assemble_decoded_frame 内（planes 即调用方的 entry.arrays）：
    scratch.flags.writeable = True
    np.right_shift(planes[3], shift, out=scratch)
    scratch.flags.writeable = False
    planes[3] = scratch          # ← entry.arrays[3] 被替换，a_raw 失去最后引用
    ```
    对比同函数两个安全调用点均传拷贝：`:1273-1274 return self._assemble_decoded_frame(index, info, list(arrays), pts, pool_entry=entry)`、`:1796-1797 ... list(arrays) ...`。而 `src/shared/codec/topos_binding.py:1083-1084`（make_views 文档）明确：`视图仅持有数组地址——调用方须自行保持数组存活到解码返回，否则 worker 写入已释放缓冲（堆损坏，远端崩溃难定位）`；views 在条目创建时绑定原始缓冲（`topos_source.py:503-504`）。
  - 影响: 触发条件＝现代 dylib（context+平面池路径）+ alpha 流且 `alpha_bit_depth < 16`（即 Topos 4444 a8/a10/a12，旗舰格式）+ 默认播放（`_ra_depth=0`，每帧都走 1301-1309）。首帧装配后 `planes[3] = scratch` 使原始 alpha ndarray 引用计数归零被释放；消费方释放该帧后 `_entry_free`（实测基线 2）判定条目空闲并复用，`decode_views_request` 经悬垂的 `views[3].pixels` 写入已释放缓冲——4K 下 16MB mmap 释放即崩溃（段错误），或被后续分配复用时表现为静默错 alpha（渲染错误/数据损坏），且如 make_views 文档所言极难定位。
  - 建议: 修复方向二选一并加防御：(1) 1309 行改为传 `list(entry.arrays)`（与 1274/1796 对齐，根因是不该原地改写调用方列表）；(2) `_assemble_decoded_frame` 不再就地改 `planes`，装配局部副本；(3) 另在 `_ToposPlaneEntry` 上维护"views 绑定的原始数组"独立引用，使 views 与 arrays 解耦，从结构上消除该类 UAF。补一条 alpha 流回归测试（连续 ≥ 池容量+2 帧读取）。
  - 置信度: 高

- [AUD-35901] [Suggestion] `_pixel_format_name` alpha 分支不校验未知 (pf, bit_depth, alpha_bit_depth) 组合，与无 alpha 分支的 W1 显式失败姿态不对称，且会生成注册表中不存在的格式名
  - 位置: src/shared/media/topos_source.py:343-346
  - 证据:
    ```python
    if pixel_format == 2:
        return f'topos_gbrap{bit_depth}a{alpha_bit_depth}'
    cf = '444' if pixel_format == 1 else '422'
    return f'topos_yuva{cf}p{bit_depth}a{alpha_bit_depth}'
    # 对照：无 alpha 分支（337-342）对未知组合显式 raise ValueError
    ```
  - 影响: 无 alpha 分支已按 W1 拒绝静默错标，但 alpha 分支对任意组合拼名。`src/shared/media/frame.py` 的 PIXEL_FORMATS 只注册了 p10/p12 的 alpha 变体（无 `topos_yuva422p16a8` 等 16-bit alpha 条目）；一旦出现超出现行档位的 alpha 流（或 pf 域外值），会产出未注册名并在下游以 KeyError 等形式远离源头地失败，而非在 open 时给出可诊断错误。
  - 建议: alpha 分支同样查表（或显式校验 `pixel_format in (0,1,2)`、bit_depth/alpha_bit_depth 属于注册表支持集合），未知组合 raise 与 337-342 同风格的 ValueError。
  - 置信度: 中

- [AUD-35902] [Suggestion] `read_frame_metal` 最外层 `except Exception` 把方法内任何编程错误也吞成 info 级"unavailable"+None，回归缺陷会静默降级为 CPU 路径
  - 位置: src/shared/media/topos_source.py:1216-1222
  - 证据:
    ```python
    except Exception as error:
        self._metal_surface_failures += 1
        logger.info(
            "ToposMediaSource: Metal surface decode unavailable for frame %s: %s",
            frame_index, error,
        )
        return None
    ```
  - 影响: 该方法约 250 行，含纹理导入、lease 状态机、池私有访问等易碎代码；AttributeError/TypeError 等回归同样落入此分支，只留 info 日志与计数器，导出性能 silently 减半且难以从日志发现（正常的能力性降级与 bug 无法区分）。
  - 建议: 缩窄预期异常类型（ToposSurfaceUnavailable/ToposCodecError/ImportError/OSError），对非预期异常类型用 `logger.warning(..., exc_info=True)` 或直接放行。
  - 置信度: 高

- [AUD-35903] [Suggestion] decode worker 预算租约以 `id(self)` 作键，键冲突时 `ValueError` 从 open() 裸抛（未包装为 MediaOpenError）
  - 位置: src/shared/media/topos_source.py:760-763
  - 证据:
    ```python
    self._decode_budget_lease = DECODE_WORKER_BUDGET.acquire(
        f"media:{id(self)}", requested=requested,
        role=self._decode_role, capacity=capacity,
    )
    # decode_worker_budget.py:119-120：if key in self._entries: raise ValueError(...)
    ```
  - 影响: open() 此段仅捕获 `ToposCodecError`（774 行），而 `DECODE_WORKER_BUDGET.acquire` 对重复键抛 ValueError。id 复用 + 旧租约未释放（引用环延迟 GC、`__del__` 未跑）的组合下，新 source 的 open() 会以裸 ValueError 失败而非 MediaOpenError，违背媒体源错误契约。窄路径，但键的稳定性依赖 GC 行为。
  - 建议: 用稳定唯一键（模块级计数器/uuid）；或在该段 `except (ToposCodecError, ValueError)` 并释放已获资源后包装为 MediaOpenError。
  - 置信度: 中

- [AUD-35904] [Suggestion] V9/GOP 读路径每帧全新分配全分辨率平面（无池化），且交付可写平面，违反池/旧路径的 read-only 交付契约
  - 位置: src/shared/media/topos_source.py:939-940
  - 证据:
    ```python
    planes = [np.zeros(g, dtype=np.uint16) for g in geoms]
    ctx.feed(self._movie.packet(target), planes=planes)
    ```
    对照契约（:499-501）：`# 对齐旧路径 read-only 契约（消费方不得就地改写解码帧）`（池路径 `arr.flags.writeable = False`；legacy 路径 frombuffer 天然只读）。
  - 影响: 4K 422 每帧约 25MB 的新分配 + `np.zeros` 清零页触碰，顺序播放速率下造成分配器/缺页抖动（正是 M2/P1-13 想消除的）；同时消费者拿到可写平面，若某消费方依赖只读性防误写则契约不一致（好在此路径无复用，不会跨帧串写）。
  - 建议: V9 路径复用 `_ToposPlanePool` 或至少复用同几何平面组（GOP 缓存本就 ≤1 条），交付前设 `flags.writeable = False` 对齐契约。
  - 置信度: 高

- [AUD-35905] [Suggestion] `read_frame_metal` 直接读取 `ToposIOSurfacePool._slots[0].row_bytes` 私有成员，且取不到时静默传 stride 0
  - 位置: src/shared/media/topos_source.py:1136-1140
  - 证据:
    ```python
    stride_bytes = tuple(
        int(pool._slots[0].row_bytes[i])
        if getattr(pool, "_slots", None) else 0
        for i in range(3)
    )
    ```
  - 影响: 跨对象私有访问使 `ToposIOSurfacePool` 内部重构（如槽惰性创建、改名）在此静默 broken——`_slots` 缺失时 stride=0 传给 `GpuPlaneResource`，下游按 0 步长计算行数会产生错误几何而非显式失败。
  - 建议: 在 `ToposIOSurfacePool` 上提供公共 `row_bytes` 属性/方法；缺失时 raise 而非填 0。
  - 置信度: 高

- [AUD-35906] [Suggestion] `read_audio_samples` 未验证 `ai.channel_count >= 1`，channel_count=0 时 `frame_bytes=0` 导致 ZeroDivisionError
  - 位置: src/shared/media/topos_source.py:1367-1368
  - 证据:
    ```python
    want = min(int(count), total_frames - start)
    bytes_per_sample = ai.bits_per_sample // 8
    frame_bytes = ai.channel_count * bytes_per_sample
    # 随后 _audio_chunk_prefix_index:1333  self._movie.audio_chunk_size(i) // frame_bytes
    ```
  - 影响: 该函数对 `codec`/`bits_per_sample` 做了显式容器字段校验，却漏了 channel_count；若损坏容器的 audio_info 报 channel_count=0，除零异常以非 Media 错误类型逃出音频读取路径。
  - 建议: 与 bits 校验并列加 `if ai.channel_count <= 0: return None`。
  - 置信度: 低（native moov 解析器可能已保证；属 Python 层校验不对称）

- [AUD-35907] [Suggestion] 音频读路径的原生调用异常未包装为 `MediaDecodeError`，与视频路径错误语义不一致
  - 位置: src/shared/media/topos_source.py:1330-1334（另 1357 `audio_info()`、1383 `read_audio_samples`）
  - 证据:
    ```python
    prefix = np.zeros(ai.chunk_count + 1, dtype=np.int64)
    for i in range(ai.chunk_count):
        prefix[i + 1] = prefix[i] + (
            self._movie.audio_chunk_size(i) // frame_bytes)
    self._audio_chunk_prefix = prefix
    ```
  - 影响: `read_next_frame` 把 `ToposCodecError` 统一包装为 `MediaDecodeError`（1304-1307/1315-1318），而 `read_audio_samples` 及索引构建中的 `ToposCodecError`（chunk 元数据损坏/IO 错误）会原样上抛，调用方若只按媒体源契约捕获 Media 系异常会漏接。
  - 建议: 音频路径同样 `except ToposCodecError → MediaDecodeError`（source_path 上下文齐全）。
  - 置信度: 高

- [AUD-35908] [Suggestion] `_audio_codec_display_name` 用魔数 `== 1` 判定 float32，未用已导入的 `TC_AUDIO_FMT_FLOAT32` 常量
  - 位置: src/shared/media/topos_source.py:1865-1866
  - 证据:
    ```python
    if ti.codec == TC_AUDIO_CODEC_LPCM and ti.bits_per_sample == 32 \
            and int(ti.sample_format) == 1:
        return 'pcm_f32be'
    # 对照 read_audio_samples:1405 用 TC_AUDIO_FMT_FLOAT32 判定同语义
    ```
  - 影响: 同一语义两处表达（魔数 vs 常量），绑定层若调整枚举值会只坏一处且难发现。
  - 建议: 统一使用 `TC_AUDIO_FMT_FLOAT32`。
  - 置信度: 高

- [AUD-35909] [Suggestion] 多处跨对象私有属性访问：`self._codec._has_decoder_ctx`/`_has_decoder_batch` 等、`self._plane_pool._max`
  - 位置: src/shared/media/topos_source.py:749、795、799、804-807、1755、1465
  - 证据:
    ```python
    if self._codec._has_decoder_ctx and not self._is_v9:      # :749
    ...
    d['plane_pool_entries'] = self._plane_pool._max            # :1465
    ```
  - 影响: 能力标志散落在 ToposCodec 私有命名里、池容量从 `_max` 直读，重构（改名/语义变化）时此文件静默走错分支（这些分支正是 decode_path 诊断的正确性来源）。
  - 建议: 在 ToposCodec/_ToposPlanePool 上提供公共只读属性（如 `has_decoder_ctx`、`max_entries`）。
  - 置信度: 高

### 通过项
- 线程安全：进程级 `_get_shared_codec` 双检锁 + 三态失败缓存正确（失败原因不误报为媒体损坏）；`_apply_decode_workers` 先局部捕获 `self._decoder_ctx` 再调用，避免与 close() 的竞争；类级"单实例不可重入"契约有明确文档并与上层 SourceManager/ClipSession 锁假设对齐。
- GPU 资源生命周期（除 AUD-35900 外）：`read_frame_metal` 的 lease 状态机异常展开覆盖 C18 两个所有权转交点（内层至 fence、构造段 BaseException 释放后上抛）；close() 对在飞 lease 的可达性设计正确（release 回调闭包持有池与槽，实测 `ToposIOSurfacePool.close` 拒绝带租约关闭时降级 debug 并保活）；`_metal_texture_cache` 以池槽内常驻 surface 对象的 id 为键，池存活期间稳定且有界（≤8）；close 顺序（ctx→lease→pool→device→texture_cache→movie）无提前释放。
- GLSL·HDR 正确性：无 GLSL 代码；uint16 满刻度 planar 语义与 R16UI 上传路径一致；`read_frame_metal` 的 color_range 翻译（1=full）符合 tpcC 语义注释；`_topos_rgb_display_encode` 的 PQ/HLG clip 仅限 8-bit 显示路径且理由成文（符合"有理由 clamp"）。
- 信号防递归：本模块不含 Qt 信号/控件，不适用。
- 状态提交一致性：不涉及 undo/redo/revision/持久化状态，不适用；`_ra_stats`/`_v7b_*` 诊断计数在 close 后保留属可接受设计。
- 异常与取消路径（除已列发现）：open() 元数据段失败统一 `self.close()` 后抛 `MediaOpenError`（含 ValueError 路径，`Rational` 的 den==0 会以 ValueError 被正确捕获）；seek/重定位使 readahead 与 GOP 游标正确失效；批量解码被拒回退单帧且错误语义一致；`__del__`/`__exit__` 幂等关闭。
- 输入验证（除已列发现）：`target_size` 正数校验 + 放大回退原始分辨率并告警；`TOPOS_READAHEAD`/`TOPOS_DECODE_WORKERS` 非法值告警后走安全默认；帧号越界统一 ValueError；`is_topos_movie`/`_iter_atoms`/`_stsd_first_entry_is_tpic` 全程防御性解析（截断/越界/非法 size 终止，绝不抛异常），moov 读取有 64MiB 上限；`_faststart_is_tpic` 先查长度再切片。
- 性能模式（除已列发现）：readahead 预算按池条目×帧字节记账、显式深度钳制、unsafe 逃生门强制 warning；池容量 depth+2 在飞余量；`_entry_free` 的 `sys.getrefcount` 判忙基线经实测为 2（arrays 与 scratch 判定均正确）；`_downscale_axis` 的 uint32 累加无溢出（≤8192×65535）、仅缩小路径起点严格递增、chroma 目标几何不会反向放大；音频 C17 前缀索引把尾部 seek 从线性降为二分；`_read_ahead` 批内 `hold` 列表防止池条目跨帧复用覆盖；`_readahead_depth` 对 4K 大帧诚实关闭而非超预算。
- 类型注解：公开方法/属性均有类型注解与 docstring（Args/Returns），内部容器有注解字符串；无明显缺失。

## src/shared/media/topos_surface_capability.py (行数: 227)

### 发现

- [AUD-35910] [Major] darwin 分支仅凭"平台判断 + PyObjC 导入成功"即返回 `zero_copy=True, evidence="probe"`，违反模块自身"零拷贝判定必须由持有已映射资源的进程内探测背书"的契约
  - 位置: src/shared/media/topos_surface_capability.py:139-156（证据点 :155）
  - 证据:
    ```python
    try:
        from src.shared.media.macos_vt.topos_surface import (
            MACOS_TOPOS_SURFACE_AVAILABLE,
        )
    except Exception:
        MACOS_TOPOS_SURFACE_AVAILABLE = False
    if MACOS_TOPOS_SURFACE_AVAILABLE:
        return ToposSurfaceCapability(
            backend=ToposSurfaceBackend.MACOS_IOSURFACE,
            available=True, zero_copy=True, ...,
            evidence="probe",
        )
    ```
    而 `src/shared/media/macos_vt/topos_surface.py:22-30` 显示该常量只是 `platform.system() == "Darwin"` + `import IOSurface` 成功，并未创建/映射任何 IOSurface。模块 docstring（:7-11）承诺：`A ``zero_copy=True`` verdict is only ever backed by an in-process probe that owns a mapped resource (``evidence="probe"``).`
  - 影响: 播放/导出策略层据此选择 IOSurface 零拷贝后端并标记 `copy_bytes=False`；若运行环境导入成功但实际 `IOSurfaceCreate` 失败（沙盒/资源受限），路径选择与性能遥测（零拷贝计数）双双失真，只能在逐帧运行时降级兜底（read_frame_metal 的 None 回退 + 0.5s 等待），且 diagnostics 把"未经验证的断言"呈现为"probe 证实"。Windows/Linux 分支严格遵守了该契约（env 标志只标注 unproven），darwin 分支是唯一例外。
  - 建议: 复用一个真实探测（创建+映射一个小 IOSurface 后释放，参考 macos_vt 下已有的 `vt_zero_copy_probe.py` 模式），`evidence="probe"` 仅在探测成功时返回；导入成功但探测失败走 CPU 回退并在 reason 标注。
  - 置信度: 高（契约与实现的事实性矛盾可直接从代码证实；实际运行时影响为中低——IOSurface 在常规 macOS 环境可用）

- [AUD-35911] [Suggestion] `TOPOS_SURFACE_BACKEND` 未知取值（拼写错误等）被静默当作 "auto" 落入平台分支，无任何告警
  - 位置: src/shared/media/topos_surface_capability.py:124-137
  - 证据:
    ```python
    name = (requested or values.get("TOPOS_SURFACE_BACKEND", "auto")).strip().lower()
    if name in {"cpu", "cpu_upload", ToposSurfaceBackend.CPU.value}:
        return _cpu_capability("forced_cpu_upload")
    if name in {"pbo", "opengl_pbo", ToposSurfaceBackend.OPENGL_PBO.value}:
        ...
    # 之后直接进入 darwin/windows/linux 平台分支，未知 name 无告警
    ```
  - 影响: 运维设置了 `TOPOS_SURFACE_BACKEND=cpu_upload_typo` 之类值时，系统按 auto 选择后端而用户以为强制生效——显式诊断覆盖被静默忽略，与该模块"truthful"的定位不符。
  - 建议: name 非空且不属已知别名/auto 时记 warning 并在返回的 reason 中带上被忽略的原值。
  - 置信度: 高

### 通过项
- Windows/Linux 分支正确实现了"环境变量不能自证零拷贝"的契约：env 标志只触发 staging 回退并把 `unproven:<probe_reason>` 写入 reason，mapped 后端仍以真实探测为准（P0-06 语义）。
- `zero_copy_proven` 属性与 evidence 语义一致；`as_dict` 完整暴露判定字段供诊断。
- `_probe_windows_d3d11_shared` 仅在探测同时给出 `available + interop_method` 时才判真（裸 available 不算证明）；`_probe_linux_dmabuf` 在无实现时诚实返回 False。
- `_env_truthy` 处理空白与大小写；`requested` 参数优先于环境变量，两者均可注入（env/system 参数）便于测试。
- 无 GPU 资源持有（纯判定模块，无生命周期问题）；无 Qt 信号（不适用）；无 undo/持久化状态（不适用）。
- 类型注解完整（Enum/dataclass/Mapping），`frozen=True` 不可变；`except Exception` 仅用于能力探测导入，语义为"探测不可用"且写入 reason，符合防御性探测惯例。

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-35900 |
| Major | 1 | AUD-35910 |
| Suggestion | 10 | AUD-35901–35909, AUD-35911 |

优先处置：AUD-35900（alpha 流播放/导出的 use-after-free，建议立即修复并补回归测试）＞ AUD-35910（能力判定证据语义）。

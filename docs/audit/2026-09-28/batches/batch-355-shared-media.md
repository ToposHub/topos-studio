# 批次报告：batch-355（src/shared/media/）

- 编号区间：AUD-35500–AUD-35599
- 审查文件：pixel_format.py (930)、probe.py (219)、proxy.py (1233)
- 汇总：Critical 0 / Major 4 / Suggestion 6

---

## src/shared/media/pixel_format.py (行数: 930)

### 发现

- [AUD-35500] [Major] Subsampling.NONE 与 YUV_444 值相同，成为枚举别名，4:4:4 语义与 RGB/GRAY 混淆
  - 位置: src/shared/media/pixel_format.py:67-72
  - 证据:
    ```python
    YUV_444 = (4, 4, 4)  # 4:4:4 无子采样
    YUV_422 = (4, 2, 2)  # 4:2:2 水平子采样
    YUV_420 = (4, 2, 0)  # 4:2:0 水平+垂直子采样
    YUV_411 = (4, 1, 1)  # 4:1:1 水平 1/4 子采样
    YUV_410 = (4, 1, 0)  # 4:1:0 水平 1/4 + 垂直 1/2 子采样
    NONE = (4, 4, 4)  # 无子采样（RGB/GRAY）
    ```
  - 影响: Python Enum 对重复值会生成别名（已实证 `Subsampling.NONE is Subsampling.YUV_444` 为 True，`list(Subsampling)` 中不存在独立 NONE 成员）。后果：所有以 `Subsampling.NONE` 注册的 RGB/GRAY 格式在 `==` 比较下等同于 `YUV_444`；`get_by_properties(subsampling=Subsampling.YUV_444)`（经 `src/shared/media/__init__.py:118` 对外导出）会混入 rgb24/rgba/gray 等全部非 YUV 格式；`from_tuple((4,4,4))` 永远返回 YUV_444 而非 NONE。任何按 subsampling 推断色彩模型的调用方会把 RGB 误判为 YUV 4:4:4。
  - 建议: 为 NONE 使用不冲突的值（如 `(0, 0, 0)`）或直接复用 YUV_444 并删除 NONE 别名、在比较语义上显式区分；同时为 `get_by_properties` 增加回归测试。
  - 置信度: 高（枚举别名行为已用 Python 实证；当前仓库内 `get_by_properties` 无活跃调用方，现存消费方主要走 `get()`+`has_alpha`，故实际影响为潜在而非现行故障）

- [AUD-35501] [Suggestion] 注册表缺口：yuva444p10le 等带 Alpha 的专业格式未注册，消费方被迫用名称启发式兜底
  - 位置: src/shared/media/pixel_format.py:680-698
  - 证据（注册表侧仅注册无 Alpha 变体）:
    ```python
    cls._register(PixelFormatInfo(
        name='yuv444p10',  # ProRes 4444 使用
        planes=3,
        subsampling=Subsampling.YUV_444,
        bit_depth=10,
        has_alpha=False,
    ```
    而消费方 src/features/edit/core/timeline_occlusion.py:203-205 注释明确写道："覆盖注册表缺口：ProRes 4444 的 ``yuva444p10le``、Topos 4444 的 ``topos_yuva*p*a*`` 等带 Alpha 格式未注册，但名称可安全识别。"
  - 影响: 注册表作为权威数据源的定位被削弱，下游各自维护名称启发式，未来格式覆盖易漂移。
  - 建议: 补注册 yuva444p10le/yuva422p10le 等常用带 Alpha 格式，使 timeline_occlusion 的启发式仅作兜底。
  - 置信度: 高（缺口有消费方注释直接佐证）

- [AUD-35502] [Suggestion] 多处注释/文档与实现不一致（YUV_410 垂直子采样、chroma_bits 算术注释、ProRes 4444 Alpha 表述）
  - 位置: src/shared/media/pixel_format.py:64 / :265 / :677,690-698
  - 证据:
    - :64 文档称 "YUV_410: 水平 1/4，垂直 1/4"，但 :71 行内注释为 "垂直 1/2"，且 `chroma_height_divisor`（:94-95）对 b==0 返回 2（即垂直 1/2）；
    - :265 注释 "4 + 1 + 1 = 6 bits/2 pixels = 12 bits/pixel" 算式不通（实际 12 = 1.5×8 bits/pixel）；
    - :677 注释 "ProRes 4444: yuv444p10 (10-bit 4:4:4 with alpha)" 但 :690-698 注册的 `yuv444p10` 为 `has_alpha=False`（ProRes 4444 实际解码为 yuva444p10le，见 AUD-35501）。
  - 影响: 误导依赖文档的维护者与调用方，尤其 Alpha 有无直接被遮挡判定逻辑消费。
  - 建议: 修正三处注释，使文档与代码一致。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：纯数据注册表，无 OpenGL/ModernGL 资源
- GLSL·HDR 正确性：无着色器代码；`bits_per_pixel` 对 444/422/420 的 YUV、RGB、GRAY 计算核验正确（444→3×、422→2×、420→1.5× 位深）
- 信号防递归：无 Qt 信号/控件
- 状态提交一致性：无 undo/redo/持久化路径
- 异常与取消路径：`from_tuple` 未识别时显式 raise ValueError，无宽泛 except
- 输入验证：`get`/`get_chroma_divisors` 对未知格式返回 None/(1,1)，无崩溃路径；`_register` 同名覆盖为幂等
- 性能模式：懒初始化 + 小表线性查询，开销可忽略；`_ensure_initialized` 理论上多线程可重复执行但注册数据确定且幂等（CPython dict 赋值原子），无实际风险
- 类型注解：完整（含 ClassVar 与 property 返回类型）

---

## src/shared/media/probe.py (行数: 219)

### 发现

- [AUD-35503] [Suggestion] check_codec_supported 别名表把解码器名（libdav1d）当作编码支持依据
  - 位置: src/shared/media/probe.py:191-199
  - 证据:
    ```python
    codec_aliases = {
        'h264': ['libx264', 'h264'],
        'hevc': ['libx265', 'hevc', 'h265'],
        'prores': ['prores', 'prores_ks', 'prores_aw'],
        'dnxhr': ['dnxhr', 'dnxhd'],
        'dnxhd': ['dnxhr', 'dnxhd'],
        'vp9': ['libvpx-vp9', 'vp9'],
        'av1': ['libaom-av1', 'av1', 'libdav1d'],
    }
    ```
  - 影响: `get_supported_codecs` 返回的是 av.codecs_available（编解码器全集，含仅解码的条目）。'libdav1d' 是 AV1 解码器：仅装了解码器的环境会被 `check_codec_supported('av1')` 判定为"可编码"，导出能力探测出现假阳性。当前仓库内该函数无活跃调用方（仅 `src/shared/media/__init__.py:162-167` 转出口），属导出 API 的潜在缺陷。
  - 建议: 区分解码/编码别名，或基于 PyAV codec 的 encode 能力位校验。
  - 置信度: 中（解码器名用于编码判断的矛盾明确，但函数语义意图无调用方佐证）

### 通过项
- 线程安全：模块级缓存仅单次赋值，GIL 下无竞态
- 异常与取消路径：`except Exception` 均带 logger.warning 且符合模块头部"不抛出异常、缺依赖返回明确状态"的设计约定（环境探测语义，非掩盖领域错误）
- 输入验证：codec 名统一 lower() 后比较；版本元组解析有 isinstance 防御
- 性能模式：三重缓存 + 延迟导入，`clear_cache` 提供重检测出口；check_ffmpeg_available 有真实调用方（src/features/edit/core/proxy_generator.py:671）
- 类型注解：完整（Optional 显式标注）
- 注：`get_ffmpeg_version` 检测失败返回 None 且不缓存失败态，会导致每次调用重复探测；因 import 有 sys.modules 缓存、开销小，未单列发现

---

## src/shared/media/proxy.py (行数: 1233)

### 发现

- [AUD-35504] [Major] generate_proxy_key 未纳入 topos_bit_depth/topos_keep_444/topos_qp，Topos 代理参数变更后旧缓存不失效
  - 位置: src/shared/media/proxy.py:715-729
  - 证据:
    ```python
    key_parts = [
        source_path,
        str(source_mtime),
        str(source_size),
        proxy_config.format.name,
        proxy_config.resolution.name,
        str(proxy_config.prores_profile),
        str(proxy_config.h264_crf),
        proxy_config.image_format,
        str(proxy_config.target_height),
        color_space,
        f"{fps:.2f}",
    ]
    key_string = '|'.join(key_parts)
    return hashlib.md5(key_string.encode('utf-8')).hexdigest()
    ```
  - 影响: 这三个参数在生成侧真实生效（src/shared/media/proxy_generator.py:620-624 用 `topos_bit_depth`/`topos_keep_444`/`topos_qp` 决定位深、4:4:4 保留与 QP），但变更后 key 不变 → 同名代理文件被判定有效，新质量配置静默不生效。docstring（:695-696）声称"配置变化时，代理自动失效"，消费方 media_pool_panel.py:9117 也注释"配置参与代理 key，变化后按需重建"——该假设对 topos_* 三参不成立。另注（越界文件，供后续批次参考）：media_pool_panel.py:9129-9136 重建 DiskProxyConfig 时未透传 topos_*，会把服务级 topos 参数重置为默认值。
  - 建议: 将三个 topos_* 参数（以及未来新增的编码参数）加入 key_parts，并为"config 字段 ⊆ key 字段"加注释或测试约束。
  - 置信度: 高

- [AUD-35505] [Major] 最小分辨率钳制按轴独立作用，破坏宽高比并会上采样小尺寸源（内存代理与磁盘代理两处同模式）
  - 位置: src/shared/media/proxy.py:186-194（HALF/QUARTER）、230-232（AUTO）、503-505（ProxyResolution.calculate_size）
  - 证据:
    ```python
    # HALF 策略：50% 分辨率
    if strategy == ProxyStrategy.HALF:
        proxy_w = max(min_w, orig_w // 2)
        proxy_h = max(min_h, orig_h // 2)
        return (proxy_w, proxy_h)
    ```
    同模式（:503-505）：
    ```python
    proxy_w = max(320, int(w * scale) // 2 * 2)  # 保证偶数
    proxy_h = max(180, int(h * scale) // 2 * 2)
    ```
  - 影响: docstring（:151）承诺"保持原始宽高比，不低于最小分辨率"，但 min 钳制对两轴独立生效：如 (1000,200) 走 QUARTER 得 (250,50)，被钳为 (320,180)，5:1 宽高比被拉成 16:9 且被放大；`ProxyResolution.calculate_size` 的硬编码 320/180 下限同样把小于该尺寸的源放大（如 FULL 档的 240x240 源 → 320x180，既上采样又变形）。内存代理路径被 src/shared/video/frame_provider_service.py:2009-2019 直接消费，导致预览解码几何变形与无谓的上采样开销。
  - 建议: 钳制时保持宽高比（按长边等比缩放至不小于最小尺寸），且结果不超过原始分辨率。
  - 置信度: 高

- [AUD-35506] [Major] calculate_target_size 奇数高度未取偶，H264/JPEG 代理编码失败风险
  - 位置: src/shared/media/proxy.py:590-593
  - 证据:
    ```python
    target_height = min(max(2, int(height)), self.target_height)
    scale = target_height / max(1, int(height))
    target_width = max(2, int(round(int(width) * scale)) // 2 * 2)
    return target_width, target_height
    ```
  - 影响: 宽度做了 `//2*2` 取偶，高度直接取源高度（构造器只保证 `self.target_height` 为偶数；源高度为奇数且小于目标高度时，如 1079 高源 + target 1080，target_height=1079 为奇数）。该尺寸经 proxy_generator.py:1210 进入 `get_ffmpeg_args` 的 `scale=w:h:force_original_aspect_ratio=decrease`（force_original_aspect_ratio 只缩不取偶），H264 格式走 yuv420p、jpg 序列走 yuvj420p，两维均须为偶数 → ffmpeg 报 "height not divisible by 2"，代理生成失败（默认 ProRes yuv422p10le 仅水平子采样，不受影响）。
  - 建议: `target_height` 与宽度同样做 `//2*2` 取偶。
  - 置信度: 高（逻辑缺陷确定；触发需奇数高度源 + H264/IMAGE_SEQUENCE 格式）

- [AUD-35507] [Suggestion] 文件中部重复导入（enum/typing 顶部已导入，中部再导入一次）
  - 位置: src/shared/media/proxy.py:413-416
  - 证据:
    ```python
    from enum import Enum, auto
    from typing import Optional
    import hashlib
    import os
    ```
  - 影响: `Enum`/`Optional` 与顶部 :45-49 的导入重复，违反 PEP 8 顶部导入约定，增加阅读成本。
  - 建议: 仅保留 `auto`/`hashlib`/`os` 的增量导入并合并到文件顶部。
  - 置信度: 高

- [AUD-35508] [Suggestion] ProxyConfig.scale_factor 对 CUSTOM/AUTO 恒返回 1.0 的占位实现
  - 位置: src/shared/media/proxy.py:116-120
  - 证据:
    ```python
    elif self.strategy == ProxyStrategy.CUSTOM:
        # CUSTOM 策略的缩放因子在 calculate_proxy_size 中计算
        return 1.0  # 占位，实际计算需要原始分辨率
    else:
        return 1.0  # AUTO 策略需要动态计算
    ```
  - 影响: 调用方若依赖该属性推导解码尺寸，CUSTOM/AUTO 配置会得到 1.0（不降级）的错误结论。当前仓库未发现活跃调用方，属潜在陷阱。
  - 建议: 删除该属性，或改为接收 original_size 的方法 / 直接抛 NotImplementedError 以阻止误用。
  - 置信度: 高（占位行为明确；无调用方故无现行影响）

- [AUD-35509] [Suggestion] DiskProxyConfig 可变、参数名 format 遮蔽内置函数，与模块内 frozen dataclass 风格不一致
  - 位置: src/shared/media/proxy.py:538-540
  - 证据:
    ```python
    def __init__(
        self,
        format: ProxyFormat = ProxyFormat.PRORES_PROXY,
        resolution: ProxyResolution = ProxyResolution.HALF,
    ```
  - 影响: `format` 遮蔽内置；非冻结实例可被中途修改，导致缓存 key（生成时快照）与实际编码参数脱钩，与 AUD-35504 叠加放大失配风险。
  - 建议: 参数改名 proxy_format，并考虑改为 frozen dataclass 统一不可变约定。
  - 置信度: 高

### 通过项
- 线程安全：全部为纯函数 + frozen dataclass（DiskProxyConfig 除外，见 AUD-35509），无共享可变状态，无锁需求
- GPU 资源生命周期：不涉及 GPU/OpenGL 资源
- GLSL·HDR 正确性：无 GLSL 代码；无 HDR 数据 clamp(0,1) 类操作（get_ffmpeg_args 的 pix_fmt 转换为磁盘代理预览口径，HDR 收敛在 proxy_generator 侧有显式日志）
- 信号防递归：无 Qt 控件/信号
- 状态提交一致性：不直接操作 undo/redo；ProxyStatus 生命周期（MISSING/GENERATING/READY/STALE/FAILED）转换注释完整，with_* 系列返回新实例不可变
- 异常与取消路径：validate_disk_proxy 显式捕获 (FileNotFoundError, OSError) 并降级 MISSING（非宽泛 except）；get_source_file_info 异常契约在 docstring 声明；get_disk_proxy_rejection_reason 的 _positive_int 仅捕获 (TypeError, ValueError) 用于外部元数据容错
- 输入验证：ProxyConfig.__post_init__ 校验 CUSTOM 必须带 max_resolution 且分辨率为正；calculate_scale_ratio 有 orig_w>0 除零防护；get_disk_proxy_rejection_reason 对外部元数据全部走 getattr 防御
- 性能模式：尺寸计算均为 O(1) 纯计算，无热路径开销
- 类型注解：完整（公开函数均有参数与返回类型标注）

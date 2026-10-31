# batch-349 审查报告

批次：batch-349 [P2] 主题 src/shared/media//
编号区间：AUD-34900–AUD-34999
审查文件：`src/shared/media/hwaccel.py`（1,009 行，全文通读）

交叉验证参考（只读）：`src/shared/media/ffmpeg_source.py`（父类 FFmpegMediaSource 的
open/close/read_next_frame/seek_frame/_seek_gop/_seek_gop_session/_decode_to_target/
_resync_main_session_if_needed/_convert_frame*/_transfer_hw_frame_if_needed）、
`src/shared/media/factory.py`（_try_create_hwaccel_source 的实际调用方式）、
`src/shared/video/playback_clock.py`（set_preserve_planes 调用点）。

---

## src/shared/media/hwaccel.py (行数: 1009)

### 发现

- [AUD-34900] [Critical] 硬解顺序读路径忽略 seek 落地帧（_pending_av_frame）与 seek 会话/主会话回同步，seek 后返回错误帧
  - 位置: src/shared/media/hwaccel.py:866-878
  - 证据:
    ```python
    # 硬件解码
    try:
        # 创建帧迭代器（如果不存在）
        if self._frame_iterator is None:
            self._frame_iterator = self._container.decode(self._video_stream)

        # 解码下一帧
        for frame in self._frame_iterator:
            decoded = self._convert_hwaccel_frame(frame)
            self._current_frame += 1
            return decoded
    ```
    父类契约（ffmpeg_source.py:1246-1255）是先消费 `_pending_av_frame`，再走
    `_pull_next_frame_from_seek_session()`，最后才建迭代器（且建前调
    `_resync_main_session_if_needed()`，ffmpeg_source.py:1269-1272）。父类 seek 的
    落地语义（ffmpeg_source.py:1739-1742 `_decode_to_target`、1653-1658
    `_seek_gop_session`）都把目标帧缓存在 `_pending_av_frame`，等下一次读来取。
  - 影响: `_using_hwaccel=True` 时调用继承的 `read_frame(N)`/`seek_frame(N)` 后：
    (1) 普通 GOP/Intra seek 返回的是第 N+1 帧（off-by-one），缓存的第 N 帧被跳过且
    永久滞留；(2) HEVC 类走 SLICE seek 会话后，主（hwaccel）容器的 demux 位置从未
    被 seek/回同步，覆盖方法直接在其陈旧位置上新建迭代器，返回完全错位的帧，
    `_main_resync_pts` 悬挂；帧号 `_current_frame` 与实际输出帧脱钩。错误帧直接进
    渲染（渲染错误）。注意 `read_frame_planar`/`_read_next_frame_planar` 未被覆盖、
    走父类正确路径，故缺陷集中在 packed RGB 合约的 `read_frame`/`read_next_frame`
    消费者（Windows NVIDIA 默认硬解路由即打包 RGB 路径，factory.py:990-998 表）。
  - 建议: 覆盖方法的硬解分支开头补齐父类三段式契约：先 `if self._pending_av_frame
    is not None` 消费落地帧（经 `_convert_hwaccel_frame`），再尝试
    `_pull_next_frame_from_seek_session()`，建主迭代器前调用
    `_resync_main_session_if_needed()`；或直接复用父类 `read_next_frame`，仅把
    转换函数参数化。
  - 置信度: 高（基于父子类代码逻辑交叉验证；未做运行时复现）

- [AUD-34901] [Major] hwaccel 重开失败后 CPU 回退失效：旧容器已关闭、_is_open 仍为 True，后续每次读取必抛 RuntimeError
  - 位置: src/shared/media/hwaccel.py:779-792（关闭旧容器后重开）、719-730（异常吞掉并宣称回退 CPU）
  - 证据:
    ```python
    if self._container is not None:
        try:
            self._container.close()
        except Exception:
            pass
        self._container = None
        self._video_stream = None

    # Reopen with hwaccel
    self._container = av.open(self._path, hwaccel=accel)
    ```
    失败时上层仅做：
    ```python
    except Exception as e:
        logger.warning(
            f"Failed to configure hwaccel {decoder.value}: {e}, "
            f"falling back to CPU"
        )
        if self._hwaccel_config.fallback_to_cpu:
            self._using_hwaccel = False
    ```
    而父类 CPU 读路径前置检查（ffmpeg_source.py:1241-1242）：
    `if self._container is None or self._video_stream is None: raise RuntimeError(...)`
  - 影响: `av.open(..., hwaccel=accel)` 失败（如 ffmpeg 未编译对应 hwaccel、驱动/
    设备初始化失败、文件在重开瞬间被移动）时，`open()` 表面成功、`_is_open=True`，
    但容器为 None；此后所有 `read_next_frame()` 抛出裸 RuntimeError（连
    MediaDecodeError 都不是），本可 CPU 解码的媒体整体不可用。`fallback_to_cpu=False`
    分支抛 MediaOpenError 时同样留下半开状态。factory 的 try/except 只包创建，
    open() 在调用方进行，兜不住此路径。
  - 建议: `_configure_hwaccel` 采用"先开新容器、成功后再关旧容器"的换序；或失败
    分支里显式重新 `av.open(self._path)`（CPU 模式）恢复父类状态；至少要在失败时
    调 `self.close()` 并让 open() 抛 MediaOpenError，不得带伤返回成功。
  - 置信度: 高

- [AUD-34902] [Major] 解码中途回退 CPU 是假回退：仍在 hwaccel 容器上解码，且丢弃迭代器导致丢帧/帧号漂移，残留 _pending_av_frame 会吐出陈旧帧
  - 位置: src/shared/media/hwaccel.py:884-891
  - 证据:
    ```python
    # 尝试回退到 CPU
    if self._hwaccel_config.fallback_to_cpu and self._using_hwaccel:
        logger.warning("Falling back to CPU decode")
        self._using_hwaccel = False
        # 重新初始化迭代器
        self._frame_iterator = None
        return super().read_next_frame()
    ```
  - 影响: (1) `self._container` 仍是 `av.open(path, hwaccel=accel)` 打开的容器，
    `super().read_next_frame()` 只是把 `_using_hwaccel` 标志清掉，解码仍走硬件路径，
    同一错误大概率复现；(2) 重建 decode 迭代器丢弃解码器内已重排/未吐出的帧，
    实际帧序列出现跳帧或重复，`_current_frame` 与真实流位置脱钩（DecodedFrame
    的 frame_index/时间轴随之错）；(3) 若此前在硬解模式下发生过 seek，
    `_pending_av_frame` 从未被硬解路径消费（见 AUD-34900），切回父类路径后第一个
    读会把这帧陈旧帧当新帧返回。
  - 建议: 中途回退应关闭 hwaccel 容器并按 CPU 参数重新 `av.open` + seek 回
    当前帧号，再续读；至少在回退时清空 `_pending_av_frame` 并文档化帧号误差。
  - 置信度: 高（逻辑层面）；运行时表现（丢帧幅度）中
  - 注: 合并陈述同段的帧号一致性问题，不另立编号。

- [AUD-34903] [Major] `_check_vaapi` 对 `os.listdir('/dev/dri')` 无 FileNotFoundError/PermissionError 防护，无 DRM 设备的 Linux 环境上探测直接抛异常
  - 位置: src/shared/media/hwaccel.py:483-491
  - 证据:
    ```python
    # 检查 /dev/dri/renderD* 设备
    import os
    dri_devices = [
        f for f in os.listdir('/dev/dri')
        if f.startswith('renderD')
    ]
    ```
  - 影响: WSL1、无 GPU 直通的虚拟机、容器等常见 Linux 环境没有 `/dev/dri`，
    `os.listdir` 抛 FileNotFoundError 并沿 `detect_available_decoders()` 一路上抛。
    factory 的 `_try_create_hwaccel_source` 有兜底 try（退化为 CPU + 告警日志），但
    `MediaSourceFactory.get_hwaccel_status_summary()`（factory.py:1234-1251）、
    `should_use_hwaccel_for_codec()`（factory.py:1253-1272）以及直接构造
    `HardwareDecodeConfig`（`__post_init__` 即探测，hwaccel.py:582-591）均无包裹，
    形成崩溃路径；工厂路径也会对每个文件重复抛异常降级。
  - 建议: 用 `os.path.isdir('/dev/dri')` 预检或 try/except FileNotFoundError/
    PermissionError 包裹 listdir，返回空列表继续走 ffmpeg 输出检查。
  - 置信度: 高

- [AUD-34904] [Major] Linux QSV 探测回退到 ffmpeg 构建项检查（'qsv' in -hwaccels 输出），非 Intel 平台误报 QSV 可用——与 W2-02 已修复的 NVDEC 误判同类
  - 位置: src/shared/media/hwaccel.py:533-546（Linux 分支落空后落到 ffmpeg 检查）、529-531（Windows 分支宽泛 except）
  - 证据:
    ```python
    # Linux: 检查 Intel GPU（通过 VAAPI）
    elif system == 'Linux':
        if HardwareCapability._check_vaapi():
            # VAAPI 在 Intel GPU 上就是 QSV 的 Linux 实现
            HardwareCapability._qsv_available = True
            return True

    # 通过 FFmpeg 检查
    if 'qsv' in _ffmpeg_hwaccels_output():
        HardwareCapability._qsv_available = True
        return True
    ```
    （对照 `_check_nvdec` 中 W2-02 的注释："「编译进 FFmpeg」不等于「本机有可用硬件」"，
    hwaccel.py:430-432。）
  - 影响: 无 Intel GPU 的 Linux 机器上，主流 ffmpeg 发行版构建几乎都编入 qsv，
    于是 `detect_available_decoders()` 报告 QSV 可用；当 VAAPI/NVDEC 均不可用时
    `get_platform_default()` 选中 QSV → `FFmpegHwaccelSource` 以 qsv 重开失败 →
    触发 AUD-34901 的坏状态链（本应回退 CPU）。另：Windows 分支
    `except Exception: _qsv_available = False` 把 `probe_gpu_capability` 的瞬时/
    导入错误永久缓存为不可用，`clear_cache()` 前无法恢复。
  - 建议: 删除 Linux 上的 ffmpeg 构建项回退（保持与 W2-02 相同语义：只认真实
    设备）；Windows 分支收窄异常类型或至少不缓存瞬时错误。
  - 置信度: 高（逻辑），影响链依赖 AUD-34901

- [AUD-34905] [Suggestion] `_check_videotoolbox` 恒真：PyAV 备用检查是死代码，且 macOS 上无条件 `return True`，探测失去判别力
  - 位置: src/shared/media/hwaccel.py:369-386
  - 证据:
    ```python
        # 备用检查：通过 PyAV
        try:
            import av
            ...
                except Exception:
                    pass
        except ImportError:
            pass

        # macOS 默认假设可用
        return True
    ```
  - 影响: 在 macOS 上无论 ffmpeg 是否编译 videotoolbox，函数恒返回 True；
    `'videotoolbox' in _ffmpeg_hwaccels_output()` 的负结果被末行无条件覆盖。
    使用不带 videotoolbox 的 ffmpeg 构建（自编译/部分发行渠道）时会被路由到 VT
    硬解并在重开时失败（接 AUD-34901）。中间的 PyAV 检查无论结果如何最终都
    `return True`，属死逻辑；`except Exception: pass` 也违反"不得宽泛 except"约束。
  - 建议: 删除死代码，直接以 `_ffmpeg_hwaccels_output()` 结果为准；如确需
    "macOS 默认可用"策略，写入显式注释并确认 AUD-34901 修复后失败可回退。
  - 置信度: 高（逻辑恒真可证）；真实世界触发频率中

- [AUD-34906] [Suggestion] 覆盖的 `read_next_frame` 缺少父类的 P2-1 播放期同步解码断言
  - 位置: src/shared/media/hwaccel.py:841-863（对照 ffmpeg_source.py:1227-1236）
  - 证据: 父类每次同步读前执行：
    ```python
    from src.shared.video.video_playback import _assert_no_sync_decode_in_playback
    _assert_no_sync_decode_in_playback(
        operation="FFmpegMediaSource.read_next_frame", ...)
    ```
    覆盖方法的硬解分支（855-878）无此检查，仅在 CPU 分支经 `super()` 间接获得。
  - 影响: 播放期间违规经 `FFmpegHwaccelSource.read_next_frame` 直接同步解码不会
    被断言捕获，P2-1 防护在硬解路径出现缺口。
  - 建议: 在覆盖方法开头复用同一断言（或抽到公共入口）。
  - 置信度: 高

- [AUD-34907] [Suggestion] `_convert_hwaccel_frame` 的 planar 判定绕过 `_is_planar_mode`，缺 `_is_yuv_pixel_format` 门控，与父类路由不一致
  - 位置: src/shared/media/hwaccel.py:920-922（对照 ffmpeg_source.py:1076-1079）
  - 证据:
    ```python
    if self._preserve_planes or self._output_mode == "planar_yuv":
        return super()._convert_frame_planar(frame)
    return super()._convert_frame(frame)
    ```
    父类 `_is_planar_mode` 额外要求 `_is_yuv_pixel_format(metadata.pixel_format)`。
  - 影响: `playback_clock.py:345` 等会对 master 源调 `set_preserve_planes(True)`；
    若硬解源的源像素格式是 RGB（如 gbrp 系 HEVC/VP9），覆盖方法会把 RGB 帧送进
    `_convert_frame_planar`，父类同场景会走 packed 转换——两路径输出合约分叉，
    planar 消费者拿到非预期平面布局。YUV 源不受影响。
  - 建议: 改用 `self._is_planar_mode` 作为唯一判据，与父类保持一致。
  - 置信度: 中（触发需 RGB 源 + preserve_planes 组合，较罕见）

- [AUD-34908] [Suggestion] `clear_cache()` 不清除 `_cached_hwaccels_output`，"重新探测"语义不完整
  - 位置: src/shared/media/hwaccel.py:548-558（对照 47-71 的模块级缓存）
  - 证据:
    ```python
    HardwareCapability._detected_decoders = None
    HardwareCapability._nvdec_available = None
    HardwareCapability._vaapi_available = None
    HardwareCapability._qsv_available = None
    ```
  - 影响: 清缓存重探后，`_ffmpeg_hwaccels_output()` 仍返回旧结果，用户更换/修复
    ffmpeg 后（驱动安装、PATH 变更）探测结果无法刷新。
  - 建议: `clear_cache()` 中同步 `_cached_hwaccels_output = None`（需移到模块内
    可达位置或提供模块级 reset）。
  - 置信度: 高

- [AUD-34909] [Suggestion] 类级探测缓存无锁，且 `HardwareDecodeConfig.__post_init__` 在构造期触发可能耗时数秒的子进程探测
  - 位置: src/shared/media/hwaccel.py:152-155、582-591
  - 证据:
    ```python
    # 缓存探测结果
    _detected_decoders: Optional[list[HardwareDecoder]] = None
    ```
    ```python
    def __post_init__(self) -> None:
        """验证配置"""
        if self.decoder is not None:
            # 检查解码器是否可用
            available = HardwareCapability.detect_available_decoders()
    ```
  - 影响: 多线程（渲染/解码 worker 与 UI）首次并发探测会重复执行
    `nvidia-smi`、`ffmpeg -hwaccels`（各 5s timeout）；在 UI 线程构造
    `HardwareDecodeConfig` 最坏可阻塞约 10s。竞态本身良性（幂等写）。
  - 建议: 探测加 `threading.Lock` 双检；`__post_init__` 里仅记录告警用的可用性
    检查改为惰性（首次真正选解码器时）。
  - 置信度: 高（结构），性能幅度中

- [AUD-34910] [Suggestion] 未使用导入与死状态：GOP_CODECS/INTRA_CODECS/check_ffmpeg_available/field 未被引用；`_check_vaapi` 内冗余 `import os`；`_hwaccel_context` 只写不读
  - 位置: src/shared/media/hwaccel.py:33、39、43、484、661/805/821/830
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from .ffmpeg_source import FFmpegMediaSource, GOP_CODECS, INTRA_CODECS
    from .probe import check_ffmpeg_available
    ```
    `grep` 全文件：`field`、`GOP_CODECS`、`INTRA_CODECS`、`check_ffmpeg_available`
    仅出现在导入行；`_hwaccel_context` 全部 6 处出现均为赋值/置 None，无读取。
  - 影响: 死代码与冗余导入增加阅读噪音；`_hwaccel_context` 暗示存在未实现的
    context 管理，误导维护者。
  - 建议: 删除未用导入、函数内重复 `import os`；移除 `_hwaccel_context` 或落实
    其用途（如 close 时显式释放）。
  - 置信度: 高

### 通过项
- 线程安全：单实例解码状态（_frame_iterator/_container 等）按单解码线程使用，未发现跨线程共享实例状态的问题（类级缓存竞态见 AUD-34909，属良性）
- GPU 资源生命周期：close() 先清 hwaccel 引用再 super().close()，顺序正确；hw 帧下载经父类 _transfer_hw_frame_if_needed（ffmpeg_source.py:1964-2003），planar/packed 两条转换链均处理硬件表面帧
- GLSL/HDR 正确性：本文件不涉及 shader 与 HDR 像素数据路径（无 clamp(0.0,1.0) 类操作）
- 信号防递归：不涉及（无 Qt 控件/信号）
- 状态提交一致性（undo/redo/revision/持久化）：不涉及（无文档状态；GOP 索引持久化在父类，本文件未触碰）
- 异常与取消路径：`_ffmpeg_hwaccels_output`、`_check_nvdec`(Linux)、`has_nvidia_gpu` 的 subprocess 均有 timeout 与 (TimeoutExpired/FileNotFoundError/OSError) 定向捕获（例外见 AUD-34903/34904/34905）
- 输入验证：codec 名称统一 lower + 别名归一（check_codec_support、is_hwaccel_recommended）；HardwareDecodeConfig 字段有默认值与文档约束
- 性能模式：探测结果全程缓存（detect_available_decoders/_nvdec/_vaapi/_qsv/ffmpeg 输出），无重复探测热点（首次构造阻塞问题见 AUD-34909）；Windows 硬解默认让位 CPU 的策略在 factory 层实现，本文件的 D3D11VA av1 注释与首帧拒绝机制描述一致
- 类型注解：公共 API（detect/get_*/check_*、HardwareDecodeConfig、FFmpegHwaccelSource 方法、便捷函数）均有参数与返回类型注解
- `get_best_decoder_for_codec` 的平台默认优先级实现与注释一致（platform_default 优先于探测顺序，hwaccel.py:332-344）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-34900 |
| Major | 4 | AUD-34901, AUD-34902, AUD-34903, AUD-34904 |
| Suggestion | 6 | AUD-34905 – AUD-34910 |

备注：AUD-34900/34901/34902/34904 相互耦合——seek 契约缺口、假回退与探测误报共同
构成"硬解选中 → 重开失败/seek 错帧 → 素材不可用或渲染错帧"的链路；建议按
34901 → 34900 → 34902 → 34904/34903 的顺序修复并补 seek 回归测试（现有
tests/media/test_v3_hwaccel_policy.py 多为 mock，未见覆盖真实 seek 后硬解续读的用例）。

# batch-334 [P2] src/shared/export — 审查报告

- 批次：batch-334（编号区间 AUD-33400–AUD-33499）
- 文件：`src/shared/export/topos_encoder.py`（3,537 行，全量通读）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 交叉核实（只读）：`src/shared/codec/topos_binding.py`（TOPOS_MATRIX_CODES 等码表）、`src/features/edit/core/timeline_export.py` / `render_cache_encoder.py` / `topos_cache_codec.py` / `file_io.py`（构造点）

## src/shared/export/topos_encoder.py (行数: 3537)

### 发现

- [AUD-33400] [Major] 序列事务日志写失败被吞掉，两阶段提交安全机制随之静默失效（活暂存被并发清除 / 崩溃后备份连同旧帧一起被删）
  - 位置: src/shared/export/topos_encoder.py:1866（吞错）、1892-1893（无 journal 判死）、1942-1946（state 非 prepared → rmtree）
  - 证据:
    ```python
    # _write_sequence_journal（1849-1871）
    except OSError as e:  # 日志属恢复辅助，失败不阻断主流程
        logger.warning("Topos encoder: 写序列事务日志失败: %s", e)
    ```
    ```python
    # _staging_owner_alive（1892-1893）
    if not journal:
        return False
    ```
    ```python
    # _recover_one_stale_staging（1942-1946）
    if state not in ("prepared", "rollback_incomplete") \
            or not isinstance(output, str) \
            or not os.path.isdir(output):
        shutil.rmtree(stale, ignore_errors=True)
        return
    ```
  - 影响: journal 是 N01/N02 恢复机制的唯一活性凭证。(1) 暂存目录创建时 journal 写失败（瞬时磁盘满/权限等）仅告警不阻断——同一目标的另一实例扫描时按"无 journal=死亡"直接 rmtree 该**活**暂存目录，正在写帧的导出随后失败（N02"绝不触碰其它实例暂存"保证被击穿）。(2) `prepared` 日志写失败后仍继续逐帧 `os.replace` 覆盖用户旧帧，此后进程中断时恢复流程读到无 journal → 连 `.backup` 目录一起 rmtree，已被替换的用户旧帧永久丢失（数据丢失路径）。
  - 建议: journal 是事务锚点，其"创建态"与"prepared 态"写入失败应视为致命（中止本次导出/提交，而非仅告警）；或恢复侧对"无 journal"目录要求额外活性证据（如暂存内常驻 flock 心跳文件：拿不到锁即属主存活）再判死。
  - 置信度: 中（代码路径确定；触发需 journal 写失败这一前置条件与并发导出/进程中断叠加）

- [AUD-33401] [Major] 暂存属主活性判定依赖 PID（复用敏感、跨主机失真），且恢复扫描不持提交锁——网络盘多主机场景可误删活事务
  - 位置: src/shared/export/topos_encoder.py:1902-1908（os.kill 判活）、1837-1840（恢复入口在提交锁之外）
  - 证据:
    ```python
    # _staging_owner_alive（1902-1908）
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:  # EPERM 等：进程存在但属他人
        return True
    ```
    ```python
    # _create_image_sequence_staging（1837-1840）——先恢复，后 mkdtemp；
    # 恢复不在 _sequence_commit_lock 内
    output = self._image_sequence_output()
    parent = os.path.dirname(output) or "."
    base = os.path.basename(output)
    self._recover_stale_image_staging(parent, base)
    ```
  - 影响: 单机同进程/跨进程路径有 pid 判活保护，但：(a) 长时运行进程中 PID 复用——死亡导出的 pid 被无关新进程占用 → 陈旧暂存被永久判活，`prepared` 中断事务不再自动恢复（输出停留在新旧混合版本），目录持续累积；(b) 网络盘多主机导出同一目标时 pid 命中与否与真实属主无关——对端未命中时，writing 态活暂存被 rmtree（该导出中途失败），提交中实例的备份目录被删（其回滚不可逆，可致用户旧帧丢失）。恢复发生在 `_create_image_sequence_staging`，先于 `_sequence_commit_lock`，不与在途提交互斥。
  - 建议: 活性判定改用恢复侧可验证的凭证（journal 记录 pid+进程启动时间戳，或暂存目录内常驻 flock 文件——锁被持有即属主存活，与平台/主机无关），并把恢复流程纳入提交锁临界区。
  - 置信度: 中（机制确定；触发集中于 PID 复用与网络盘多主机并发导出场景）

- [AUD-33402] [Major] 基类以 `container_mode="image_sequence"` 运行时引用未定义属性 `_image_tier`，首帧即 AttributeError
  - 位置: src/shared/export/topos_encoder.py:1763（直接引用）、463-467（基类构造显式接受该模式）、3485（仅子类赋值）
  - 证据:
    ```python
    # 基类 __init__（463-467）显式接受 image_sequence
    self._container_mode = str(container_mode or "mov").strip().lower()
    if self._container_mode not in ("mov", "image_sequence"):
        raise ValueError(
            f"Topos encoder: unsupported container_mode={container_mode!r}"
        )
    ```
    ```python
    # _write_image_packet（1763-1768）——基类 __init__ 从未赋值 _image_tier
    if self._image_tier is not None:
        meta_payload = encode_topos_meta(
            tier_id=int(self._image_tier.tm_tier_id),
            vendor='TOPOS',
            label=self._image_tier.file_label,
        )
    ```
    对照：`_prepare_packed`（3312）用 `getattr(self, '_image_tier', None)` 兜底，证明基类确实没有该属性。
  - 影响: 直接构造 `ToposVideoEncoder(..., container_mode="image_sequence")`（构造器合法接受的参数组合）时，首帧 `_write_image_packet` 抛 AttributeError，被 `_encode_prepared` 包装为 "Topos encode error"——导出必然失败且报错不可读。当前仓库 3 处调用（timeline_export.py:4805/5004、render_cache_encoder.py:69 用默认 mov；其余走子类 `ToposImageSequenceEncoder`），属潜伏崩溃缺陷。
  - 建议: 基类 `__init__` 统一初始化 `self._image_tier = None`（子类已在 super().__init__ 之前覆写），或 `_write_image_packet` 改用与 3312 行相同的 `getattr(self, '_image_tier', None)`。
  - 置信度: 高

- [AUD-33403] [Suggestion] `_pipeline_discard` join 超时后置空共享队列/条件变量，与仍在运行的转换 worker 竞争，后台线程抛 AttributeError 噪声
  - 位置: src/shared/export/topos_encoder.py:1580-1591（置 None）vs 1485-1488（worker 访问）
  - 证据:
    ```python
    # _pipeline_discard（1580-1591）
    for t in self._convert_threads:
        t.join(timeout=2.0)
    self._convert_threads = []
    ...
    self._converted_queue = None
    self._converted_map = None
    self._convert_cond = None
    ```
    ```python
    # _pipeline_convert_worker（1485-1488）——join 超时的 worker 稍后执行
    try:
        entry = (seq, self._prepare_planes(frame), None)
    except BaseException as e:  # noqa: BLE001 转换失败必须传递
        entry = (seq, None, e)
    self._converted_queue.put(entry)   # 此刻 _converted_queue 已是 None
    ```
  - 影响: 慢转换帧超过 join(2s) 时 worker 仍存活，随后对被置 None 的 `_converted_queue` / `_convert_cond` 的访问在后台线程抛 AttributeError/TypeError——abort 路径产生未处理线程异常噪声（在途帧本就丢弃，无数据风险，仅健壮性/日志污染）。
  - 建议: 置空前确保 worker 真正退出（join 无超时，或超时后重试等待），或只清引用不置 None、由哨兵驱动 worker 自行退出后再拆除队列。
  - 置信度: 高

- [AUD-33404] [Suggestion] packed 路径缺少 `data.shape` 与帧宽高/导出配置的一致性校验；尺寸巧合时 reshape 静默错排，失败时单帧畸形输入永久禁用 native 融合转换
  - 位置: src/shared/export/topos_encoder.py:3294-3299（无几何校验）、3013-3014（reshape）、3022-3029（永久降级）
  - 证据:
    ```python
    # _prepare_packed（3294-3299）——只查 ndim/通道数，未查 h/w 与 data 一致
    h = int(getattr(frame, 'height', 0) or data.shape[0])
    w = int(getattr(frame, 'width', 0) or data.shape[1])

    if data.ndim != 3 or data.shape[2] not in (3, 4):
        raise RuntimeError(
    ```
    ```python
    # _try_native_packed（3013-3014）——元素总数巧合相符时静默按错误行宽重排
    planes = self._codec.convert_packed_rgb(
        data.reshape(h, w * nch), w, h,
    ```
    ```python
    # （3028）单次失败即整流降级
    self._cvt_available = False
    ```
  - 影响: 帧属性与 data 实际几何不一致（畸形内部帧）时：元素总数相符 → reshape 静默成功但行宽错排，错误画面进入转换；不相符 → reshape 抛错被 `except Exception` 吞掉，仅因一帧畸形输入就**永久**关闭本流的 native 融合转换（整段退回慢速 numpy 路径，4K 性能回退），几何错配最终以 native 层报错形式浮现，诊断性差。对照 `_prepare_planar` 有完整几何校验（3150-3155），packed 路径是校验缺口。
  - 建议: packed 路径补显式校验 `data.shape[:2] == (h, w)`（并与 `config.width/height` 一致），报错文案指明帧几何与导出配置不符。
  - 置信度: 高（校验缺失确定；实际触发依赖上游产出畸形帧）

- [AUD-33405] [Suggestion] 转换 worker 线程并发读写 `_frames_domain_checked` 与告警标志，无同步（抽样调度不确定）
  - 位置: src/shared/export/topos_encoder.py:2516-2520（计数器）、3221（调用点）、1485/1498（worker 线程执行 `_prepare_planes`）
  - 证据:
    ```python
    # _should_full_domain_check（2516-2520）——读-改-写无锁
    if os.environ.get('TOPOS_ENCODER_FULL_CHECK', '') == '1':
        return True
    due = self._frames_domain_checked % 256 == 0
    self._frames_domain_checked += 1
    return due
    ```
    `_prepare_planar` 在 3221 行调用它；pipelined 模式下 `_prepare_planes` 由多个转换 worker 线程并发执行（1485/1498）。
  - 影响: 多 worker 并发时计数器读-改-写竞争——码值域抽检的帧序偶发偏移/重复；`_opaque_alpha_warned`/`_drop_alpha_warned`/`_float_range_warned` 同样被并发置位，告警可能重复。校验语义不受损（至多漏检/多检一帧），但违反本项目"确定性"原则（同输入不同运行行为可漂移）。
  - 建议: 用 `threading.Lock` 保护该计数器与标志组，或把域检查调度移回主线程（按提交序而非转换完成序判定）。
  - 置信度: 中

- [AUD-33406] [Suggestion] `_seg_discard` 未 `cancel_futures=True`，abort/定稿失败后已排队 GOP 段仍在后台完整编码
  - 位置: src/shared/export/topos_encoder.py:2605-2608
  - 证据:
    ```python
    def _seg_discard(self) -> None:
        """abort/失败路径：丢弃在途段与缓冲（无输出残留）。"""
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
    ```
  - 影响: `shutdown(wait=False)` 不取消已排队 future——用户取消导出后，队列中的段仍逐一在后台线程完整编码（结果被丢弃）：白耗 CPU，且与用户随后立即发起的下一次导出争抢 codec 进程级线程池（SLICE_THREADS_POLICY 生效值受干扰），直至排空。运行中的段无法中断属可接受残余，排队段可以立即丢弃。
  - 建议: 改为 `self._pool.shutdown(wait=False, cancel_futures=True)`（requires-python>=3.10，项目已满足）。
  - 置信度: 高

- [AUD-33407] [Suggestion] packed 转换的 matrix 兜底静默替换为 bt709——当前不可达，但与码表无同步约束，属潜伏的隐性色彩偏移
  - 位置: src/shared/export/topos_encoder.py:2990-2994、3384-3387（两处同型）
  - 证据:
    ```python
    color_metadata = getattr(self._config, 'color_metadata', None)
    matrix_name = str(getattr(color_metadata, 'matrix', 'bt709') or 'bt709').lower()
    if matrix_name not in _YUV_FORWARD:
        matrix_name = 'bt709'
    kr, kb = _YUV_FORWARD[matrix_name]
    ```
    已核实 `TOPOS_MATRIX_CODES`（topos_binding.py:101-108）与 `_YUV_FORWARD`（本文件 129-136）当前六键完全一致，构造期 `_map_color_codes` 已拒绝未知 matrix，故此兜底现不可达。
  - 影响: 两表之间没有同步约束——未来 binding 侧扩充矩阵码（如 smpte240m/ycgco）而 `_YUV_FORWARD` 未同步时，packed 导出将静默用 bt709 权重编码、码流却按声明 matrix 打标签（无声色彩偏移），与文件自身声明的 R-13 哲学（"未知标签显式失败，不许默认值掩盖"）相悖。
  - 建议: 运行期兜底改为显式报错（构造期已校验过，再遇到即为状态不一致），或模块加载时断言 `_YUV_FORWARD` 键集 ⊇ `TOPOS_MATRIX_CODES` 键集。
  - 置信度: 高（当前不可达，防御性缺口）

- [AUD-33408] [Suggestion] 代码健康：close() 内不可达的重复 0 帧检查、`_audio_warned` 无效赋值
  - 位置: src/shared/export/topos_encoder.py:1602-1605 与 1610-1611（重复）、2231-2232（无效赋值）
  - 证据:
    ```python
    # 1602-1605（对一切容器模式生效）
    if self._frame_count == 0:
        raise RuntimeError(
            "Topos encoder: 没有任何帧被编码——拒绝产出空文件"
        )
    ```
    ```python
    # 1610-1611（image_sequence 分支内的同型检查，永不可达）
    if self._frame_count == 0:
        raise RuntimeError("Topos image sequence: no frames written")
    ```
    ```python
    # add_audio_stream 尾部（2231-2232）——_audio_warned 从未用于告警去重
    if not self._audio_warned:
        self._audio_warned = True
    ```
  - 影响: 死代码与无效赋值降低可读性；`_audio_warned` 的"警告去重"语义从未生效（疑似历史遗留）。
  - 建议: 删除 1610-1611 重复检查与 2231-2232 无效赋值（或补全其本意的告警逻辑）。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：本文件无 OpenGL/GPU 资源（纯 CPU 导出侧）；native 句柄（codec/mux/GOP context/线程池登记）在 close/abort 全部退出路径均有释放与置 None（C13/P1-03 修复经代码确认）
- GLSL·HDR 正确性：无 GLSL；HDR/浮点契约处理正确——信号域下界裁负、上界仅显式警告不做编码器内无条件 [0,1] 静默裁剪（R2），HALF 样本域无 clamp 无标定直通，alpha 16-bit 满刻度容器 `<<(16−N)` 标度与 spec §8.6 对齐（编码/解码对称）
- 信号防递归：文件内无 Qt 控件/信号，不适用
- planar 输入验证：格式白名单 / pf·位深逐项匹配（复验 P0-01）/ dtype 强制 uint16 / 几何 / 紧排列 stride / 码值域（首帧+每 256 帧抽样+环境变量强制全检）/ alpha 位深独立校验 / premultiplied 整段一致性——完整
- 音频输入验证与字节序：codec/采样率/声道白名单 fail-fast，形状/采样率与声明一致才写，float32 NaN/Inf 拒绝，24-bit 走 float64→'>i4' 切高 3 字节、32-bit float64 量化避免 2^31 舍入越界——均正确
- 状态提交一致性（mov 路径）：临时文件 + finish 成功后 `os.replace` 原子替换；0 帧/定稿失败/mux close 失败/替换失败均删临时文件且不发布（复验 P1-03 确认）；abort 幂等
- 码控/预算闭环：三级预算换算（max_file 先扣音轨+容器预留）、close 硬裁决、alpha 预算逐帧记账 + tpcB 元数据（u16 bp 饱和标志区分 6.5535 与 65.5）——逻辑一致
- 线程模型：`_encode_prepared`（mux 写/pts/记账）严格限定主调用线程；GOP 分段 worker 输出按段序确定性回填（注释声明 1 vs 4 worker 逐字节一致测试钉死）；流水线帧序经 seq 重排防御断言
- 构造期收缩域：entropy/aq/rdo/gop/tier×pix_fmt×profile 交叉（4444 须 444、xq 须 12-bit、raw 须 CFA+qp 显式、HALF 须 profile5+mode1 等）均在 `__init__` 显式拒绝，无静默降级
- 类型注解：公开 API 与绝大多数方法有完整注解（`_build_configs -> tuple`、`_seg_encode` 无返回注解等个别内部方法从宽，不单列）

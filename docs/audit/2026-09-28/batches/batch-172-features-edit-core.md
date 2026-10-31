# batch-172 审查报告：src/features/edit/core（工程归档/迁移/序列化/代理）

- 批次：batch-172 [P1]，编号区间 AUD-17200–AUD-17299
- 文件数：5（共 1,842 行）
- 审查日期：2026-09-28
- 跨文件核实：调用方（media_pool_panel.py、edit_project_io.py、stem_export.py、timeline_export.py）、依赖（project.py MediaAsset、asset_usage_index.py、disk_proxy_service.py、shared/project/manifest.py）均已 Read/grep 验证

---

## src/features/edit/core/project_archive.py (行数: 249)

### 发现

- [AUD-17201] [Major] 目录类素材（image_sequence）SHA-256 校验被静默跳过，状态仍记为 copied
  - 位置: src/features/edit/core/project_archive.py:204-221
  - 证据:
    ```python
    if os.path.isdir(src):
        shutil.copytree(src, dest_path, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest_path)
    ...
    src_hash = _sha256_of(src) if os.path.isfile(src) else ""
    dst_hash = _sha256_of(dest_path) if os.path.isfile(dest_path) else ""
    status = STATUS_COPIED if src_hash == dst_hash else STATUS_HASH_MISMATCH
    ```
  - 影响: 目录素材（image_sequence 不在 MediaAsset.is_virtual 排除列表内，project.py:323-327）两个哈希均为空串，""=="" 恒真——复制损坏/不完整的序列目录仍被记为 copied（sha256=""），归档报告的校验承诺对目录素材完全失效。
  - 建议: 对目录做逐文件哈希（排序后聚合），或引入独立状态（如 verification_skipped）明确报告"未校验"，不得与 copied 混同。
  - 置信度: 高

- [AUD-17202] [Major] 哈希/大小计算在 per-file try 之外，单个文件 OSError 即中断整个收集并留下无报告的半成品归档
  - 位置: src/features/edit/core/project_archive.py:219-232（对照 try 块 203-217；_sha256_of 定义 43-51 无异常处理）
  - 证据:
    ```python
    try:
        if os.path.isdir(src):
            shutil.copytree(...)
        else:
            shutil.copy2(src, dest_path)
    except OSError as exc:
        ...  # 仅覆盖复制
        continue

    src_hash = _sha256_of(src) if os.path.isfile(src) else ""   # 越界：open() 可抛 OSError
    ...
    file_size=os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0,  # 同样未防护
    ```
  - 影响: 源文件在 exists() 检查后被删除/加锁（权限、杀毒软件、竞态）时 `_sha256_of` 的 open 抛 OSError，异常直接冲出 collect_project——调用方（media_pool_panel.py:5539 的 except 分支）只弹错误框，已复制的媒体残留在目标目录、缺失素材使用位置报告全部丢失。
  - 建议: 将哈希与 getsize 纳入同一 per-file 容错，失败降级为 STATUS_COPY_FAILED 记录并 continue，保证报告始终产出。
  - 置信度: 高

- [AUD-17203] [Major] collect_project 为长阻塞同步 I/O 且无进度/取消钩子，UI 调用方在主线程直接执行
  - 位置: src/features/edit/core/project_archive.py:140-249（签名仅 target_dir/sequences/include_unused/media_dir，无 progress/cancel 参数）；调用方 src/features/edit/ui/media_pool_panel.py:5539（菜单槽内同步调用）
  - 证据:
    ```python
    def collect_project(
        project: EditProject,
        *,
        target_dir: str,
        sequences: Iterable[Any] = (),
        include_unused: bool = True,
        media_dir: str = "media",
    ) -> tuple[EditProject, ArchiveReport]:
    ```
    （每个素材执行整文件复制 + 源/目标各一次全文件 SHA-256，即 3 倍数据量的磁盘 I/O，全部同步）
  - 影响: 多 4K 素材工程（几十 GB 级）收集时 Qt 主线程冻结数分钟，无进度反馈、无法取消，事件循环停摆。
  - 建议: 增加 progress_callback 与 cancelled_fn 参数（在素材粒度轮询），由调用方移入后台线程执行。
  - 置信度: 高

- [AUD-17204] [Suggestion] 重复收集到同一归档目录时 copytree(dirs_exist_ok=True) 只合并不清理，旧残留媒体永久留在归档内
  - 位置: src/features/edit/core/project_archive.py:205
  - 证据:
    ```python
    if os.path.isdir(src):
        shutil.copytree(src, dest_path, dirs_exist_ok=True)
    ```
  - 影响: 二次收集（用户重复选同一目标目录）时上次已删除源的媒体副本仍在归档里，report.passed 为 True 但归档内容与工程不一致。
  - 建议: 收集前清空/重建 media 子目录，或每次收集使用带时间戳的独立子目录。
  - 置信度: 中

- [AUD-17205] [Suggestion] sequences 参数类型为 Iterable[Any]，丢失类型约束
  - 位置: src/features/edit/core/project_archive.py:144
  - 证据: `sequences: Iterable[Any] = (),`
  - 影响: 传入错误序列类型（缺 used_asset_ids/locations_for 协议）要到运行时才暴露；与项目强类型规范不符。
  - 建议: 收窄为 `Iterable[TimelineSequence]`（或定义协议类型）。
  - 置信度: 高

### 通过项
- 线程安全：纯核心模块无共享可变状态（frozen dataclass、全部局部变量），无 Qt 依赖
- GPU 资源生命周期：不涉及 OpenGL/GPU 资源
- GLSL/HDR 正确性：不涉及着色器与 HDR 数据
- 信号防递归：不涉及 Qt 信号
- 状态提交一致性：batch_relink_assets 派生新工程实例，原工程不被修改
- 输入验证：_sanitize_name + _unique_dest_name 防路径注入与目标覆盖；虚拟素材正确排除；缺失素材不静默跳过且带全部使用位置

---

## src/features/edit/core/project_migration.py (行数: 216)

### 发现

- [AUD-17211] [Suggestion] migrate_project_payload 就地修改传入载荷，浅拷贝仅保护顶层版本键，与"返回新载荷"语义不符
  - 位置: src/features/edit/core/project_migration.py:88-106（_walk 原地改写嵌套 dict）、211-215（仅 shallow copy）
  - 证据:
    ```python
    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("blur_type") == "directional":
                node["blur_type"] = "motion"      # 原地修改调用方数据
    ...
    if isinstance(migrated, dict):
        migrated = dict(migrated)                   # 浅拷贝，嵌套结构仍共享
        migrated[EDIT_CONTENT_VERSION_KEY] = reached
    ```
  - 影响: 调用方持有的原 full_data 在函数返回前已被改写；docstring 承诺"返回 (新载荷, …)"暗示非破坏语义。已核实当前唯一生产调用方（edit_project_io.py:1049）不复用原 dict，暂无实际损伤，属契约隐患。
  - 建议: 文档明确"就地修改、返回同一对象"，或对载荷做 deepcopy 后迁移。
  - 置信度: 高（行为确定；现有调用方不受影响）

### 通过项
- 线程安全：纯函数操作，CONTENT_MIGRATIONS 为不可变 tuple，无共享可变状态
- 异常与取消路径：单步迁移异常记录日志后 break 不阻断打开；实际到达版本如实写回（不谎报）
- 迁移链健壮性：_applicable_steps 有环防护（range(len(CONTENT_MIGRATIONS)+1)），链断裂保守停止并告警
- 前向兼容：较新版本工程不盲迁移，返回告警条目
- 输入验证：_resolve_version 对非数字版本回退 v1；非 dict 载荷原样返回
- 版本一致性：EDIT_CONTENT_SCHEMA_VERSION 与共享层 manifest.py:29 的 CURRENT_EDIT_CONTENT_SCHEMA_VERSION=3 一致，迁移链终点 v3 匹配
- 类型注解：完整
- GPU/GLSL/信号防递归/undo维度：不涉及

---

## src/features/edit/core/project_serializer.py (行数: 425)

文件头标注 SUPERSEDED(2026-08-19)、无生产引用——已核实：全仓 grep 无任何 `project_serializer` 导入；现行实现为 src/features/edit/core/serialization.py:160 的同名类（edit_project_io.py 实际使用的是后者）。以下发现按休眠代码评估。

### 发现

- [AUD-17221] [Suggestion] save 写 ZIP 工程包而 load 仍按裸 JSON 解析，类内读写不对称（load 永远打不开本类保存的文件）
  - 位置: src/features/edit/core/project_serializer.py:171-178（save 走 ToposProjectIO ZIP 包）vs 204-205（load 用 open+json.load）
  - 证据:
    ```python
    success = ToposProjectIO.save(
        topos_project, filepath, config,
        full_assets_data=full_assets_data, ...)
    ...
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    ```
  - 影响: 若此归档文件被复用，加载自身保存的 .topos（ZIP 二进制）必然 UnicodeDecodeError/JSONDecodeError → 返回 None。load 侧读取的 `path_relative`/`path_original` 键也与 save 侧 full_assets_data 写出的 `path` 键不对应。因无生产引用，当前为休眠缺陷。
  - 建议: 删除该归档文件（推荐），或在其头部明确"load 仅支持旧版散装 JSON，与 save 不构成往返"。
  - 置信度: 高（不对称事实；因 SUPERSEDED 无生产引用，实际风险已中和）

- [AUD-17222] [Suggestion] save/load 捕获宽泛 Exception 且用 traceback.print_exc() 输出，错误细节不进日志体系
  - 位置: src/features/edit/core/project_serializer.py:184-188、285-289
  - 证据:
    ```python
    except Exception as e:
        logger.error(f"Failed to save project: {e}")
        import traceback
        traceback.print_exc()
        return False
    ```
  - 影响: 违反"不得宽泛 except 掩盖领域错误"铁律：磁盘满、权限、序列化数据损坏等不同原因均折叠为 False/None，堆栈只进 stderr 不进 logger，现场排障困难。
  - 建议: 改用 logger.exception(...) 并按异常类型分层处理。
  - 置信度: 高

- [AUD-17223] [Suggestion] 死代码与未使用导入：_to_relative_path 无调用、uuid/asdict 未使用、create_default_project 的 project_dir/sample_rate 参数被忽略
  - 位置: src/features/edit/core/project_serializer.py:25（import uuid）、27（from dataclasses import asdict）、338-365（_to_relative_path 全文无调用）、392-425（参数未使用）
  - 证据:
    ```python
    import uuid
    ...
    from dataclasses import asdict
    ...
    @staticmethod
    def _to_relative_path(absolute_path: str, base_dir: str) -> str:  # 保存已委托 ToposProjectIO，本方法无调用
    ```
  - 影响: 误导后续维护者以为相对路径转换在此发生（实际在 ToposProjectIO，relative_paths=True）；未用导入增加噪音。
  - 建议: 随 AUD-17221 一并删除归档文件；若保留则清理死代码。
  - 置信度: 高

### 通过项
- SUPERSEDED 状态核实：无生产引用（serialization.py 为现行实现），以下维度按休眠代码确认无活跃风险
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- relink_assets / check_offline_assets 逻辑本身正确（派生新实例、不修改原工程）
- 输入验证：load 侧对缺失键有默认值兜底（fps/width/height/sample_rate 等）

---

## src/features/edit/core/proxy_generator.py (行数: 763)

### 发现

- [AUD-17231] [Major] PyAV 取消路径残留截断的代理文件，之后会被识别为 ready 代理进入播放
  - 位置: src/features/edit/core/proxy_generator.py:324-326（取消 return None 在 try 内，仅 finally 关容器）；对照 375-378（只有 except 分支才 _remove_quietly）
  - 证据:
    ```python
    for packet in input_container.demux(*streams):
        if cancelled_fn is not None and cancelled_fn():
            return None          # finally 只 close 容器，不删半成品文件
    ...
    finally:
        output_container.close()
    ...
    except Exception as e:
        logger.debug(f"PyAV video proxy failed: {e}")
        _remove_quietly(proxy_path)   # 仅异常路径清理
        return None
    ```
  - 影响: 取消后磁盘上留下未 finalize 的 MP4。已核实 DiskProxyService.get_disk_proxy_info（src/shared/media/disk_proxy_service.py:246-266）按"代理文件存在且有效"即返回 READY——之后播放热路径取到坏代理，解码错误/黑帧。正常完成路径的 size>0 校验（360-362 行）在取消 return 时被跳过。
  - 建议: 取消 return 前调用 _remove_quietly(proxy_path)（与异常路径对齐）。
  - 置信度: 高

- [AUD-17232] [Major] CLI 兜底超时/失败不清理部分输出文件，且 kill 后不回收子进程
  - 位置: src/features/edit/core/proxy_generator.py:445-449
  - 证据:
    ```python
    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg timeout for {asset.path}")
        if process is not None:
            process.kill()
        return None
    ```
  - 影响: 超时后部分写入的 proxy_path 留在磁盘（后果同 AUD-17231，会被判 READY）；kill 后未 communicate()/wait()，子进程在回收前呈僵尸态。returncode!=0 分支（421-424）同样不清理坏输出。
  - 建议: kill 后 `process.communicate(timeout=5)` 回收，并在所有失败分支删除 proxy_path。
  - 置信度: 高

- [AUD-17233] [Major] 进度百分比按 frame.time*100 计算，未除以总时长，任何 >1 秒的视频进度立即到 95%
  - 位置: src/features/edit/core/proxy_generator.py:345-348
  - 证据:
    ```python
    if progress_callback:
        progress_callback(
            min(95, int(proxy_frame.time * 100))
        )
    ```
  - 影响: `proxy_frame.time` 是秒；1 秒后 time*100≥100 被 min 截到 95，此后全程显示 95%——进度条对所有真实视频（均 >1s）立刻失真，用户无法判断编码是否卡死。UI 端 `_on_proxy_progress` 收到的即此值。
  - 建议: 打开容器时取 duration（如 `float(input_stream.duration * input_stream.time_base)` 或 `input_container.duration`），按 `frame.time/duration*100` 归一化。
  - 置信度: 高

- [AUD-17235] [Major] delete_all_proxies_in_directory 以子串匹配删除文件，可误删文件名含 "proxy" 的用户原片
  - 位置: src/features/edit/core/proxy_generator.py:750-755
  - 证据:
    ```python
    for filename in os.listdir(directory):
        if suffix in filename:
            filepath = os.path.join(directory, filename)
            if os.path.isfile(filepath):
                try:
                    os.remove(filepath)
    ```
  - 影响: 用户自命名含 "_proxy" 的原始媒体（如 "interview_proxy_reel.mov"）会被当代理永久删除（数据丢失）。已核实全仓（含测试）无调用方，属休眠风险，但一旦被接入即危险。
  - 建议: 匹配 `_generate_proxy_path` 的精确生成模式（原文件名+suffix+同扩展名），即对目录内每个文件 f 仅删除存在对应 `name+suffix+ext` 源的条目；或直接删除此无引用的公共 API。
  - 置信度: 高（行为确定；当前无调用方）

- [AUD-17234] [Suggestion] ProxyGeneratorWorker._cancelled 无任何公开设置途径，worker 路径的取消能力不可达
  - 位置: src/features/edit/core/proxy_generator.py:131、141-142
  - 证据:
    ```python
    self._cancelled = False
    ...
    cancelled_fn=lambda: self._cancelled,
    ```
    （全仓 grep 无外部对该属性赋值；唯一使用方 media_pool_panel.py:10210 启动后未设置）
  - 影响: 经 QThreadPool 启动的代理任务无法取消；cancelled_fn 恒为 False，AUD-17231 的取消路径在生产中实际不可触发（但 generate_proxy 静态 API 允许外部传入 cancelled_fn，缺陷仍真实存在）。
  - 建议: 提供 `cancel()` 方法并改用 threading.Event；普通 bool 跨线程读写亦无同步保证。
  - 置信度: 高

- [AUD-17236] [Suggestion] PyAV 路径音频码率硬编码 128000，忽略 config.audio_bitrate
  - 位置: src/features/edit/core/proxy_generator.py:310（对照 79 行 `audio_bitrate: str = "128k"`）
  - 证据:
    ```python
    out_audio_stream.bit_rate = 128000
    ```
  - 影响: 自定义音频码率配置仅在 CLI 兜底路径生效，主路径固定 128k，配置语义不一致。
  - 建议: 解析 config.audio_bitrate（"128k"→131072 或 128000）后赋值。
  - 置信度: 高

- [AUD-17237] [Suggestion] 代理统一转 8bit yuv420p 且不处理色彩范围/原色域，HDR 源的代理预览会偏色
  - 位置: src/features/edit/core/proxy_generator.py:286-291、340-342
  - 证据:
    ```python
    output_stream.pix_fmt = "yuv420p"
    ...
    proxy_frame = decoded_frame.reformat(
        width=out_w, height=out_h, format="yuv420p",
    )
    ```
  - 影响: HDR（PQ/HLG、limited range）源直落 SDR 8bit 无 tone-mapping/范围标记，代理预览对比度与色彩错误。风险受限：proxy_policy.export_use_master 默认 True，坏代理不进导出链，仅影响编辑预览判断。
  - 建议: 探测源色彩空间后走 zscale/tone-map（CLI）或设置 PyAV 色彩属性，至少在代理元数据标记"SDR 预览，勿用于校色判断"。
  - 置信度: 中

- [AUD-17238] [Suggestion] generate_proxy 的异常清理删除的是扩展名改写前的路径，图片代理实际输出 (.jpg) 不会被清理
  - 位置: src/features/edit/core/proxy_generator.py:184、198-206（清理）vs 483-484（图片路径内部改扩展名）
  - 证据:
    ```python
    proxy_path = _generate_proxy_path(asset.path, config.suffix)   # foo_proxy.png
    ...
    if os.path.exists(proxy_path):
        try:
            os.remove(proxy_path)          # 删的是 foo_proxy.png
    ...
    base_path = os.path.splitext(proxy_path)[0]
    proxy_path = f"{base_path}.{config.image_format}"   # 实际输出 foo_proxy.jpg
    ```
  - 影响: 若图片生成路径向上抛异常（罕见——PyAV/CLI 子路径各自内部捕获），残留 foo_proxy.jpg。低触发概率的清理错位。
  - 建议: 图片路径改扩展名的逻辑上移到 _generate_proxy_path（按 media_type 分派），使外层清理与实际输出一致。
  - 置信度: 中

- [AUD-17239] [Suggestion] progress_callback 类型标注为 Optional[Any]，丧失可调用契约
  - 位置: src/features/edit/core/proxy_generator.py:161、217、385、467 等
  - 证据: `progress_callback: Optional[Any] = None,`
  - 影响: 传入非可调用对象（如 int）到运行时才报 TypeError；与项目强类型规范不符。
  - 建议: 统一标注为 `Optional[Callable[[int], None]]`。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：不直接持有 OpenGL 资源；PyAV 容器以 try/finally 成对 close（355-358、539-543）
- 线程安全（信号）：ProxySignals 从池线程 emit，Qt 跨线程信号为队列投递，无 UI 线程 OpenGL 操作
- 异常路径：PyAV 主路径失败静默降级 CLI 兜底；PyAV 异常时 _remove_quietly 清理（375-378）
- 取消语义（部分）：取消后阻止 CLI 兜底重跑（245-246）
- 成功校验：两条路径均检查输出文件存在与大小后返回
- check_ffmpeg_available：PyAV/CLI 双检测，5s 超时与 FileNotFoundError 均有防护
- delete_proxy：OSError 捕获并返回 False，不抛出

---

## src/features/edit/core/proxy_policy.py (行数: 189)

### 发现

- [AUD-17251] [Major] mode="all" 分支先于尺寸未知检查，违反模块自述的"未知尺寸不盲生成"不变量
  - 位置: src/features/edit/core/proxy_policy.py:144-150
  - 证据:
    ```python
    if policy.mode == "all":
        if _codec_class(codec) == "topos":
            return ProxyActionDecision("skip", "topos_edit_codec_no_proxy_needed")
        return ProxyActionDecision("generate", "policy_all")
    if width <= 0 or height <= 0:
        return ProxyActionDecision("skip", "dimensions_unknown")
    ```
  - 影响: "all" 模式下 metadata 缺失（width/height=0）的素材返回 generate——docstring 明言"也不能盲生成（P2-04 排序需要尺寸）"，但该保证仅在 auto 分支成立。调用方（media_pool_panel.py:10048，metadata 缺失时显式传 0）在 all 模式下会把无尺寸素材盲目入队。
  - 建议: 将 `width <= 0 or height <= 0` 检查移到 mode=="all" 分支之前，使 dimensions_unknown 对所有模式生效。
  - 置信度: 高

- [AUD-17252] [Suggestion] export_source_path 在 master 路径为空时回退返回 effective_path（可能是代理），与"导出强制原片"语义冲突
  - 位置: src/features/edit/core/proxy_policy.py:166-171
  - 证据:
    ```python
    if policy.export_use_master:
        if path:
            return path
        effective = getattr(asset, "effective_path", None)
        return effective if isinstance(effective, str) and effective else ""
    ```
  - 影响: MediaAsset.effective_path（project.py:224-228）在 has_proxy 时返回 proxy_path——即 export_use_master=True 分支可能返回代理进导出链，恰是模块 docstring 要防的成片质量/HDR 降级。已核实当前 MediaAsset 校验非虚拟素材 path 非空（project.py:142-143），实际触发面小；但参数类型为 Any，不排除其他资产实现。
  - 建议: export_use_master 分支在 path 为空时直接返回空串，由调用方显式报"素材无原片"，不得静默换代理。
  - 置信度: 中（行为确定；现有数据模型下可达性低）

- [AUD-17253] [Suggestion] export_source_path/_policy_of 参数类型为 Any，绕过静态检查
  - 位置: src/features/edit/core/proxy_policy.py:163、178
  - 证据: `def export_source_path(project: Any, asset: Any) -> str:`
  - 影响: 三个导出调用方（stem_export.py:593、timeline_export.py:4003/6035/9310）传入的应为 EditProject/MediaAsset，Any 掩盖字段拼写与类型错误。
  - 建议: 标注为 EditProject/MediaAsset（duck-typing 需求可用 Protocol 表达）。
  - 置信度: 高

### 通过项
- 线程安全：frozen dataclass + 纯函数决策，无共享可变状态
- GLSL/HDR/GPU 生命周期/信号防递归：不涉及
- 状态提交一致性：策略随工程持久化（to_dict/from_dict 往返对称），决策纯函数可复算
- 输入验证：__post_init__ 校验 mode/正数尺寸；_codec_class 对空/非字符串 codec 安全
- 异常路径：_policy_of 对 getter 抛异常有兜底（noqa 注明理由），取源路径不抛
- codec 分类与阈值判定（auto 分支）逻辑正确，reason 码可诊断

---

## 汇总

| 文件 | Critical | Major | Suggestion |
|------|----------|-------|------------|
| project_archive.py | 0 | 3 | 2 |
| project_migration.py | 0 | 0 | 1 |
| project_serializer.py | 0 | 0 | 3 |
| proxy_generator.py | 0 | 4 | 5 |
| proxy_policy.py | 0 | 1 | 2 |
| 合计 | 0 | 8 | 12 |

无 Critical。最高优先修复建议：AUD-17231/17232（坏代理残留→播放错误）、AUD-17202（收集中断丢报告）、AUD-17251（all 模式盲生成）。

# 批次报告：batch-356 [P2] src/shared/media//

- 编号区间：AUD-35600–AUD-35699
- 审查文件（3 个，2,698 行）：
  - `src/shared/media/proxy_generator.py` (1,666)
  - `src/shared/media/proxy_manager.py` (698)
  - `src/shared/media/proxy_manifest.py` (334)
- 汇总：Critical 0 / Major 9 / Suggestion 13
- 交叉验证参考：`src/shared/media/proxy.py`、`src/shared/media/topos_source.py`、`src/shared/codec/topos_binding.py`、`src/shared/media/disk_proxy_service.py`、`src/shared/media/asset_proxy_manager.py`

---

## src/shared/media/proxy_generator.py (行数: 1666)

### 发现

- [AUD-35600] [Major] 成功路径末尾对源文件重复探测两次，且 fps=0 或两次探测结果不一致时把成功误报为失败
  - 位置: src/shared/media/proxy_generator.py:1643
  - 证据:
    ```python
    return ProxyGenerationResult(
        success=True,
        source_path=source_path,
        proxy_path=proxy_path,
        proxy_size_bytes=proxy_size,
        proxy_resolution=proxy_res,
        duration_seconds=total_frames / self._probe_source(source_path)[2] if self._probe_source(source_path) else 0.0,
    ```
  - 影响: 条件与取值各调用一次 `_probe_source`（每次为 PyAV container open 或 ffprobe 子进程），而 `generate()` 入口已探测过 fps——每个成功编码白白多付两次全量探测。更严重的是：fps 为 0.0（average_rate/base_rate 为 0/1 的畸形流可达）时 `total_frames / 0.0` 抛 ZeroDivisionError；两次独立探测结果不一致（第二次返回 None）时 `None[2]` 抛 TypeError——两者均被 1656 行 `except Exception` 捕获，代理文件已生成却返回失败结果，上层（ProxyManager/DiskProxyService）会按失败处理甚至触发重试。
  - 建议: 复用 `generate()` 入口探测到的 `fps`（随参数或实例状态传入），删除成功路径上的两次 `_probe_source`；若必须再探测，先缓存到局部变量并防御 fps<=0。
  - 置信度: 高

- [AUD-35601] [Major] CLI 路径进度监控按 `\n` 分行——ffmpeg 统计行以 `\r` 结尾，编码期间 readline 阻塞，帧数解析与取消轮询全部停摆
  - 位置: src/shared/media/proxy_generator.py:1538-1548
  - 证据:
    ```python
    if select.select([process.stderr], [], [], 0.1)[0]:
        line = process.stderr.readline().decode('utf-8', errors='replace')
        if line:
            stderr_lines.append(line)
            # 解析帧数
            if 'frame=' in line:
                try:
                    frame_part = line.split('frame=')[1].split()[0]
                    frame_count = int(frame_part)
    ```
  - 影响: stderr 为二进制管道，`readline()` 只按 `\n` 返回；ffmpeg 横幅行结束后，编码期间的 stats 更新全部以 `\r` 结尾 → 监控线程阻塞在 readline 直至编码结束的最终 `\n`。期间 `frame=` 解析、0.5s 进度回调、`self._cancelled` 轮询与循环内 `process.kill()` 均不执行：CLI 路径进度条全程停在低位，取消仅靠 `cancel()` 写 stdin 'q' 兜底（功能上仍可退出，但循环内 kill 路径失效）。
  - 建议: 不用 readline：按字节读取并按 `\r` 或 `=` 切片解析 stats（业界解析 ffmpeg 进度的通行做法），或改用 `-progress pipe:1` + 行协议。
  - 置信度: 高

- [AUD-35602] [Major] Windows 上 CLI 路径 stderr 永不排水——管道缓冲写满后 ffmpeg 阻塞，监控循环死锁
  - 位置: src/shared/media/proxy_generator.py:1549-1552
  - 证据:
    ```python
    except (ImportError, OSError):
        # Windows 不支持 select on pipes
        import time as _time
        _time.sleep(0.1)
    ```
  - 影响: Windows 的 `select.select` 仅支持 socket，对管道句柄每轮抛 OSError → 走 except 分支只 sleep，从不读取 stderr。ffmpeg 统计输出持续写入管道，OS 缓冲（约 4–64KB）写满后 ffmpeg 阻塞在 stderr 写 → `process.poll()` 永不返回 → 监控循环无限空转，编码挂死。项目含 `src/shared/media/windows_hw`，Windows 为受支持平台；IMAGE_SEQUENCE 格式只能走 CLI 路径（`_run_pyav` 明确拒绝），长序列必然触雷。
  - 建议: Windows 分支改为非阻塞读或专用线程持续 `stderr.read(4096)` 排水并解析；或统一用 `communicate()` + `-progress` 管道线程模型。
  - 置信度: 中

- [AUD-35603] [Major] `_run_pyav_to_topos` 硬编码 limited range：全量程源被错标 limited，代理预览对比度/颜色错误
  - 位置: src/shared/media/proxy_generator.py:973、988（配合 1017-1018 的 reformat 未传 color_range）
  - 证据:
    ```python
    movie_cfg = codec.movie_config(
        out_w, out_h,
        ...
        color_range=0,
    ...
    fc.color_range = 0
    ...
    frame = decoded_frame.reformat(
        width=out_w, height=out_h, format="yuv422p10le")
    ```
  - 影响: `reformat` 未指定 `color_range`，swscale 不做量程转换——full-range 源（yuvj/JPEG 系）数据保持 0-255 全量程，而容器与帧标签恒写 limited（0）→ 播放侧按 16-235 解码 0-255 数据，画面灰蒙、对比度/饱和度失真。同文件 `_run_topos`（TPIC 源）对 range 显式映射并校验（568-573 行），此路径却无对应处理。代理是预览口径，颜色错标直接影响剪辑审看。
  - 建议: 读取 PyAV 帧或 codec_context 的 color_range，full 时传给 reformat 显式转换，或如实写入标签；无法判定时按 G7 口径告警一次。
  - 置信度: 中

- [AUD-35604] [Suggestion] `_run_topos` 对 total_frames=0 的退化源产出 0 帧空代理并返回成功
  - 位置: src/shared/media/proxy_generator.py:775、829-842
  - 证据:
    ```python
    for i in range(int(md.total_frames or 0)):
        if self._cancelled:
            ...
    mux.finish()
    mux.close()
    mux = None
    proxy_size = os.path.getsize(proxy_path) if os.path.exists(proxy_path) else 0
    return ProxyGenerationResult(
        success=True, ...
    ```
  - 影响: `md.total_frames` 为 0/None 时循环零次即 finish，返回 success=True 的空 TPIC 代理并被缓存为"有效"。对比 `_run_pyav_to_topos` 有显式防护（1053-1054 行 `if frame_count == 0: return _fail(...)`），此路径缺失。TPIC 的 sample_count 通常权威且 ≥1，属边缘情形，故降为 Suggestion。
  - 建议: 循环后校验实际编码帧数，为 0 时走 `_fail`。
  - 置信度: 中

- [AUD-35605] [Suggestion] 取消标志竞态：generate() 入口重置 `_cancelled`，可吞掉启动瞬间发出的取消
  - 位置: src/shared/media/proxy_generator.py:200-201（对照 proxy_manager.py:591-594 与 316-318）
  - 证据:
    ```python
    start_time = time.time()
    self._cancelled = False
    ```
  - 影响: ProxyManager `_run_task` 在锁内置 status="generating" 后调用 generate()；若 cancel_task 恰在"置 generating 之后、generate() 重置标志之前"的窗口内执行（置 `_cancelled=True`），generate() 随即清零标志，任务完整跑完编码并在结束时把 status 覆盖为 "complete"——用户取消被静默无视。窗口很窄，影响有限。
  - 建议: generate() 入口改为仅在"未被取消"时清除标志（如 `if not self._cancelled: ...` 不可行则提供显式 reset() 由 manager 在确认状态后调用），或循环内同时检查任务层取消状态。
  - 置信度: 高（机制）/影响小

- [AUD-35606] [Suggestion] `_probe_source_with_pyav` 宽泛 except 把"无视频流"等领域错误吞成 None，根因丢失
  - 位置: src/shared/media/proxy_generator.py:1180-1184
  - 证据:
    ```python
            finally:
                container.close()
        except Exception as e:
            logger.error(f"PyAV source probe failed: {e}")
            return None
    ```
  - 影响: `container.streams.video[0]` 在无视频流时抛 IndexError，与解码器错误、权限错误等一并被吞，回退 ffprobe 也失败后用户只见笼统的 "Failed to probe source file"，违背"不得宽泛 except 掩盖领域错误"。
  - 建议: 捕获 `av.AVError`/`OSError` 与 IndexError 分别处理并上抛结构化原因（至少把异常类型写进 error_message）。
  - 置信度: 高（事实层面）

### 通过项
- GPU 资源生命周期：纯 subprocess/numpy 路径，无 ModernGL/OpenGL 对象，不存在 UI 线程触 GL 问题
- 信号防递归：模块不含 Qt 信号/控件，无递归风险
- GLSL·HDR 正确性（TPIC 路径）：源色彩标签显式映射、未知值显式失败不默认掩盖（R-13）；位深取 min(请求,源) 不盲目上采样；GBR→bt709 近似有一次性显式告警；alpha 位深取自解码平面权威声明（R1）
- 取消/失败路径清理：三条内部编码路径均在失败/取消时删除半成品代理文件（`_fail`、`_run_pyav` finally 的 `completed` 标志）
- 资源释放：container/mux/src 均有 finally 关闭；`ToposMuxFile.close()` 幂等（已核实 topos_binding.py:2700-2706），`_run_topos` 取消路径的双重 close 无害
- 输入验证：源文件存在性前置检查；ffprobe 输出分段数校验

---

## src/shared/media/proxy_manager.py (行数: 698)

### 发现

- [AUD-35607] [Major] `max_concurrent` 从未被实施——每任务即时起独立线程，并发编码数无上限
  - 位置: src/shared/media/proxy_manager.py:180-181、254-261
  - 证据:
    ```python
        self.config = config
        self.max_concurrent = max_concurrent
    ...
            # 启动后台线程
            thread = threading.Thread(
                target=self._run_task,
                args=(task,),
                daemon=True,
                name=f"ProxyGen-{task_id}",
            )
            thread.start()
    ```
  - 影响: `self.max_concurrent` 在全文件仅此一处赋值、无任何读取；`queue_proxy_generation` 对每个任务直接 start 新线程。批量导入 N 个素材即 N 路并行 ffmpeg/PyAV 编码抢占 CPU，违背文档"管理多个并发生成任务/max_concurrent"语义与 DiskProxyService `max_concurrent_tasks=2` 的意图，直接冲击 4K 实时播放/交互基准。
  - 建议: 用 `threading.Semaphore(max_concurrent)` 或任务队列+固定 worker 池实施限流，排队任务延迟启动（可与现有 `_generation_deferred` 等待循环合并）。
  - 置信度: 高

- [AUD-35608] [Major] on_complete/on_error 在持有 `self._lock` 时调用——与 DiskProxyService 形成锁序倒置死锁风险
  - 位置: src/shared/media/proxy_manager.py:612-635（`_run_task`）、215-227（queue 时的同步 on_complete 也在锁内）
  - 证据:
    ```python
        # 更新任务状态
        with self._lock:
            task.result = result
            task.completed_at = time.time()

            if result.success:
                task.status = "complete"
                ...
                if task.on_complete:
                    task.on_complete(result)
    ```
  - 影响: 已核实（disk_proxy_service.py:602-605）`on_complete` 链上的 `_on_generation_complete` 首先获取服务层 `self._lock`，且继续调用 `_dispatch_status_callbacks`/`_update_frame_provider_service`；同时服务侧 `get_disk_proxy_info`（disk_proxy_service.py:262-269）持服务锁调用 `manager.get_valid_proxy_path`→需要管理器锁。worker 线程锁序 manager._lock→service._lock，UI 线程 service._lock→manager._lock，构成经典 AB-BA 死锁条件；即使不死锁，持管理器锁执行 UI 分发也显著拉长其它代理查询的阻塞时间。progress_wrapper（597-602）已把回调移出锁外，on_complete/on_error 应同样处理。
  - 建议: 在 `with self._lock:` 内仅更新任务状态与缓存，出锁后再调用 on_complete/on_error（对齐 progress_wrapper 的写法）。
  - 置信度: 高（锁序事实确凿；死锁触发需时序）

- [AUD-35609] [Major] `pause_all_generation` 对主编码路径无效——PyAV/topos 进程内编码不设 `self._process`，pause()/resume() 恒返回 False
  - 位置: src/shared/media/proxy_manager.py:280-284（对照 proxy_generator.py:386-395 与 1494-1500）
  - 证据:
    ```python
            for task in self._tasks.values():
                if task.status == "generating" and task.generator is not None:
                    if task.generator.pause():
                        paused += 1
    ```
  - 影响: `ProxyGenerator.pause()` 仅在 `self._process is not None` 时生效，而 `_process` 只在 `_run_ffmpeg`（CLI 兜底路径）赋值；`_run_pyav`（文档标注的打包环境主路径）、`_run_pyav_to_topos`、`_run_topos` 均为进程内编码、从不设置 `_process`。调用方（scene_cut_dialog.py:1837、timeline_panel.py:1357）在场景剪切探测期间期望让出 CPU，实际主路径照常满负荷编码——前台交互卡顿/掉帧，功能承诺落空（返回的 paused=0 也无告警）。
  - 建议: 进程内路径实现协作式暂停（在帧循环检查一个 `threading.Event` 等待恢复），或至少在 pause() 未生效时记 warning 让调用方知情。
  - 置信度: 高

- [AUD-35610] [Major] 同源重复入队时静默丢弃后来调用方的全部回调
  - 位置: src/shared/media/proxy_manager.py:229-235
  - 证据:
    ```python
            # 检查是否已有任务在处理此源文件
            if source_path in self._source_to_task:
                existing_task_id = self._source_to_task[source_path]
                existing_task = self._tasks.get(existing_task_id)
                if existing_task and existing_task.status in ("queued", "generating"):
                    logger.info(f"Task already exists for {source_path}: {existing_task_id}")
                    return existing_task_id
    ```
  - 影响: 第二个调用方传入的 on_complete/on_error 被静默丢弃且无任何合并机制；上层 DiskProxyService 的 GENERATING 分支也只把 on_progress 追加到服务级列表（disk_proxy_service.py:362-366），第二个调用方的完成通知永不触发——UI 素材项永远停在"生成中"或收不到结果。
  - 建议: 为已有任务追加回调列表（ProxyTask 回调改为 list），任务结束时逐个调用；或至少记录 warning 并在文档中标明返回 existing_task_id 时不回调。
  - 置信度: 高

- [AUD-35611] [Suggestion] 完成任务永不清理：`_tasks` 字典无界增长
  - 位置: src/shared/media/proxy_manager.py:251、634-635
  - 证据:
    ```python
            self._tasks[task_id] = task
            self._source_to_task[source_path] = task_id
    ...
            # 清理映射
            self._source_to_task.pop(task.source_path, None)
    ```
  - 影响: 终态任务只从 `_source_to_task` 移除，`_tasks` 中的 ProxyTask（含 result/progress/generator 引用）长会话持续累积，缓慢内存泄漏且 `get_stats`/`pause_all_generation` 遍历成本增长。
  - 建议: 任务终态后延迟从 `_tasks` 移除，或提供 prune 接口按 completed_at 清理。
  - 置信度: 高

- [AUD-35612] [Suggestion] `get_valid_proxy_path` 在锁外读取 `_proxy_cache`，校验与取值之间存在失效窗口
  - 位置: src/shared/media/proxy_manager.py:391-393
  - 证据:
    ```python
        if self.is_proxy_valid(source_path):
            return self._proxy_cache.get(source_path)
        return None
    ```
  - 影响: `is_proxy_valid` 释放锁后另一线程可 invalidate/pop 缓存，随后 `.get` 返回 None（良性）或返回刚被判定过期但尚未 pop 的路径（不一致）；与全文件其余方法的加锁口径不统一。
  - 建议: 整体放入 `with self._lock:`，一次取值后校验。
  - 置信度: 高（事实层面）

- [AUD-35613] [Suggestion] 持管理器锁执行磁盘 IO：is_proxy_valid/get_stats/cleanup_* 在 `self._lock` 内 stat/remove 文件
  - 位置: src/shared/media/proxy_manager.py:361-374、443-449、484-494
  - 证据:
    ```python
        with self._lock:
            ...
                if not os.path.isfile(proxy_path):
                    self._invalidate_proxy_cache(source_path)
                    return False
                ...
                        current_mtime, current_size = get_source_file_info(source_path)
    ```
  - 影响: UI 线程的代理状态查询（经 DiskProxyService 持服务锁再取管理器锁）在慢速盘/网络卷上会被 stat/getsize/remove 阻塞，放大 AUD-35608 的锁持有时间；cleanup 批量删除期间所有查询停摆。
  - 建议: 锁内只做字典操作，文件系统检查移到锁外（快照后验证）。
  - 置信度: 高（事实层面）

- [AUD-35614] [Suggestion] `ProxyStats.total_assets`/`cache_hit_rate` 恒为 0——统计字段未实现
  - 位置: src/shared/media/proxy_manager.py:132-138、430-452
  - 证据:
    ```python
        with self._lock:
            stats = ProxyStats()
            # 计算任务统计
            for task in self._tasks.values():
                if task.status == "queued":
    ```
  - 影响: `total_assets` 与 `cache_hit_rate` 从未赋值，UI/诊断拿到恒 0 的覆盖率与命中率，统计面板失真。
  - 建议: 实现两个字段（total = len(proxy_cache)+活跃任务；命中率需记录命中/未命中计数）或删除字段避免误导。
  - 置信度: 高

- [AUD-35615] [Suggestion] `asset: Any` 弱类型并直接假设 `.path/.asset_id/.with_proxy_path` 接口
  - 位置: src/shared/media/proxy_manager.py:522-547、549-572
  - 证据:
    ```python
        def update_proxy_for_asset(
            self,
            asset: Any,
            proxy_path: str,
        ) -> Any:
            ...
                self._proxy_cache[asset.path] = proxy_path
    ```
  - 影响: 类型注解维度缺失：传入不满足协议的对象要到运行时才在锁内抛 AttributeError，IDE/静态检查无从校验。
  - 建议: 定义 Asset 协议（Protocol）或引用具体素材类型作注解。
  - 置信度: 高

### 通过项
- 取消路径：cancel_task/cancel_all_tasks 状态检查与映射清理正确，RLock 重入安全；_run_task 的延迟启动等待循环每轮检查 cancelled，不遗漏
- 缓存失效一致性：mtime+size 双因子校验，is_proxy_valid/cleanup_expired_proxies/update 后记录版本，口径一致
- 信号防递归：模块无 Qt 信号，进度回调经 progress_wrapper 加锁写 task.progress，无递归风险
- GPU 资源生命周期：不涉及 GL 对象

---

## src/shared/media/proxy_manifest.py (行数: 334)

### 发现

- [AUD-35616] [Major] `_source_index` 每源仅一条索引：多策略/版本条目被挤出，invalidate/remove 漏处理孤儿条目，重载后陈旧条目可重新可见
  - 位置: src/shared/media/proxy_manifest.py:261-266、268-286（对照 204-210 的 load 重建）
  - 证据:
    ```python
    def upsert_entry(self, entry: ProxyManifestEntry) -> None:
        """插入或更新 entry。"""
        with self._lock:
            key_hash = entry.key.to_hash()
            self._entries[key_hash] = entry
            self._source_index[entry.key.source_path] = key_hash
    ...
    def invalidate_by_source(self, source_path: str, reason: str = "") -> None:
        """按 source 失效所有相关 entry（源文件被修改时调用）。"""
        with self._lock:
            key_hash = self._source_index.get(source_path)
    ```
  - 影响: key 含 codec_policy/resolution_policy/app_version，同一 source 可产生多个 entry，但索引每源只保留最后一次 upsert 的 key_hash：`invalidate_by_source`（docstring 称"失效所有相关 entry"）与 `remove_entry` 只处理当前索引指向的一条，旧策略条目永久滞留 `_entries` 并被 save 持久化；`load()` 按 JSON 顺序重建索引（同源最后一条胜出），磁盘上残留的旧策略 READY 条目在重载后可能成为该源的可见条目，`has_ready_proxy` 返回过期结果（如指向已删除的旧格式代理文件）。唯一消费者 asset_proxy_manager 当前标注 DORMANT（无生产入口），故生产影响尚未激活。
  - 建议: `_source_index` 改为 `Dict[str, Set[str]]`，invalidate/remove 遍历全部 key_hash；或按 (source, policy) 显式查询。
  - 置信度: 中

- [AUD-35617] [Suggestion] get_entry/upsert_entry 直接暴露内部可变 entry，绕过"所有查询/修改加锁"的封装承诺
  - 位置: src/shared/media/proxy_manifest.py:243-250、261-265
  - 证据:
    ```python
    def get_entry(self, source_path: str) -> Optional[ProxyManifestEntry]:
        """按 source_path 查询 entry（<1ms，纯内存）。"""
        self.ensure_loaded()
        with self._lock:
            key_hash = self._source_index.get(source_path)
            if key_hash is None:
                return None
            return self._entries.get(key_hash)
    ```
  - 影响: ProxyManifestEntry 为可变 dataclass；调用方持有引用后可从任意线程无锁改 status/progress，save() 序列化时可能读到写一半的状态（字段间不一致），类 docstring 的线程安全声明名不副实。
  - 建议: 返回副本（dataclasses.replace/asdict 重建），或冻结 entry 用"替换整个对象"语义更新。
  - 置信度: 中

- [AUD-35618] [Suggestion] `ensure_loaded` 检查-加载竞态：`_loaded` 在锁外读取
  - 位置: src/shared/media/proxy_manifest.py:234-237
  - 证据:
    ```python
    def ensure_loaded(self) -> None:
        """懒加载（首次访问时自动 load）。"""
        if not self._loaded:
            self.load()
    ```
  - 影响: 两个线程首次并发查询会各自执行一次全量 load（含磁盘读与清空重建），后完成者覆盖前者——load 幂等故结果正确，但浪费 IO 且中间态下另一线程的 upsert 可能被随后的 load 清掉。
  - 建议: `ensure_loaded` 全程持锁（double-checked locking）。
  - 置信度: 高（事实层面）

- [AUD-35619] [Suggestion] `from_dict` 对 `proxy_path`/`error` 未做类型规整——外部 JSON 数据验证缺口
  - 位置: src/shared/media/proxy_manifest.py:149、157
  - 证据:
    ```python
        return cls(
            key=key,
            status=status,
            proxy_path=d.get("proxy_path"),  # type: ignore[arg-type]
            ...
            error=d.get("error"),  # type: ignore[arg-type]
    ```
  - 影响: 其余字段均经 int()/str()/float() 规整，唯独这两个原样透传：损坏/手改的 manifest.json（如 proxy_path 为 dict/list/int）会把非法类型带入内存，下游 `os.path.isfile(proxy_path)` 抛 TypeError。违背"外部数据须验证"铁律，影响概率低。
  - 建议: `proxy_path`/`error` 分别做 `Optional[str]` 规整（非 str/None 置 None 并告警）。
  - 置信度: 高（事实层面）/影响低

- [AUD-35620] [Suggestion] `save()` 吞掉全部异常仅 warning，持久化失败对调用方不可见
  - 位置: src/shared/media/proxy_manifest.py:218-232
  - 证据:
    ```python
                tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp_path.replace(self._manifest_path)
            except Exception as e:
                logger.warning(f"Failed to save proxy manifest: {e}")
    ```
  - 影响: 磁盘满/权限错误时静默丢失整份清单状态，进程退出后所有 GENERATING/READY 记录回到旧版本，代理重复生成；调用方（asset_proxy_manager 的 flush）无从感知失败。
  - 建议: 至少区分 OSError 类型向上返回 bool 或抛出领域异常；残留的 .json.tmp 也应清理。
  - 置信度: 高（事实层面）

- [AUD-35621] [Suggestion] `entry_count`/`stats()` 不走 `ensure_loaded`：加载前查询得到空统计
  - 位置: src/shared/media/proxy_manifest.py:292-304
  - 证据:
    ```python
        @property
        def entry_count(self) -> int:
            with self._lock:
                return len(self._entries)

        def stats(self) -> Dict[str, int]:
            with self._lock:
    ```
  - 影响: 与 get_entry/has_ready_proxy 的懒加载行为不一致：构造后直接调 stats()/entry_count（asset_proxy_manager 初始化日志即调 entry_count，好在它先显式 load）返回 0，诊断口径错误。
  - 建议: 两个方法开头补 `self.ensure_loaded()`。
  - 置信度: 高

### 通过项
- 持久化原子性：save 采用 tmp 文件 + `replace()` 原子替换，避免半写损坏
- load 健壮性：损坏条目逐条跳过（logger.debug）、整体损坏降级空 manifest 不抛出；ProxyStatus 反序列化 ValueError 回退 MISSING
- key 设计：source mtime_ns + size + 策略 + app_version 参与失效判断，与 proxy.py 的 generate_proxy_key 口径一致
- 信号防递归/GPU 生命周期/GLSL：纯数据持久化模块，不涉及

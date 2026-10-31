# Batch 167 审查报告：src/features/edit/core（播放控制器 + 工程 IO）

- 批次: batch-167 [P1]，编号区间 AUD-16700–AUD-16799
- 文件: `src/features/edit/core/edit_playback_controller.py` (1,238 行)、`src/features/edit/core/edit_project_io.py` (1,601 行)
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 关键交叉验证源: `src/features/edit/ui/timeline_panel.py`（JKL/Timer 消费方）、`src/app/controllers/file_controller.py`（load_project 调用方）、`src/shared/project/topos_project_io.py`（load 的异常边界）、`src/features/edit/core/generator_transaction_guard.py`、`src/features/edit/core/timeline_v2.py`（from_dict 严格性）、`src/features/edit/core/playback_debug.py`（trace 门控）

---

## src/features/edit/core/edit_playback_controller.py (行数: 1,238)

### 发现

- [AUD-16700] [Major] JKL 首次按 L/J 直接进入 2x 倍速（档位 off-by-one），1x 档永远无法通过 J/K/L 触达
  - 位置: src/features/edit/core/edit_playback_controller.py:738-750（J 侧同构 754-772）
  - 证据:
    ```python
    # 重置反向加速计数
    if self._last_jkl_key != 'L':
        self._jkl_speed_index = 0

    # 加速档位递增
    self._jkl_speed_index = min(self._jkl_speed_index + 1, len(PLAYBACK_SPEEDS) - 1)
    self._last_jkl_key = 'L'

    # 获取速度
    speed = PLAYBACK_SPEEDS[self._jkl_speed_index]
    ```
    实测模拟：初始 index=0/last=None，第 1 次按 L → index=1 → speed=2.0x；第 2 次 → 4x；第 3 次 → 8x。类 docstring（第 39 行 `jkl_forward_press()  # L: 1x, 2x, 4x, 8x`）与方法 docstring（"连按加速：1x → 2x → 4x → 8x"）均声明首按应为 1x。消费方 `timeline_panel._on_l_pressed()` 直接调用本方法，无前置 play(1x)；空闲重置 `jkl_reset()` 将 index 归 0 后下一按仍是 2x。
  - 影响: 标准 JKL 操作语义错误——用户按 L 期望 1x 正放，实际立即 2x；按 J 期望 -1x 倒放，实际 -2x。剪辑审片时的精确慢速往返不可达。
  - 建议: 先用当前档位取速度再递增（首按命中 index 0），或递增改为 `min(index, len-1)` 并在取速后 `index += 1`；为 `jkl_backward_press` 同步修正，并补首按=1x 的回归测试。
  - 置信度: 高

- [AUD-16701] [Major] `except (ValueError, Exception)` 宽泛捕获掩盖非领域异常（两处），违反"不得宽泛 except 掩盖领域错误"铁律
  - 位置: src/features/edit/core/edit_playback_controller.py:1031、1065
  - 证据:
    ```python
    try:
        tc_type = Timecode.auto_detect_type(self._fps_rational)
        tc = Timecode.from_frame_number(max(0, frame), self._fps_rational, tc_type)
        return tc.to_string()
    except (ValueError, Exception):
        # 回退到简单整除（保持向后兼容，不应发生在正常 fps 下）
    ```
    以及 `timecode_to_frame` 中同样的 `except (ValueError, Exception):`（1065 行）。元组中的 `Exception` 使 `ValueError` 冗余，等价于裸 `except Exception`。
  - 影响: Timecode 模块的 TypeError/AttributeError/KeyError 等编程错误被静默吞掉并落入粗糙的整除回退（`int(round(self._fps))` 对 NTSC 帧率产生错误时间码），掩蔽真实缺陷且无任何日志。
  - 建议: 改为 `except ValueError:`（领域错误）并记录 `logger.debug`；其余异常自然抛出。两处同步修改。
  - 置信度: 高

- [AUD-16702] [Suggestion] `get_progress()` 在单帧时间线（duration==1）上除零
  - 位置: src/features/edit/core/edit_playback_controller.py:1084-1086
  - 证据:
    ```python
    if self._timeline_duration <= 0:
        return 0.0
    return self._state.current_frame / (self._timeline_duration - 1)
    ```
  - 影响: duration==1 时分母为 0 → ZeroDivisionError。当前全库未发现调用方（潜伏缺陷），但这是公开 API，单帧（静帧）时间线是合法状态。
  - 建议: 分母取 `max(1, self._timeline_duration - 1)` 或 duration==1 时直接返回 0.0/1.0。
  - 置信度: 高（数学路径确定；触发依赖未来调用方）

- [AUD-16703] [Suggestion] `tick()` 帧号未变化时仍发射 `frame_changed`
  - 位置: src/features/edit/core/edit_playback_controller.py:941-953
  - 证据:
    ```python
    else:  # EXACT mode
        # 精确模式：不跳帧，逐帧推进
        if delta > 0:
            ...
        elif delta < 0:
            ...
    # 发射帧变化信号
    self.frame_changed.emit(self._state.current_frame)
    ```
    EXACT 模式 delta==0 时两个分支都不执行（帧号不变），仍无条件 emit。
  - 影响: EXACT 播放且 tick 间隔小于帧时长（如 60fps 下 16ms tick vs 16.67ms 帧长）时周期性发射重复帧；下游 `_on_controller_frame_changed` / `frame_ready` 消费方做冗余时间码刷新与 trace。
  - 建议: emit 前比较 `if self._state.current_frame != before_frame:`。
  - 置信度: 高（代码确定；影响量级小）

- [AUD-16704] [Suggestion] `set_playback_range` 不校验 start/end 合法性
  - 位置: src/features/edit/core/edit_playback_controller.py:372-388
  - 证据:
    ```python
    self._playback_range_start = start
    self._playback_range_end = end
    if start is not None and end is not None:
        logger.debug(f"Playback range set: {start} - {end}")
    ```
  - 影响: start > end、负值、或超出 `timeline_duration` 的入参直接生效；`tick()` 中 `max(range_start, min(target, range_end))` 语义反转，配合 `target >= range_end` 判定会在首个 tick 立即触发 end_reached 或异常循环跳转，行为难以预期（无崩溃）。
  - 建议: 入参 clamp 到 [0, duration-1] 并在 start > end 时交换或拒绝（warning + 忽略）。
  - 置信度: 高（缺校验确定；实际触发依赖 UI 传入异常值）

- [AUD-16705] [Suggestion] `set_fps` 未拦截 NaN/Inf，进入有理数换算后抛未捕获异常
  - 位置: src/features/edit/core/edit_playback_controller.py:311-325（及 95-100）
  - 证据:
    ```python
    if fps <= 0:
        logger.warning(f"Invalid fps: {fps}, using default {DEFAULT_FPS}")
        fps = DEFAULT_FPS
    ...
    self._fps_rational = _float_to_rational_fps(fps)
    ```
    NaN 不满足 `<= 0`（实测 `round(float('nan'))` 抛 `ValueError: cannot convert float NaN to integer`），`_float_to_rational_fps` 末行 `return Rational(int(round(fps)), 1)` 对 NaN 抛 ValueError、对 Inf 抛 OverflowError。
  - 影响: 媒体元数据解析出 NaN 帧率时 `set_fps` 崩溃而非回退默认值。
  - 建议: 校验改为 `if not (fps > 0) or math.isinf(fps):`（NaN 参与 `> 0` 比较为 False，可一并拦截）。
  - 置信度: 高（路径实测确定；NaN 来源少见）

- [AUD-16706] [Suggestion] 死变量与未使用导入（质量清理）
  - 位置: src/features/edit/core/edit_playback_controller.py:1226-1230、61
  - 证据:
    ```python
    fps = self._controller.fps
    speed = self._controller.playback_speed

    # 基础间隔 = 1000ms / fps（每帧一次 tick）
    base_interval = int(1000.0 / fps)
    ```
    `speed` 读取后从未使用；文件头 `from typing import Optional, Callable` 中 `Callable` 全文未使用。
  - 影响: 误导读者以为间隔随速度调整；类型导入冗余。
  - 建议: 删除 `speed` 行与 `Callable` 导入，或在注释中说明速度已被 reanchor 机制取代的历史。
  - 置信度: 高

### 通过项
- 线程安全: 全部 API 约定主线程使用（QObject/QTimer），`tick()` 为纯计算，无跨线程共享可变状态；未发现 UI 线程外调用。
- GPU 资源生命周期: 本文件不持有任何 ModernGL/OpenGL 资源，无 GPU 泄漏面。
- GLSL·HDR 正确性: 无着色器/HDR 数据路径（N/A）。
- 信号防递归: play/pause/seek/step/reanchor 等 setter 不回调自身信号；下游消费方（timeline_panel `_on_controller_frame_changed` 仅更新时间码、playhead 由 `_on_timer_frame_ready` 独占）无回环路径。
- 状态提交一致性: 播放/暂停/停止/seek/循环跳转时单调时钟基准（start_time/elapsed_at_pause）重置逻辑核对一致；暂停恢复基于 current_frame 重建基准，未发现基准漂移。
- 预览/精确模式丢帧策略: DROP_THRESHOLD 与 MAX_DROPS_PER_TICK 限制生效，反向丢帧对称处理。
- fps Rational 校验: `set_fps_rational` 对 num/den<=0 有拦截；NTSC 反查表逻辑正确（Rational 属性名 num/den 与 shared 实现一致）。
- `reanchor_to_frame` 对 range/duration 有 clamp；`PlaybackControllerTimer` 使用 PreciseTimer 且由 UI 侧 state_changed 负责 start/stop，配对完整。
- playback_trace 均经环境变量门控（TOPOS_EDIT_PLAYBACK_TRACE），默认关闭，无每帧开销。
- 类型注解: 公开方法均有完整签名与返回类型注解。

---

## src/features/edit/core/edit_project_io.py (行数: 1,601)

### 发现

- [AUD-16710] [Critical] 加载外部 .topos 包后的反序列化无异常边界，损坏/手改包可经主"打开工程"路径使整个应用崩溃
  - 位置: src/features/edit/core/edit_project_io.py:1124-1131（另见 305、329-338、381）
  - 证据:
    ```python
    timeline_sequences = None
    if full_data and "timeline_sequences" in full_data:
        timeline_sequences_data = full_data.get("timeline_sequences", {})
        timeline_sequences = {
            seq_id: TimelineSequence.from_dict(seq_data)
            for seq_id, seq_data in timeline_sequences_data.items()
        }
    ```
    `ToposProjectIO.load` 内部虽有 try/except 返回 (None, None)，但其 full_data 是原始 JSON；本文件随后在无任何 try 包裹下执行强转换：`TimelineSequence.from_dict` 链上 `TimelineMarker.from_dict` 使用 `data["frame"]`（缺键即 KeyError），`MediaAsset(... rating=int(asset_data.get("rating", 0)) ...)`（第 305 行，字符串值即 ValueError），`ColorStackData.from_dict`（第 381 行）同理。`load_project` 全函数无 try/except。主打开路径 `file_controller.py:4056`（`open_project()` 菜单动作 → `_load_edit_project`，已核实函数体 4017-4094 无 try 包裹）异常直接穿出到 Qt 槽。
  - 影响: 用户打开一个 JSON 载荷被手改/损坏的 .topos 文件 → 未处理异常从菜单槽抛出 → PyQt6 打印回溯后 abort，应用崩溃退出（同文件另两个调用方 `_load_standalone_project`、save_router 有 try，唯独主 Edit 打开路径没有）。违反"外部数据须验证"铁律。
  - 建议: 在 `load_project` 内将"迁移+转换+from_dict"段包 try/except（记录 exception 并 return None，与 ToposProjectIO.load 的契约对齐）；转换前对 rating/keywords/frame 等字段做类型收敛。
  - 置信度: 中（异常路径与调用方缺失保护均已核实；触发需包内数据实际畸形）

- [AUD-16711] [Major] `_cleanup_old_backups` 用文件名前缀匹配备份，可误删其它工程的备份文件
  - 位置: src/features/edit/core/edit_project_io.py:1432-1451
  - 证据:
    ```python
    backup_pattern = filepath.name + BACKUP_EXTENSION
    parent_dir = filepath.parent
    backups = sorted(
        [
            f
            for f in parent_dir.iterdir()
            if f.name.startswith(filepath.stem) and f.suffix.endswith(BACKUP_EXTENSION)
        ],
        key=lambda f: f.stat().st_mtime,
    )
    ```
    `backup_pattern` 计算后从未使用（死变量）；过滤条件是 `startswith(filepath.stem)`——工程 `demo.topos`（stem "demo"）会把 `demo2.topos.bak`、`demo_reel.topos.bak` 等其它工程的备份一并计入并按 mtime 淘汰删除（目录下前缀匹配且 .bak 结尾的文件数超过 max_backup_count=3 时触发 unlink）。
  - 影响: 同目录存在多个同前缀命名的工程且备份总数超过阈值时，保存操作会删掉别的工程的 `.bak`，破坏其回滚能力。
  - 建议: 删除死变量；匹配改为精确名 `f.name == filepath.name + BACKUP_EXTENSION`（当前 `_create_backup` 本就只写单个固定名备份，多备份轮换逻辑实为死代码，可整体简化）；`f.stat()` 包 OSError 保护。
  - 置信度: 高

- [AUD-16712] [Major] 保存时忽略 `_create_backup` 失败结果，备份失败仍继续覆盖原文件
  - 位置: src/features/edit/core/edit_project_io.py:806-808（对照 1397-1420）
  - 证据:
    ```python
    # 备份上一版（如果配置启用且文件存在）
    if create_backup and self.config.backup_on_save and filepath.exists():
        self._create_backup(filepath)
    ```
    `_create_backup` 声明 `-> bool` 并在 copy2 失败时 `logger.error(...); return False`，但调用方不接收返回值。
  - 影响: 磁盘满/权限异常导致备份失败后，保存流程仍用新数据覆盖唯一原文件；若后续 ZIP 写入再失败或被中断，用户失去上一版且无备份可恢复（restore_from_backup 亦无从谈起）。
  - 建议: 备份失败时中止保存（return False 并向用户报错），或至少提供"备份失败仍继续"的显式选项；与 save_project 的错误契约（返回 bool）保持一致。
  - 置信度: 高（代码忽略返回值确定；后果依赖双重故障场景）

- [AUD-16713] [Major] `_collect_color_management_settings` 宽泛 `except Exception` 静默返回空 dict 且无日志
  - 位置: src/features/edit/core/edit_project_io.py:106-114
  - 证据:
    ```python
    def _collect_color_management_settings() -> dict:
        """收集用户全局色彩管理偏好（见 color_space_descriptor）。"""
        try:
            from src.shared.color.color_space_descriptor import (
                collect_color_management_settings,
            )
            return collect_color_management_settings()
        except Exception:
            return {}
    ```
  - 影响: 收集逻辑的任何运行期错误（非 ImportError）都被吞掉，工程照常落盘但 `settings.color_management` 为空——下次打开工程回退用户全局色彩偏好，P0-COLOR-04 的"色彩管理随工程落盘"承诺静默失效，且无任何日志可诊断。违反"不得宽泛 except 掩盖领域错误"铁律。
  - 建议: 拆分为 `except ImportError`（容许模块缺失，debug 日志）与记录 `logger.exception` 的兜底；或至少在 except 中 `logger.warning("color management settings dropped", exc_info=True)`。
  - 置信度: 高

- [AUD-16714] [Major] 加载侧色彩管理覆盖注入失败被 `except Exception: pass` 完全吞掉
  - 位置: src/features/edit/core/edit_project_io.py:271-279
  - 证据:
    ```python
    try:
        from src.shared.color.color_space_descriptor import (
            set_project_color_overrides,
        )
        set_project_color_overrides(
            (getattr(topos_project, "settings", None) or {}).get(
                "color_management"))
    except Exception:
        pass
    ```
  - 影响: 与 AUD-16713 对称的加载侧问题——注入失败时工程以错误的（全局默认）色彩管理设定渲染，画面与原作者所见不一致且完全无感知、无日志；注释声称"两条加载路径一处注入全覆盖"，静默失败直接击穿该保证。
  - 建议: 至少 `except Exception: logger.exception(...)`；对可恢复的 ImportError 单独降级处理。
  - 置信度: 高

- [AUD-16715] [Suggestion] 手动保存/另存为在 UI 线程同步执行完整 ZIP 写，大工程可致长卡顿
  - 位置: src/features/edit/core/edit_project_io.py:919-941（对照 960-988、1304-1329）
  - 证据:
    ```python
    success = ToposProjectIO.save(
        topos_project,
        str(filepath),
        topos_config,
        full_assets_data=full_assets_data,
        ...
    )
    ```
    后台化仅覆盖 autosave（`_AsyncSaveWorker` + 私有 QThreadPool）；`save_project`/`save_project_as`/`quick_save` 均在调用线程（UI）同步执行含 compress_level=6 的 ZIP 打包与大 JSON 序列化。
  - 影响: 4K/多素材工程保存期间 UI 冻结（秒级），违背项目"实时性能基准"的使用体验目标。
  - 建议: 将手动保存复用 `_AsyncSaveWorker` 通道（主线程先快照，完成后 `project_saved` 经 queued 信号回 UI），或在保存前给出进度反馈。
  - 置信度: 高（结构确定；卡顿时长依赖工程规模）

- [AUD-16716] [Suggestion] 后台保存线程写回 `self._last_generator_preflight` 共享诊断状态
  - 位置: src/features/edit/core/edit_project_io.py:783、793（经 92-99 的 worker 调用）
  - 证据:
    ```python
    initial_check = preflight_orphan_check(project, sequences)
    self._last_generator_preflight = initial_check
    ```
    autosave worker 线程执行 `save_project`（autosave=True）时同样走到这两行，与 UI 线程手动保存交错写入同一实例属性。
  - 影响: 诊断值可能被后台保存覆盖（当前全库未发现读取方，属潜伏风险；GIL 下引用赋值原子，无内存安全问题）。
  - 建议: 将 preflight 结果作为局部值/返回值传递而非实例状态，或仅在同线程（非 autosave）路径写入。
  - 置信度: 高（跨线程写确定；当前无实际读方故降为 Suggestion）

- [AUD-16717] [Suggestion] 扩展名纠正使用 `with_suffix`（替换而非追加），带点号的自定义文件名被截断
  - 位置: src/features/edit/core/edit_project_io.py:801-804
  - 证据:
    ```python
    is_recovery_path = autosave and filepath.name.lower().endswith(AUTOSAVE_EXTENSION)
    if filepath.suffix != EDIT_PROJECT_EXTENSION and not is_recovery_path:
        filepath = filepath.with_suffix(EDIT_PROJECT_EXTENSION)
    ```
    实测 `Path('cut.v3').with_suffix('.topos')` → `cut.topos`，用户输入的版本段 `.v3` 被替换丢失。
  - 影响: 另存为 `cut.v3` 时实际写成 `cut.topos`，与用户选择路径不符（`_current_filepath`/`project_saved` 记录的是改后路径，无提示）。
  - 建议: 对不含 `.topos` 的名字用追加（`filepath.with_name(filepath.name + EDIT_PROJECT_EXTENSION)`）而非替换，或仅当 suffix 为其它已知工程后缀时替换。
  - 置信度: 高（行为实测；触发依赖用户使用点号命名）

- [AUD-16718] [Suggestion] `cleanup()` 的 `waitForDone()` 无超时，慢速/挂起 IO 可无限阻塞退出
  - 位置: src/features/edit/core/edit_project_io.py:1587-1589
  - 证据:
    ```python
    if self._autosave_workers:
        self._autosave_pool.waitForDone()
        self._autosave_workers.clear()
    ```
  - 影响: 恢复写线程若卡在网络盘/慢速介质上（os.replace 前 IO 挂起），应用退出被永久阻塞；注释本身承认"等待超时"是该池设计动机之一，但此处未设上限。
  - 建议: 使用 `waitForDone(msecs)`（如 5000ms）有界等待，超时则 `clear()` 并放弃删除恢复文件（避免 AUD 注释中描述的"晚完成 worker 复活 .autosave"假恢复提示）。
  - 置信度: 中（挂起场景为推测；代码路径确定）

- [AUD-16719] [Suggestion] 回调类型注解使用内建 `callable`（非类型），9 处
  - 位置: src/features/edit/core/edit_project_io.py:587-597、654-663
  - 证据:
    ```python
    self._get_ui_state_callback: Optional[callable] = None
    ...
    def set_callbacks(
        self,
        get_project: Optional[callable] = None,
        get_ui_state: Optional[callable] = None,
    ```
  - 影响: `callable` 是内建函数而非类型，静态检查（mypy/pyright）无法校验回调签名，属类型注解缺失维度；与项目"完整类型注解"规范不符。
  - 建议: 改为 `Callable[[], EditProject | None]` 等精确签名（`from collections.abc import Callable`）。
  - 置信度: 高

### 通过项
- 线程安全（核心自动保存路径）: 越线程前在 UI 线程 `copy.deepcopy` 全部可变状态（project/ui_state/timelines/AI payloads）；单线程私有 QThreadPool 隔离全局池；完成经 queued 信号 `_on_async_autosave_finished` 回主线程；`_shutting_down` 门阻止关机后复活恢复文件/重排保存。
- 自动保存合并: `_autosave_in_progress`/`_autosave_pending` 正确合并并发触发（完成后 singleShot(0) 补一轮）。
- 异常与取消路径（保存侧）: `_AsyncSaveWorker.run` 捕获异常并经信号回传错误字符串；autosave 快照段 try/except 完整（含 AI/教学动画回调调用）。
- FX-H03 保存前一致性: timeline map 不完整、clips 引用缺失资产、generator 缺 bin 条目三条阻断路径齐全，先于任何备份/写盘执行。
- 状态提交一致性: `settings.root_bin_id` 双向持久化；加载侧重复 Root 合并/重挂父子逻辑正确（`ProjectBin.with_parent` 存在）；`comp_clips`/`smart_bins` 空 dict 显式写出防复活；autosave 不改 `_current_*`、成功手动保存后清理 `.autosave`。
- 备份/恢复/清理文件操作: `_create_backup`/`restore_from_backup`/`discard_autosave` 均有异常捕获与日志（OSError/Exception + logger）。
- 恢复文件判定: `get_autosave_filepath` 的 mtime 新旧判定与陈旧文件即时清理逻辑正确；`get_autosave_filepath` 对已是 .autosave 的入参正确剥后缀。
- 输入验证（路径层）: `load_project` 检查存在性与 `is_topos_file`；旧格式显式拒绝并日志。
- 信号防递归: 本类信号（project_saved/autosave_*）仅作通知发射，无自触发回路。
- GPU 资源生命周期 / GLSL·HDR: 本文件无 GPU 资源与着色器路径（N/A）。

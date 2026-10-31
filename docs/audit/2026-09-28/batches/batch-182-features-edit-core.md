# 批次报告：batch-182 — src/features/edit/core（controller/selection/serialization/ducker/stem_export）

编号区间：AUD-18200–AUD-18299
审查日期：2026-09-28
统计：Critical 1 / Major 11 / Suggestion 10

---

## src/features/edit/core/scene_cut_controller.py (行数: 654)

### 发现

- [AUD-18200] [Major] "Add Cuts to Media Pool" 绕过命令/undo 系统，直接改写并发布项目
  - 位置: src/features/edit/core/scene_cut_controller.py:347
  - 证据:
    ```python
        # Add to project
        result = add_subclips_to_project(
            project=project,
            analysis=analysis,
            specs=specs,
            parent_asset=parent_asset,
            duplicate_strategy=duplicate_strategy,
        )

        # Update project
        if self._project_setter:
            self._project_setter(result.project)
    ```
  - 影响: 模块设计原则（文件头 "All mutations through project commands"）被违反；`src/features/edit/core/commands.py` 存在完整命令体系（全库 grep 无 AddSubclips/add_subclips 命令包装），该路径新增素材/改 bin 不入 undo 栈，用户无法撤销，且与其它项目变更的 revision/持久化口径不一致。
  - 建议: 将 add_subclips_to_project 包装为 project command 并经命令栈提交；controller 只发命令、不直接 set_project。
  - 置信度: 高

- [AUD-18201] [Major] `_on_add_to_media_pool` 无异常保护，`add_subclips_to_project` 的 ValueError 会逃逸进 Qt 槽
  - 位置: src/features/edit/core/scene_cut_controller.py:543
  - 证据:
    ```python
        result = self.add_cuts_to_media_pool(analysis, asset)

        if result and result.added_count > 0:
    ```
    （被调方 `scene_cut/subclips.py:470-473`：`if parent_asset.asset_id not in project.assets: raise ValueError(...)`；连接处本文件 493-497 经 lambda 直连 `dialog.cuts_added_to_pool`）
  - 影响: 对话框打开期间切换工程后点击 Add Cuts（parent asset 不在新工程中）→ ValueError 在 PyQt 槽内未处理，PyQt6 默认走 qFatal/异常钩子，可能直接中止应用；即使有全局钩子也仅打印栈，无用户反馈、无 detection_error 发射。
  - 建议: 在 `_on_add_to_media_pool`/`add_cuts_to_media_pool` 捕获领域异常，转 `detection_error` 或对话框内提示，而非放任逃逸。
  - 置信度: 中

- [AUD-18202] [Suggestion] `_task_registry` 赋值后从未使用（死代码）
  - 位置: src/features/edit/core/scene_cut_controller.py:149
  - 证据:
    ```python
        # Task registry for concurrent detection management
        self._task_registry = get_active_tasks_registry()
    ```
  - 影响: 并发检测管理实际由 dialog/worker 自行处理；该字段误导读者以为 controller 做去重/并发控制，实际无任何效果。
  - 建议: 删除该字段与相应 import，或在 start_detection_* 中真正用于同 key 任务去重。
  - 置信度: 高

- [AUD-18203] [Suggestion] `_session_analyses` 会话内无上限增长
  - 位置: src/features/edit/core/scene_cut_controller.py:145
  - 证据:
    ```python
        self._session_analyses: dict[tuple[str, str, str, str], SceneCutAnalysis] = {}
    ```
    （写入点：519 `self._session_analyses[self._analysis_key(context)] = analysis`、555）
  - 影响: 每个资产/剪辑/范围组合各缓存一份完整 SceneCutAnalysis（含全部候选帧数据），长会话批量检测大量素材时内存持续增长，仅在切工程时清空。
  - 建议: 加 LRU 上限（如最近 8-16 个 key）或按指纹弱引用缓存。
  - 置信度: 中

### 通过项
- 线程安全：controller 仅在 UI 线程活动，无跨线程共享可变状态
- GPU 资源生命周期 / GLSL·HDR：本文件不涉及 OpenGL 与浮点着色
- 信号防递归：dialog.finished→_on_dialog_closed 同步直连，`_show_dialog_for_context` 先 close 旧对话框（finished 同步发出、正确以旧 key 存档）再建新对话框，顺序正确；close_active_dialog 二次置 None 无害
- 输入验证：asset 存在性/media_type/clip 源范围（P1-11 空范围拒绝）均有守卫；`with_range`/`with_all_decisions` 均存在（已核对 models.py）
- 项目切换会话缓存清理（_get_project 的 project_id 比对）逻辑正确

---

## src/features/edit/core/selection.py (行数: 517)

### 发现

- [AUD-18204] [Major] `TimelineSelection.__eq__` 与 dataclass 自动生成的 `__hash__` 违反相等/哈希契约
  - 位置: src/features/edit/core/selection.py:411
  - 证据:
    ```python
        def __eq__(self, other: object) -> bool:
            if not isinstance(other, TimelineSelection):
                return NotImplemented
            ...
            # Compare item contents (order-independent for items)
            self_item_ids = set(i.item_id for i in self.items)
            other_item_ids = set(i.item_id for i in other.items)
            if self_item_ids != other_item_ids:
                return False
    ```
    （frozen dataclass 因类未显式定义 `__hash__` 仍自动追加基于全部字段（含 primary_item 与 items 元组顺序）的哈希）
  - 影响: 已实测复现：items 顺序不同/primary 不同的两个选择 `a == b` 为 True 而 `hash(a) != hash(b)`，`b in {a}` 返回 False。一旦 TimelineSelection 进入 set/dict/缓存键（选择去重、命令合并等），成员判断结果错误。
  - 建议: 手动定义与 __eq__ 一致的 `__hash__`（如 `hash((frozenset(item_ids), is_range, range_start, range_end))`），或将 __eq__ 改为字段级比较后同步哈希。
  - 置信度: 高（已用实际模块运行验证）

- [AUD-18205] [Major] `__eq__` 忽略 primary_item、item_type 与 items 顺序，语义不同的选择被判相等
  - 位置: src/features/edit/core/selection.py:419
  - 证据:
    ```python
        # Compare item contents (order-independent for items)
        self_item_ids = set(i.item_id for i in self.items)
        other_item_ids = set(i.item_id for i in other.items)
        if self_item_ids != other_item_ids:
            return False
    ```
    （415 行注释声称 "order matters for primary"，实现却顺序无关且完全不比较 primary_item/item_type）
  - 影响: 已实测：CLIP 与 TRACK 同 id 判等、多选下仅改 primary_item（with_clip_selected replace=False 会更换 primary）也判等。任何以 `new_selection == old_selection` 短路刷新的调用方（属性面板、高亮同步）会漏更新；注释与实现相互矛盾。
  - 建议: __eq__ 至少比较 primary_item 与各项 item_type/track_id；或明确文档化"仅按 id 集合相等"并修正注释。
  - 置信度: 高（行为已实测；下游短路调用当前未搜到，具体漏更新点标中）

### 通过项
- 线程安全：纯不可变数据类，无 PyQt/共享状态
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 工厂方法（with_*/toggle/clear）均返回新实例，frozen 不可变性成立
- `select_clips_in_range` 半开区间相交判断（timeline_start < end and timeline_end > start）正确
- to_dict/from_dict 对称；from_dict 当前无生产调用方（已全库确认），未知 item_type 的 ValueError 风险暂不计
- 性能：均为 O(n) 元组推导，选择集规模小

---

## src/features/edit/core/serialization.py (行数: 891)

### 发现

- [AUD-18206] [Critical] v4→v5 迁移把 `project["timelines"]`（TimelineSequenceRef 字典）当序列列表迭代 → AttributeError → 旧版工程加载必败
  - 位置: src/features/edit/core/serialization.py:656
  - 证据:
    ```python
        timelines = data.get("timelines") or []
        for tl in timelines:
            for clip in (tl.get("clips") or []):
    ```
    （`_migrate_data` 仅收到 project 子字典——405 行 `EditProjectSerializer._dict_to_project(project_data, version)`，其中 timelines 由 `_project_to_dict` 260-263 行写成 `{timeline_id: _timeline_ref_to_dict(...)}` 字典；已用 `git show a5b38cc1^` 确认 v4 时代同为 ref 字典，clip 从不存在于此）
  - 影响: 任何含时间线的 v4 JSON 经 from_json/load_from_file 加载：迭代字典得到字符串 key，`tl.get("clips")` 抛 `AttributeError: 'str' object has no attribute 'get'`，被 764-766 行宽泛 except 吞成 "Failed to load project" 返回 None —— 旧工程完全无法打开（用户视角数据丢失）。注：当前生产加载走 edit_project_io 的 .topos ZIP 路径（明确不再支持旧 JSON），此 API 是旧格式唯一声称兼容的入口且必崩，测试只做同版本 roundtrip 故从未覆盖。
  - 建议: 迁移应作用于 wrapper 全量数据（timeline_sequences.{seq_id} 的 track/clip 结构，参照 captions/schema.upgrade_captions_in_project 的遍历方式）；迭代前做 isinstance(list/dict) 防御；为 v4 固定样例加回归测试。
  - 置信度: 高（v4 布局已用 git 历史核实）

- [AUD-18207] [Major] animated_params 迁移从未触及其实际所在（wrapper["timeline_sequences"]），transform 关键帧动画静默丢失
  - 位置: src/features/edit/core/serialization.py:467
  - 证据:
    ```python
        # 提取完整的 TimelineSequence 数据（可选）
        timeline_sequences_data = data.get("timeline_sequences")
        timeline_sequences = None
        if timeline_sequences_data:
            timeline_sequences = {
                seq_id: TimelineSequence.from_dict(seq_data)
                for seq_id, seq_data in timeline_sequences_data.items()
            }
    ```
    （clip 与 animated_params 实际存于 seq_dict 的 track 列表中；`ClipInstance.from_dict`（timeline_v2.py:1101-1106）按原 key 逐字解析，无 position_x→translate_x 改名/×1920 换算；渲染端读 `transform.translate_x`（timeline_render_plan.py:910））
  - 影响: 即便修复 AUD-18206 的崩溃，v4 工程的 transform 关键帧仍保持旧 key 且值为归一化尺度 → `get_param_at_frame("transform.translate_x")` 查不到动画，位移动画静默消失或数值错误（渲染错误/数据丢失）。
  - 建议: 在 from_json_with_ui_state 中先对整个 wrapper 执行 v4→v5 迁移再解析 timeline_sequences；与 project_migration.py 的内容迁移链统一入口。
  - 置信度: 高（目标结构经 captions/schema.py 文档与 timeline_v2 源码双重核实）

- [AUD-18208] [Major] 工程保存非原子写入，中断即损坏唯一工程文件
  - 位置: src/features/edit/core/serialization.py:698
  - 证据:
    ```python
            # 序列化并写入
            json_str = EditProjectSerializer.to_json(project)
            filepath.write_text(json_str, encoding="utf-8")
    ```
    （save_to_file_with_ui_state 725-734 同样直接 write_text；文件头 52-54 行声明 .topos.autosave/.topos.bak 约定但均未实现）
  - 影响: 写入过程崩溃/断电/磁盘满 → 目标 .topos 被截断为半份 JSON，且无备份可回退，用户工程数据丢失。注：当前生产保存走 edit_project_io（ToposProjectIO ZIP），本函数仅测试/旧路径调用，风险随调用方接入而激活。
  - 建议: 写临时文件 → fsync → os.replace 原子替换；替换前将旧文件转存 .bak。
  - 置信度: 高（机制确定；生产可达性已注明）

- [AUD-18209] [Major] load/save 四处 `except Exception` 宽泛吞错，掩盖领域错误并混淆失败原因
  - 位置: src/features/edit/core/serialization.py:764
  - 证据:
    ```python
            json_str = filepath.read_text(encoding="utf-8")
            project = EditProjectSerializer.from_json(json_str)

            logger.info(f"Project loaded from: {filepath}")
            return project
        except Exception as e:
            logger.error(f"Failed to load project: {filepath}, {e}")
            return None
    ```
    （702-704、738-740、793-795 同型）
  - 影响: 违反 .clinerules 铁律"不得宽泛 except 掩盖领域错误"——AUD-18206 的 AttributeError 当前正被此处吞为一条泛化日志，调用方无法区分文件缺失/JSON 损坏/版本不支持/迁移 bug，也无从向用户给出准确提示。
  - 建议: 分层捕获（OSError/JSONDecodeError/ValueError 分别上报），load API 返回带原因的结果对象或抛出类型化异常。
  - 置信度: 高

- [AUD-18210] [Suggestion] 反序列化对显式 JSON null 无防御（tuple(None)/metadata=None 直接崩溃）
  - 位置: src/features/edit/core/serialization.py:564
  - 证据:
    ```python
            keywords=tuple(data.get("keywords", ())),
            rating=int(data.get("rating", 0)),
            ...
            metadata=data.get("metadata", {}),
    ```
  - 影响: `data.get("keywords", ())` 在字段存在且为 null 时返回 None → `tuple(None)` TypeError；`metadata=None` 传入 MediaAsset 后下游 `.get` 崩溃。手改/异常工具产出的工程文件属外部数据（铁律要求验证），现经宽泛 except 统一变成"加载失败"。
  - 建议: 对 list/dict 字段做 `x or 默认值` + isinstance 校验；rating 用 try/float 兜底。
  - 置信度: 中

- [AUD-18211] [Suggestion] 版本注释过期："目前版本为 3"，实际 SERIALIZATION_VERSION = 5
  - 位置: src/features/edit/core/serialization.py:625
  - 证据:
    ```python
        Note:
            目前版本为 3，如果未来有新版本，需要在这里添加迁移逻辑。
    ```
  - 影响: 误导后续维护者对迁移链现状（v4→v5 已存在且 v3 起点）的判断。
  - 建议: 更新为当前版本与迁移链描述（v3→v4→v5）。
  - 置信度: 高

### 通过项
- 线程安全：全静态方法、无共享可变状态
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- to_dict/from_dict 字段对称性：assets（含 P1-12 日志字段）、bins、smart_bins、timelines ref、color_stacks、comp_clips 均对称写出/读回
- 版本上限守卫（version > SERIALIZATION_VERSION 拒绝）与 project_type 校验存在
- v4→v5 关键帧数值换算的 try/except (TypeError, ValueError) 防御本身正确（问题在遍历目标，见 18206/18207）
- caption 升级（upgrade_captions_in_project）在解析 timeline_sequences 之前执行的顺序正确

---

## src/features/edit/core/sidechain_ducker.py (行数: 269)

### 发现

- [AUD-18212] [Major] `auto_duck` 对 sidechain 短于 music 无校验 → 越界 IndexError 崩溃
  - 位置: src/features/edit/core/sidechain_ducker.py:266
  - 证据:
    ```python
        sc = sidechain
        if sc.ndim == 2:
            sc = sc[:, :n]
        else:
            sc = sc[:n]
        return ducker.process(music, sidechain=sc)
    ```
    （process 内 166 行 `sc_peak = np.max(np.abs(...), axis=0)` 得长度 m<n 的 sc_db，179-180 行 `for i in range(n): if sc_db[i] > threshold` 越界；docstring 仅口头要求 "长度需 >= music"）
  - 影响: 音乐长于语音（ducking 场景常态：配乐贯穿、对白稀疏）时离线 auto_duck 直接 IndexError 崩溃。当前无生产调用方（仅 tests/edit/test_sidechain_ducker.py），接线后即暴露。
  - 建议: 长度不匹配时对 sidechain 零填充至 n（静默不触发 duck）并记日志，或显式抛出带说明的 ValueError。
  - 置信度: 高（机制确定）

- [AUD-18213] [Major] duck 中途禁用/启用产生瞬时增益跳变（click），违背本模块"避免突变"的设计目标
  - 位置: src/features/edit/core/sidechain_ducker.py:156
  - 证据:
    ```python
        if not self._params.enabled or samples.ndim != 2 or samples.shape[1] < 1:
            return samples
    ```
    （set_params(None)→enabled=False，`_gain_db` 保留在如 -6dB；上一块输出已衰减，下一块直通 0dB）
  - 影响: duck 状态下用户关闭 ducker → music 瞬间 +6dB 阶跃 → 可闻爆音；与文件头/注释反复强调的 "attack/release 控制压低/恢复速度，避免突变（click）" 直接矛盾（无 side-chain 路径都专门做了平滑释放）。
  - 建议: enabled 翻转时不直通：继续走 `_smooth_release_to_zero`（或一次性渐变）把增益释放回 0 后再旁路。
  - 置信度: 中（DSP 行为确定；无生产调用方，用户影响待接线后确认）

- [AUD-18214] [Suggestion] `auto_duck` 传入 1-D music 时被静默直通（无处理也无提示）
  - 位置: src/features/edit/core/sidechain_ducker.py:263
  - 证据:
    ```python
        n = music.shape[-1] if music.ndim == 2 else len(music)
        sc = sidechain
        if sc.ndim == 2:
    ```
    （263 行专门兼容 1-D 取 n，但 process 156 行 `samples.ndim != 2` 直接原样返回）
  - 影响: 调用方给单声道 1-D 数组时 auto_duck "成功"返回但完全未做 ducking，结果静默错误。
  - 建议: 1-D 输入 reshape(1,-1) 处理后返回原形状，或抛 TypeError 明确契约。
  - 置信度: 高

- [AUD-18215] [Suggestion] 逐样本 Python 循环：实时块尚可、离线整段处理数量级偏慢
  - 位置: src/features/edit/core/sidechain_ducker.py:179
  - 证据:
    ```python
        gains = np.empty(n, dtype=np.float64)
        for i in range(n):
            if sc_db[i] > threshold:
    ```
    （_smooth_release_to_zero 215-218 同型；auto_duck 离线入口对整段 music 一次 process）
  - 影响: 实时路径 48k 次迭代/秒（每块 ~1024）尚可接受；auto_duck 对 10 分钟音轨 = 2880 万次纯 Python 迭代，秒级到分钟级耗时。分段成块处理可复用连续状态（状态已跨块）。
  - 建议: auto_duck 内按固定块（如 4096）切分循环调用 process；热路径可预计算 sc_db 阈值布尔序列后用 numba/向量化近似。
  - 置信度: 中

### 通过项
- 线程安全：单线程纯 DSP 状态机（未见跨线程调用方；若未来 UI 线程 set_params 与音频线程 process 并行需加保护，当前无调用方不计发现）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交：不涉及
- 连续状态跨块一致性：gain_db/hold_counter 均在 process 尾部回写，reset_state 覆盖 seek/暂停场景
- DSP 语义：增益范围 [target_duck_db, 0] 只衰减不放大；attack/release 系数公式与 CompressorEffect 同源；hold 计数逻辑正确
- 输入验证：enabled/ndim/空块守卫存在；DuckerParams.from_dict 全字段类型转换
- 类型注解完整

---

## src/features/edit/core/stem_export.py (行数: 639)

### 发现

- [AUD-18216] [Major] stem 渲染将整条时间线 PCM 全量驻留内存（output + 每 Bus 一份全长缓冲），长工程内存爆炸
  - 位置: src/features/edit/core/stem_export.py:288
  - 证据:
    ```python
    output = np.zeros((channels, total_samples), dtype=np.float32)
    bus_inputs: dict = {}
    ...
                    if target not in bus_inputs:
                        bus_inputs[target] = np.zeros((channels, total_samples), dtype=np.float32)
    ```
  - 影响: 2ch/48kHz/2 小时 float32 单缓冲 ≈ 2.7GB；MASTER stem 同时挂 output + 每个 stem 内 Bus 各一份全长缓冲，多 Bus 长片轻松超过物理内存 → MemoryError 导出中途崩溃（B2 生产入口已接线）。
  - 建议: 按块（如 1 秒）流式渲染并直接写入 WAV 编码器；Bus 输入改为滚动环形缓冲。
  - 置信度: 高

- [AUD-18217] [Major] stem 文件名仅按名称净化生成，同名轨道/组互相覆盖且均记成功
  - 位置: src/features/edit/core/stem_export.py:84
  - 证据:
    ```python
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.name)
        return f"{safe or 'stem'}.wav"
    ```
    （TRACK stem 名取 `t.name or t.track_id`（130 行），GROUP 名取 `sg.capitalize()`（156 行）；导出循环 528-530 行 `path = os.path.join(output_dir, spec.filename)` 直接覆盖写）
  - 影响: 两条轨道同名（复制轨道常见）或轨道名恰与组名净化后相同 → 后写 stem 覆盖先写 stem，结果列表却两条均 success —— 分轨静默丢失，交付物不完整且无告警。
  - 建议: plan 阶段检测重名并对后续 stem 追加 `_2`/track_id 后缀；或写前检查文件已存在。
  - 置信度: 高（机制确定；同名概率标中）

- [AUD-18218] [Suggestion] 最内层逐帧循环内重复执行模块 import，且 frames_to_samples 重复导入
  - 位置: src/features/edit/core/stem_export.py:322
  - 证据:
    ```python
            for frame_idx in range(overlap_start, overlap_end):
                ...
                from src.features.edit.core.channel_layout import (
                    adapt_samples_for_delivery,
                )
    ```
    （38 行已 `from src.features.edit.core.interchange import frames_to_samples`，235 行函数内又导入一次）
  - 影响: 每帧每 clip 每轨各做一次 import 语句执行（sys.modules 查找 + 绑定）；24fps×90 分钟 = 约 13 万次纯开销，热路径代码噪音。
  - 建议: 两处 import 上移至模块/函数顶部。
  - 置信度: 高

- [AUD-18219] [Suggestion] 嵌套序列音频展开失败被 `except Exception: pass` 静默吞掉，stem 悄悄缺内容
  - 位置: src/features/edit/core/stem_export.py:253
  - 证据:
    ```python
            expanded = flatten_nested_audio_tracks(
                sequence, sequence_resolver, asset_lookup)
            if expanded != tuple(getattr(sequence, "audio_tracks", ()) or ()):
                from dataclasses import replace as _dc_replace
                sequence = _dc_replace(sequence, audio_tracks=expanded)
        except Exception:  # noqa: BLE001 — 展开失败退回原轨
            pass
    ```
  - 影响: 展开抛错时退回原轨本身合理，但零日志：嵌套序列工程的 stem 少了虚拟轨音频，用户与排查者都无从得知原因（铁律：宽泛 except 不得掩盖领域错误）。
  - 建议: 至少 `logger.warning("nested audio expansion failed; falling back", exc_info=True)`。
  - 置信度: 高

- [AUD-18220] [Suggestion] write_stem_wav 失败时残留半截损坏 WAV 文件
  - 位置: src/features/edit/core/stem_export.py:446
  - 证据:
    ```python
        except Exception as e:
            logger.warning(f"Failed to write stem WAV {path}: {e}")
            if container is not None:
                try:
                    container.close()
    ```
  - 影响: 中途失败（磁盘满等）close 后留下可打开但截断的 .wav；结果标记 False，但用户目录里仍有看似合法的坏文件，易被误用于交付。
  - 建议: 失败分支删除/改名残留文件（写临时名成功后再 rename 可与 AUD-18216 的流式改造合并解决）。
  - 置信度: 高

- [AUD-18221] [Suggestion] 多处类型注解缺失泛型参数（bare tuple/dict/Callable）
  - 位置: src/features/edit/core/stem_export.py:78
  - 证据:
    ```python
        stem_type: StemType
        name: str
        track_ids: tuple = ()
        include_master: bool = True
    ```
    （289 行 `bus_inputs: dict = {}`；200-202 行 decode_clip_fn/sequence_resolver/asset_lookup 均为 bare `Optional[Callable]`）
  - 影响: 类型检查器无法校验 track_id 集合与回调契约，降低可维护性。
  - 建议: `tuple[str, ...]`、`dict[str, np.ndarray]`、`Callable[[Any, Any, int, float, int], Optional[np.ndarray]]` 等。
  - 置信度: 高

### 通过项
- 线程安全：纯函数式导出（调用方 B2 worker 在 QThread 中运行，本模块无共享可变状态；decoder 字典为闭包内局部）
- GPU 资源生命周期 / 信号防递归：不涉及
- 取消路径：export_all_stems 每 stem 前轮询 cancel_check，已完成的 stem 保留、cancelled 标记正确；进度回调收尾完整（取消提前返回除外，属文档化行为）
- 异常隔离：单 stem 失败被捕获记入结果，不中断其余 stem
- P1-15 fps 有理换算（frames_to_samples）与 P1-09 声道适配（adapt_samples_for_delivery）、C3 片段增益的应用点与时间线导出同口径；renderer API（render_track_block/render_bus_stage/apply_master/compute_audible_solo_closure）签名已核实匹配
- RMA-04 solo 闭包过滤、RMA-06/§3.1 start_frame/fps 传参正确
- 解码器生命周期：close_all 契约文档明确；decoder 按 asset_id 复用
- write_stem_wav 声道布局推断（ChannelLayout + Nc 回退）与分块写入结构合理

---

## 汇总

| 文件 | Critical | Major | Suggestion |
|------|----------|-------|------------|
| scene_cut_controller.py | 0 | 2 | 2 |
| selection.py | 0 | 2 | 0 |
| serialization.py | 1 | 3 | 2 |
| sidechain_ducker.py | 0 | 2 | 2 |
| stem_export.py | 0 | 2 | 4 |
| 合计 | 1 | 11 | 10 |

Critical：AUD-18206（v4→v5 迁移迭代错结构致旧工程加载必败）。

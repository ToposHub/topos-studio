# Batch-163 审查报告 — src/features/edit/core（7 文件，2,856 行）

编号区间：AUD-16300–AUD-16318。审查日期：2026-09-28。
维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

---

## src/features/edit/core/comp_clip_data.py (行数: 331)

### 发现

- [AUD-16300] [Suggestion] from_dict 对显式 JSON null 字段抛 TypeError 而非受控校验错误
  - 位置: src/features/edit/core/comp_clip_data.py:275-278（结合 :88-93）
  - 证据:
    ```python
    width=data.get("width", 1920),
    height=data.get("height", 1080),
    fps=data.get("fps", 24.0),
    duration_frames=data.get("duration_frames", 240),
    ```
    ```python
    if self.width <= 0 or self.height <= 0:
        raise ValueError(f"Invalid resolution: {self.width}x{self.height}")
    ```
  - 影响: `dict.get(key, default)` 的默认值只在键缺失时生效；持久化 JSON 中若字段为 `null`（如 `"width": null`），`width` 为 `None`，`__post_init__` 的 `self.width <= 0` 抛 `TypeError: '<=' not supported between 'NoneType' and 'int'`，加载路径得到不可读的崩溃而非 ValueError。违反"外部数据须验证"。
  - 建议: from_dict 中对数值字段做 `None` 归一（`data.get("width") or 1920` 不适用于 0，应用显式 `v if v is not None else default`）或先做类型检查再构造。
  - 置信度: 高

- [AUD-16301] [Suggestion] from_dict 静默再生缺失的 comp_clip_id/asset_id/graph_id，破坏跨对象关联
  - 位置: src/features/edit/core/comp_clip_data.py:271-273
  - 证据:
    ```python
    comp_clip_id=data.get("comp_clip_id") or str(uuid.uuid4()),
    asset_id=data.get("asset_id") or str(uuid.uuid4()),
    graph_id=data.get("graph_id") or str(uuid.uuid4()),
    ```
  - 影响: 三条 ID 分别指向媒体池资产与合成图。任一 ID 缺失时静默生成新 UUID 虽能通过 `__post_init__`，但产物成为孤儿：媒体池查不到 asset_id、Composite 页找不到 graph_id，且无任何日志/告警，数据损坏被掩盖。
  - 建议: 缺失关键关联 ID 时应记录 warning 并交由调用方决定（重建关联或丢弃），而非无条件再生。
  - 置信度: 中

- [AUD-16302] [Suggestion] 未使用的导入 `field`
  - 位置: src/features/edit/core/comp_clip_data.py:20
  - 证据:
    ```python
    from dataclasses import dataclass, field
    ```
  - 影响: 全文无 `field(` 调用，死导入造成噪声。
  - 建议: 删除 `field`。
  - 置信度: 高

### 通过项
- 线程安全：frozen 数据类且全部字段为标量/字符串，无共享可变状态
- __post_init__ 输入验证完备（ID 非空、source_kind 白名单、分辨率/fps/duration 非负）
- 工厂方法参数与校验一致；with_* 均更新 modified_at
- to_dict/from_dict 字段对称，序列化往返完整
- 数值字面量规范（24.0/1920 等，无裸浮点）
- 无 GPU / 信号 / undo 相关逻辑（纯数据模型，维度不适用）

---

## src/features/edit/core/composite_failure_policy.py (行数: 53)

### 发现

- [AUD-16303] [Suggestion] 策略返回值为裸 dict，缺少类型参数/TypedDict
  - 位置: src/features/edit/core/composite_failure_policy.py:26-31
  - 证据:
    ```python
    @staticmethod
    def for_edit_preview(error: str | None = None) -> dict:
        return {
            "action": CompositeFailureAction.SHOW_ERROR_OVERLAY,
            "message": error or "Composite render error",
            "show_ghost": True,
        }
    ```
  - 影响: 四个工厂返回结构不同键集的裸 `dict`（show_ghost / keep_old_cache 仅部分存在），消费方只能靠约定取键，重构或拼写错误无法被类型检查捕获；与项目"强类型"约束不符。
  - 建议: 定义 `TypedDict`（或 dataclass）描述每种策略的返回结构。
  - 置信度: 高

### 通过项
- 线程安全：纯静态无状态
- 异常路径语义正确：导出 RAISE_ERROR 不静默输出源、渲染缓存 keep_old_cache 保留旧有效帧，与文档一致
- 无 GPU 资源、无信号、无外部 I/O（维度不适用）

---

## src/features/edit/core/composite_graph_data.py (行数: 976)

### 发现

- [AUD-16304] [Major] frozen 数据类的 with_* 方法浅拷贝共享 nodes/connections 等可变容器，"不可变"契约可被外部突变破坏
  - 位置: src/features/edit/core/composite_graph_data.py:139-141（with_name，其余 7 个 with_* 同型）
  - 证据:
    ```python
    name=name,
    nodes=self.nodes,
    connections=self.connections,
    viewer_state=self.viewer_state,
    ```
  - 影响: `frozen=True` 只阻止属性重绑定，不阻止容器内容突变。所有 with_* 产生的新实例与旧实例共享同一个 `nodes` 列表和 `viewer_state` 等字典；任何消费方（如加载到 NodeEngine 前的临时修补、UI 侧 append/修改）突变其一，会同时改变全部历史快照——用于 undo 栈/缓存键时表现为"旧状态被幽灵修改"，且无任何报错。模块 docstring 明确承诺"不可变数据类（frozen=True）"。
  - 建议: with_* 内对容器做 `copy.deepcopy`（或至少浅拷贝）后再传入；或在文档中显式降级契约并审计所有消费方不突变。
  - 置信度: 中（契约违反是结构性的；实际是否被突变未逐一追踪消费方）

- [AUD-16305] [Major] from_dict 对 null 字段崩溃：nodes=None 直接进 _normalize_nodes_format 迭代抛 TypeError
  - 位置: src/features/edit/core/composite_graph_data.py:454-455（另 :495-505 connections/width 同类）
  - 证据:
    ```python
    nodes = data.get("nodes", [])
    normalized_nodes = CompositeGraphData._normalize_nodes_format(nodes)
    ```
    ```python
    for node in nodes:
        normalized_node = dict(node)
    ```
  - 影响: `graphs.json` 中 `"nodes": null`（损坏文件或旧版本写出）时，`_normalize_nodes_format(None)` 在 `for node in nodes:` 处抛 `TypeError: 'NoneType' object is not iterable`；`"width": null` 则在 `__post_init__` 的 `self.width <= 0` 处抛 TypeError。这是项目加载路径（project.py 多处调用 from_dict），单图损坏会以不可读异常中断整个工程加载。违反"外部数据须验证"。
  - 建议: from_dict 入口对 nodes/connections/viewer_state 等做 `x if x is not None else 默认值` 归一，数值字段做类型检查。
  - 置信度: 高（代码路径确定；触发需要 JSON 中出现显式 null）

- [AUD-16306] [Suggestion] validate()/is_valid() 与 __post_init__ 豁免规则不一致，空白合成恒判"无效"
  - 位置: src/features/edit/core/composite_graph_data.py:924-927（对照 :122-126）
  - 证据:
    ```python
    if not self.source_clip_id:
        errors.append("source_clip_id is empty")
    if not self.source_asset_id:
        errors.append("source_asset_id is empty")
    ```
    ```python
    if self.source_kind not in ("empty_comp_clip", "detached_asset"):
        if not self.source_clip_id:
            raise ValueError("source_clip_id cannot be empty")
    ```
  - 影响: `create_default_empty_comp_clip` 合法产出 source_asset_id 为空的实例，但 `validate()` 不看 source_kind，恒返回错误 → `is_valid()` 恒 False。当前未发现外部调用（grep 仅见类内定义），属潜伏缺陷：一旦被用于保存前校验，空白 Comp Clip 会被误拒。
  - 建议: validate() 复用 __post_init__ 的 source_kind 豁免逻辑。
  - 置信度: 高

- [AUD-16307] [Suggestion] 8 个 with_* 方法手工复制全部 24 个字段，应改用 dataclasses.replace
  - 位置: src/features/edit/core/composite_graph_data.py:132-391
  - 证据:
    ```python
    def with_name(self, name: str) -> CompositeGraphData:
        return CompositeGraphData(
            composite_id=self.composite_id,
            source_clip_id=self.source_clip_id,
            ...（24 个字段逐一抄写，共 8 处）
    ```
  - 影响: 每次给数据类加字段须同步修改 8 处；漏改任意一处即静默丢字段（如某 with_* 忘记传 fps，调用后 fps 回落默认 24.0）。维护成本与出错面大。
  - 建议: `return dataclasses.replace(self, name=name, modified_at=time.time())`。
  - 置信度: 高

- [AUD-16308] [Suggestion] 默认图节点 ID 截断为 8 位十六进制（32 位），存在碰撞风险
  - 位置: src/features/edit/core/composite_graph_data.py:602-604（:736-737 同型）
  - 证据:
    ```python
    input_node_id = str(uuid.uuid4())[:8]
    topos_color_node_id = str(uuid.uuid4())[:8]
    output_node_id = str(uuid.uuid4())[:8]
    ```
  - 影响: 8 hex = 32 位 ID。同图内 3 节点单次碰撞概率约 2^-33，但该模式被 composite UI（node_graph_commands.py 复制/粘贴节点）沿用，图内节点数增多后生日碰撞概率上升；一旦碰撞，连接表按 id 寻址错乱，图结构损坏且难排查。
  - 建议: 保持完整 uuid4 字符串，或至少 12-16 位。
  - 置信度: 中（机制确定；触发概率低）

### 通过项
- 无 OpenGL 调用（纯数据模块，GPU 维度不适用）
- __post_init__ 对 composite_id/分辨率校验；空合成/脱钩豁免有明确注释依据
- from_dict 向后兼容回退链（source_refs → timeline_clip_input 参数）逻辑清晰
- to_dict/from_dict 字段对称，条件省略字段均有默认值兜底
- 无宽泛 except（模块无异常捕获）

---

## src/features/edit/core/consolidate.py (行数: 797)

### 发现

- [AUD-16309] [Critical] 分段输出文件名无去重，同名素材分段相互覆盖，归档工程指向错误媒体内容
  - 位置: src/features/edit/core/consolidate.py:430（死变量）、:474-477、:524-525
  - 证据:
    ```python
    used_names: set = set()          # 声明后全文从未使用
    ```
    ```python
    base = os.path.splitext(
        os.path.basename(asset.display_name or src)
    )[0] or f"asset_{asset_id[:8]}"
    base = _sanitize_name(base, fallback=f"asset_{asset_id[:8]}")
    ```
    ```python
    out_name = f"{base}_c{idx}{ext}"
    dst = os.path.join(media_root, out_name)
    ```
  - 影响: 输出名只由 display_name 基名 + 分段序号 + 扩展名决定。两个不同素材同名（多卡拍摄的 DSC_0001.MP4、或 transcode 模式下 a.mov/a.mp4 都变 a_c1.mov）时，第二个素材的 ffmpeg 以 `-y` 覆盖第一个素材刚生成的分段文件；第一个素材的 new_asset.path 仍指向该文件，但内容已是第二个素材的画面——归档工程静默渲染错误素材（渲染错误/数据损坏）。首个记录的 sha256 与最终文件也不再匹配。`used_names` 死变量表明去重逻辑计划过但从未实现。
  - 建议: 用 `used_names` 对 out_name 去重（冲突时追加 `_{asset_id[:8]}` 或递增后缀）；生成后校验文件非预期存在。
  - 置信度: 高（机制确定；触发需要同名素材，摄影机媒体常见）

- [AUD-16310] [Major] remux 的 `-map 0:v`/`-map 0:a` 未加 `?`：纯音频素材两种模式必失败，无音轨视频在 remux 模式必失败
  - 位置: src/features/edit/core/consolidate.py:354-356（结合 :533-542 的分支路由）
  - 证据:
    ```python
    "-map", "0:v",
    "-map", "0:a",
    "-map", "0:s?",
    ```
    ```python
    if mode == MODE_TRANSCODE and asset.media_type != "audio":
        _transcode_segment(...)
    else:
        _remux_segment(...)
    ```
  - 影响: ffmpeg 流选择器无尾部 `?` 时匹配不到流即报错退出（`Stream map '0:v' matches no streams`）。纯音频素材（media_type=="audio"）两种模式都走 `_remux_segment`，`-map 0:v` 必失败；无音轨的视频（航拍素材、屏幕录制、代理）在 remux 模式因 `-map 0:a` 必失败。这类素材整体标记 STATUS_FAILED、原始素材保留但不在归档内，工程"自洽归档"目标落空。字幕已正确用了 `0:s?`，音视频遗漏。
  - 建议: 视需要改为 `0:v?`/`0:a?`（至少对 media_type=="audio" 的素材只映射 `0:a?`），或按 media_type 选择映射集合。
  - 置信度: 高

- [AUD-16311] [Major] 同步阻塞 API + ffmpeg 默认无超时，唯一调用方在 UI 线程直接调用，整理期间界面冻结且不可取消
  - 位置: src/features/edit/core/consolidate.py:318-331、:397（调用方证据 src/features/edit/ui/media_pool_panel.py:5652-5655）
  - 证据:
    ```python
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )
    ```
    ```python
    ffmpeg_timeout: Optional[float] = None,
    ```
    ```python
    new_project, _new_seqs, report = consolidate_project(
        self._project, sequences=sequences, target_dir=target,
        mode=mode, handles_frames=handles,
    )   # media_pool_panel.py，Qt 菜单槽内同步调用，未传 ffmpeg_timeout
    ```
  - 影响: 每段一次 ffmpeg 子进程（transcode ProRes 长片段可达分钟级），调用方未传 `ffmpeg_timeout`（默认 None 不限），也没有进度回调/取消点；在 UI 槽内同步执行期间主线程事件循环停转——界面冻结（macOS 风车），用户无法取消，长工程整理可能被系统判定无响应。
  - 建议: 调用方移入 worker 线程/任务队列；consolidate_project 增加进度回调与取消检查点（段间），并为 ffmpeg_timeout 提供合理默认。
  - 置信度: 高

- [AUD-16312] [Suggestion] 素材任一分段失败即整体放弃重映射，已成功的分段文件成为归档内孤儿
  - 位置: src/features/edit/core/consolidate.py:546-552、:627-629
  - 证据:
    ```python
    except Exception as exc:  # noqa: BLE001
        logger.warning("Consolidate segment failed %s: %s", src, exc)
        status = STATUS_FAILED
        ...
        seg_ok = False
    ```
    ```python
    if seg_ok and seg_assets:
        seg_assign[asset_id] = seg_entries
        pending_assets.append((asset, seg_assets))
    ```
  - 影响: 一资产 3 段中第 2 段失败时，第 1/3 段的文件已写入 media/ 且 new_asset 已创建，但整个资产不进 seg_assign——文件留在归档中无任何引用，浪费磁盘且让"更小的自洽工程包"名不副实（另见 AUD-16310：失败概率被放大）。
  - 建议: 失败资产回滚时删除其已成功分段的文件，或改为按段重映射（需处理区间缺口）。
  - 置信度: 高

- [AUD-16313] [Suggestion] _segment_for 兜底分支可把区间外 source_start 静默映射到错误分段
  - 位置: src/features/edit/core/consolidate.py:750-754（结合 :223-224）
  - 证据:
    ```python
    # 防御：起点不在任何区间（理论上合并已保证），取首个不早于它的段
    for new_id, aligned, start, end in entries:
        if source_start < end:
            return new_id, aligned
    ```
    ```python
    duration = int(getattr(clip, "source_duration", 0) or 0)
    if duration <= 0:
        continue
    ```
  - 影响: "理论上合并已保证"不成立：`compute_used_ranges` 跳过 duration<=0 的退化 clip，若该资产同时被正常 clip 使用，退化 clip 的 source_start 可能落在合并区间之外（如区间 [(0,50),(100,150)]，退化 clip start=75）——兜底分支把它映射到第 2 段，new_start=0，剪辑静默指向错误内容而非进入 unmapped 报告。
  - 建议: 兜底也做范围校验（|source_start - start| 超阈值即 unmapped），或把退化 clip 一并纳入区间合并。
  - 置信度: 中

- [AUD-16314] [Suggestion] 多处函数缺返回类型注解
  - 位置: src/features/edit/core/consolidate.py:714-715、:743-744
  - 证据:
    ```python
    def _rebuild_bin_assets(bin_: Any, seg_assign: Mapping[str, Sequence[Any]]):
    ```
    ```python
    def _segment_for(asset_id: str, source_start: int):
    ```
  - 影响: 与模块其余部分的完整注解风格不一致，返回结构只能靠阅读函数体。
  - 建议: 补 `-> Any` / `-> Optional[Tuple[str, int]]` 等。
  - 置信度: 高

### 通过项
- 异常与取消路径：分段级 `except Exception` 有日志 + 报告记录 + noqa 说明，属刻意的失败隔离而非掩盖领域错误；ConsolidateError 区分 ffmpeg 不可用/超时/非零退出
- 关键帧对齐方向安全：`aligned_sec = min(kf, start_sec)` 只向前扩，且 `min(aligned_frame, rng.start)` 防浮点回退
- 状态提交一致性：clip/track/序列均用 dataclasses.replace / _rebuild 不可变重建，不改输入对象；素材箱引用同步替换
- compute_used_ranges 区间合并（排序 + 相邻/重叠合并）正确；handles 下限裁 0
- 静态素材（图片/序列/目录源）整份复制、偏移 0，与 collect 行为一致
- _fps_float 对 Rational/str/float/除零的防御完整
- 线程安全：无共享可变全局（纯函数式核心，不依赖 Qt，与声明一致）；调用线程问题见 AUD-16311
- 报告数据结构完整可序列化（schema 版本、missing_usage、remapped/unmapped、warnings）

---

## src/features/edit/core/crop_cpu.py (行数: 141)

### 发现

- [AUD-16315] [Major] 羽化渐变方向与 crop_render/GPU 参考实现相反：边界处 alpha=0、渐变在矩形内侧，同一 CropParams 在不同渲染路径产生不同可见区域
  - 位置: src/features/edit/core/crop_cpu.py:74-78、:130-141（对照 crop_render.py:163-167 与 CROP_FRAGMENT_SHADER:361-366）
  - 证据:
    ```python
    d_left = cols - x_min
    d_right = x_max - cols
    alpha_x = _smoothstep(d_left, feather_px) * _smoothstep(d_right, feather_px)
    ```
    ```python
    t = np.clip(distance / feather, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)
    ```
    （crop_render.py 参考实现：`t = np.clip(dist / f, -1.0, 0.0) + 1.0` —— 边界处 alpha=1，渐变在矩形外侧）
  - 影响: `_smoothstep` 在 distance=0（裁切边界）处 alpha=0，距边界 feather_px 处才到 1——羽化整体缩进矩形内；而 crop_render.build_crop_mask 与 GPU 着色器在边界处 alpha=1、向矩形外渐隐。两者对同一 CropParams 的可见画面相差半个 feather 带。两条路径都活着：timeline_export.py:6929/6995 走 apply_crop_cpu，timeline_export.py:8987-8988 与 GPU 预览（edit_gpu_preview_renderer.py:4116）走 crop_render——同一工程不同导出路由/预览与导出之间裁切结果不一致（渲染正确性缺陷）。另外本文件未像 crop_rect_for_size 那样把 feather 钳到 kept*0.5，超大 feather 会让中心都到不了 1。
  - 建议: 以 crop_render 为单一几何源：apply_crop_cpu 改为构造 CropParams→crop_rect_for_size→build_crop_mask 复用同一 matte（或直接调用 crop_render），删除本地平行实现。
  - 置信度: 高（分歧由代码直接证明；哪侧是预期语义需产品确认，但两实现共存本身即缺陷）

- [AUD-16316] [Suggestion] crop_is_identity 接受长度<5 的元组但 apply_crop_cpu 解包 5 元，长度恰为 4 时崩溃
  - 位置: src/features/edit/core/crop_cpu.py:27-28、:59
  - 证据:
    ```python
    if not crop_tuple or len(crop_tuple) < 4:
        return True
    ```
    ```python
    left, right, top, bottom, feather = (float(v) for v in crop_tuple[:5])
    ```
  - 影响: 长度为 4（缺 feather）且含非零值的 crop 元组通过 identity 检查后在解包处抛 ValueError。当前调用方（timeline_export）传 layer.crop 五元组，属防御契约不一致的潜伏问题。
  - 建议: identity 检查与解包的长度契约统一为 5（`len(crop_tuple) < 5 → identity` 或解包时给 feather 默认 0.0）。
  - 置信度: 低（当前不可达，仅契约不一致）

### 通过项
- HDR/位深正确性：按 dtype 推导 max_value，uint16 导出缓冲不被降为 uint8，alpha 换算 rint+clip 正确
- 不修改调用方数组（copy / concatenate 生成新帧；identity 时原样返回零开销）
- 像素中心 (+0.5) 采样约定与 GPU 一致；smoothstep 公式为标准 GLSL 形式
- 纯 numpy 无状态函数，线程安全；无 GPU 资源

---

## src/features/edit/core/crop_render.py (行数: 372)

### 发现

- [AUD-16317] [Suggestion] apply_crop_premultiplied 中 astype 后再 copy 造成每帧冗余全帧分配
  - 位置: src/features/edit/core/crop_render.py:236
  - 证据:
    ```python
    out = frame_rgba.astype(np.float32).copy()
    ```
  - 影响: `astype(np.float32)` 默认 copy=True 已返回新数组，`.copy()` 再复制一次。该函数在预览路径逐帧调用（timeline_preview_renderer.py:3945），4K RGBA float32 每帧多一次 ~132MB 分配，拖累实时预览。
  - 建议: 删除 `.copy()`。
  - 置信度: 高

### 通过项
- GLSL 正确性：CROP_FRAGMENT_SHADER 数值字面量全部带 .0、无循环、类型显式，符合 .clinerules
- GPU/numpy 数学一致：着色器 per-axis 归一化 (dx/u_feather.x, dy/u_feather.y) 取 min 与 numpy 像素距离/f 取 min 代数等价；硬边 step 与 dist>=0 一致
- fbo_y_flipped 双向 UV 映射有完整文档与默认值；crop top/bottom 语义在 GL 默认朝向下正确
- HDR 正确性：apply_crop_straight_alpha 只缩放 alpha 不动 RGB（无 0-1 钳位）；apply_crop_premultiplied 的 RGB*alpha 是预乘语义而非 clamp
- feather 钳位到 kept*0.5 防止 ramp 在保留区交叉归零
- apply_crop_bgr 的 uint16 中间量与 //255 缩放无溢出
- 不可变 CropRect + 纯函数，线程安全；无资源泄漏面

---

## src/features/edit/core/custom_transitions.py (行数: 186)

### 发现

- [AUD-16318] [Suggestion] reload_user_transitions 仅有 import 时的自调用，"目录变化时调用"未接线；且注册表 clear+重建非原子
  - 位置: src/features/edit/core/custom_transitions.py:109-116、:184-185
  - 证据:
    ```python
    def reload_user_transitions() -> Dict[str, CustomTransition]:
        """扫描声明目录并重建注册表（应用启动 / 目录变化时调用）。"""
        _REGISTRY.clear()
    ```
    ```python
    # 启动时懒加载一次（目录缺失/为空安全）
    reload_user_transitions()
    ```
  - 影响: 全库 grep 无其他调用方——用户在运行中新增/编辑 Presets/User/transitions/*.json 后必须重启应用才生效，docstring 承诺的"目录变化时调用"没有实现方。另外 `_REGISTRY.clear()` 后逐文件重填非原子，若未来接入目录监视（渲染线程并发 resolve_user_transition），存在读到空表、用户转场瞬时解析为 None 的窗口。
  - 建议: 由 Presets 面板/目录监视接线 reload；重载改为"构建新 dict 后一次性赋值 `_REGISTRY.clear(); _REGISTRY.update(new)`"或替换引用，消除空窗口。
  - 置信度: 高（无调用方由 grep 证实；并发窗口取决于未来接线）

### 通过项
- 输入验证完善：坏 JSON / 非 dict / 缺 id/label/base / 未知 base / params 非对象 / 重复 id 全部跳过并记日志，绝不阻断启动，符合"外部数据须验证"
- user_transitions_dir 的 getcwd+Presets 约定与全库一致（presets_panel、custom_export_presets、title_templates 等同型）
- user_transition_registry() 返回副本，外部拿不到内部可变引用；CustomTransition 冻结数据类
- 无 GPU 资源、无信号递归面（维度不适用）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-16309 |
| Major | 5 | AUD-16304, AUD-16305, AUD-16310, AUD-16311, AUD-16315 |
| Suggestion | 12 | AUD-16300–16303, AUD-16306–16308, AUD-16312–16314, AUD-16316–16318 |

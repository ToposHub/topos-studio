# batch-171 审查报告 — src/features/edit/core/project.py

批次：batch-171 [P1] 主题 src/features/edit/core//
编号区间：AUD-17100–AUD-17199
审查方式：全文件通读（5,392 行）+ 只读交叉验证（commands.py / media_pool_panel.py / timeline_v2.py 调用方）。

## src/features/edit/core/project.py (行数: 5392)

### 发现

- [AUD-17100] [Critical] update_comp_clip 原地修改与旧工程快照共享的合成图节点 dict，破坏不可变快照隔离（undo 历史被污染）
  - 位置: src/features/edit/core/project.py:4183-4243
  - 证据:
    ```python
    comp_dict = dict(new_compositions[graph_id])        # 4183 浅拷贝，nodes 列表仍共享
    ...
    nodes = list(comp_dict.get("nodes", []))            # 4197 新列表，但 node dict 对象仍是旧工程的
    for node in nodes:
        if node.get("type") == "constant":
            params = dict(node.get("parameters", {}))
            if width is not None:
                params["width"] = width
            ...
            node["parameters"] = params                 # 4230 原地改写旧工程共享的 node dict
    ```
  - 影响: 本模块的全部更新路径都遵守 copy-on-write（新 dict + replace），唯独这里对共享 node dict 做原地赋值。旧 EditProject 实例（undo 栈快照、revision 历史）与 project_id 相同的其他快照会"看到"新的 width/height/背景色——撤销后图数据不回滚，快照一致性被破坏，可导致撤销后渲染参数错误（渲染错误/状态丢失）。
  - 建议: 重建 node（`node = {**node, "parameters": params}`）并用新列表写回 `comp_dict["nodes"]`，保证旧快照零共享突变；与本文件其他 copy-on-write 路径对齐。
  - 置信度: 高

- [AUD-17101] [Major] remove_timeline 不清理各 bin 的 timeline_ids 引用，删除时间线后留下悬挂引用
  - 位置: src/features/edit/core/project.py:2512-2552
  - 证据:
    ```python
    new_timelines = dict(self.timelines)
    del new_timelines[timeline_id]

    # 如果移除的是活动时间线，清除 active_timeline_id
    new_active_id = self.active_timeline_id
    if self.active_timeline_id == timeline_id:
        new_active_id = None
    ```
    （对比 remove_asset 1365-1388 会遍历所有 bin 清理引用；本方法无任何 bin 清理。已验证调用方 commands.py:4398 DeleteTimelineCommand 也不清理。）
  - 影响: bin.timeline_ids 中残留已删除时间线的 ID，随工程持久化；undo 恢复靠命令层快照而非引用一致性，媒体池按 bin 列时间线时可能显示幽灵项或查询 miss。
  - 建议: remove_timeline 内遍历 bins，对包含该 timeline_id 的 bin 调 with_timeline_removed（与 remove_asset 对称）。
  - 置信度: 高

- [AUD-17102] [Major] remove_bin 不处理子 bin 的 parent_id，删除后子 bin 的父引用指向不存在的 bin
  - 位置: src/features/edit/core/project.py:2253-2297
  - 证据:
    ```python
    new_bins = dict(self.bins)
    del new_bins[bin_id]
    ...
    return EditProject(
        ...
        bins=new_bins,
    ```
    （docstring 仅说明素材引用的处理："素材箱中的素材引用会被移除，但素材本身保留在其他素材箱或 root bin 中"；对以该 bin 为 parent 的子 bin 无任何重挂或清理。）
  - 影响: 子 bin 的 parent_id 悬挂，层级树构建时这些子 bin 变成不可达/孤儿，或被 UI 静默丢弃；状态随工程文件持久化。
  - 建议: remove_bin 时将被删 bin 的子 bin 的 parent_id 重置为 root_bin_id（或上移一级），并在 bin_count 语义中保持一致。
  - 置信度: 高

- [AUD-17103] [Major] import_subclip 回退分支创建的 Root bin 被后续 `new_bins = dict(self.bins)` 覆盖丢弃，root_bin_id 指向不存在的 bin
  - 位置: src/features/edit/core/project.py:1443-1469
  - 证据:
    ```python
    if target_bin_id is None or target_bin_id not in self.bins:
        ...
        else:
            # 创建 root bin
            root_bin_id = str(uuid.uuid4())
            ...
            new_bins = dict(self.bins)
            new_bins[root_bin_id] = root_bin
            new_root_bin_id = root_bin_id
            target_bin_id = root_bin_id

    # 添加到目标 bin
    new_bins = dict(self.bins)          # ← 重建，丢弃上面创建的 root bin
    target_bin = self.bins.get(target_bin_id)   # ← 新 bin 不在 self.bins 中，必为 None
    if target_bin:
        ...
    else:
        new_root_bin_id = target_bin_id
    ```
  - 影响: 当工程无可用 root bin 时（如反序列化的旧/损坏 schema），子片段被加入 assets 但不进任何 bin，且 root_bin_id 指向 bins 中不存在的 ID——root_bin 属性返回 None，媒体池根视图与后续 import_asset 走"root bin is None"再建新 root，项目状态不一致。
  - 建议: 回退分支创建的 root bin 应保留到最终 new_bins（把第二次 `dict(self.bins)` 改为基于已构建的 new_bins 增量更新），并对 target_bin 为 None 的分支重新审视。
  - 置信度: 高（代码路径明确；触发依赖 root bin 缺失的边缘状态）

- [AUD-17104] [Major] check_media_availability 只豁免 comp_clip，其余虚拟素材被 os.path.exists 判为不可用，与 is_virtual 语义不一致
  - 位置: src/features/edit/core/project.py:5014-5020
  - 证据:
    ```python
    for asset_id, asset in project.assets.items():
        if asset.is_comp_clip:
            availability[asset_id] = True  # Comp clips don't have real file paths
        else:
            availability[asset_id] = os.path.exists(asset.path)
    ```
    （同文件 get_offline_assets 5034-5038、update_offline_status 5057-5060 均用 `asset.is_virtual` 跳过全部虚拟类型。）
  - 影响: solid / adjustment / motion_graphic / nested_sequence / placeholder 素材的 path 是 `topos://` URI 或空串，os.path.exists 恒 False——调用该函数会把所有非 comp_clip 虚拟素材报告为离线/不可用，产生错误的可用性视图。
  - 建议: 改用 `asset.is_virtual`（涵盖 comp_clip 及全部生成器/虚拟类型）。
  - 置信度: 高

- [AUD-17105] [Major] reset_clip_composite 返回类型不一致：注解为 EditProject，实际返回 (EditProject, TimelineSequence) 元组，且与早退 `return self` 混用
  - 位置: src/features/edit/core/project.py:4330-4453
  - 证据:
    ```python
    def reset_clip_composite(
        self,
        timeline_id: str,
        clip_id: str,
        timeline: 'TimelineSequence | None' = None,
    ) -> EditProject:
    ```
    而结尾（4434-4453）：
    ```python
    return EditProject(
        ...
    ), timeline.with_clip_updated(restored_clip)
    ```
    （中途 clip 为 None / original_asset_id 为空时又 `return self`。已验证当前仅 tests/edit/test_comp_clip_reset.py 调用并按元组解包，无生产调用方。）
  - 影响: 按注解使用的调用方（期望 EditProject）会拿到 tuple，后续属性访问直接 AttributeError；同一方法两种返回形状是潜在崩溃源，类型检查器也会漏报。
  - 建议: 注解改为 `-> tuple[EditProject, TimelineSequence]`，或将 timeline 更新职责移出（由调用方处理），统一单一返回类型。
  - 置信度: 高

- [AUD-17106] [Major] duplicate_asset 丢弃素材日志字段（keywords/rating/flag/color_label/comment）与 is_offline，用户整理的元数据在复制后静默丢失
  - 位置: src/features/edit/core/project.py:2118-2136
  - 证据:
    ```python
    new_asset = MediaAsset(
        asset_id=new_asset_id,
        path=src_path,
        media_type=src.media_type,
        display_name=src_name,
        metadata=metadata,
        ...
        reel=src.reel,
        scene=src.scene,
        shot=src.shot,
        take=src.take,
    )
    ```
    （reel/scene/shot/take 被显式保留，但 keywords、rating、flag、color_label、comment 均未传，落回默认值；is_offline 也被重置为 False。）
  - 影响: 媒体池"复制素材"（DuplicateAssetCommand，media_pool_panel Alt 拖拽）后，用户打的评级/旗标/颜色标签/关键词/注释全部丢失；离线源素材的副本还虚报在线。专业日志工作流（P1-12）数据丢失。
  - 建议: 复制时携带 `keywords=src.keywords, rating=src.rating, flag=src.flag, color_label=src.color_label, comment=src.comment, is_offline=src.is_offline`（proxy 重置为 none 可保留现状）。
  - 置信度: 高

- [AUD-17107] [Major] _probe_audio_info 的 container.close() 无 finally 保护，探测中途异常时 PyAV 容器（文件句柄）泄漏
  - 位置: src/features/edit/core/project.py:4708-4766
  - 证据:
    ```python
    container = av.open(path)
    ...
    codec_name = audio_stream.codec.name if audio_stream.codec else 'unknown'   # 4727 未被内层 try 覆盖
    ...
    container.close()          # 4754 仅在正常路径执行

    except Exception as e:     # 4764 外层兜底
        logger.warning(f"Failed to probe audio info: {path}, {e}")
        return None            # container 未关闭
    ```
  - 影响: audio_stream.codec 等属性在损坏文件上抛出的任何异常都会跳过 close，文件句柄/解复用器泄漏；批量导入大量损坏媒体时句柄累积，最终打不开文件（资源泄漏，符合 Critical 定义边缘，按 Major 计）。
  - 建议: 用 `with av.open(path) as container:`（PyAV 支持 context manager）或 try/finally 保证关闭。
  - 置信度: 高

- [AUD-17108] [Major] duplicate_asset 在纯数据模型内调用全局 scene store register()，产生撤销不可回收的副作用并造成跨 feature 反向依赖
  - 位置: src/features/edit/core/project.py:2054-2077
  - 证据:
    ```python
    try:
        from src.features.teaching_animation.scene_store import get_shared_scene_store
        motion_graphic_scene = get_shared_scene_store().get(scene_id)
    except (ImportError, RuntimeError):
        motion_graphic_scene = None
    ...
    get_shared_scene_store().register(copied_scene)
    ```
  - 影响: (1) EditProject 全部方法均为纯函数式转换，唯独此处写全局单例——撤销 DuplicateAssetCommand 或丢弃该工程快照后，已注册的 copied_scene 永久留在共享 store（undo 无法反注册），重开旧工程可能见到泄漏的场景；(2) features/edit/core 反向 import features/teaching_animation，违反分层（products→features→shared→app）约束。
  - 建议: 把 scene 克隆+注册移到 service/命令层（DuplicateAssetCommand 执行时注册、undo 时反注册），project.py 只返回带新 scene_id 的元数据。
  - 置信度: 中（副作用与分层违规证据明确；实际泄漏后果取决于 store 的生命周期管理，未深读 scene_store）

- [AUD-17109] [Suggestion] invalidate_render_cache_for_asset 未按 composite 关联过滤，对每个匹配 comp clip 将全部 manifest 失效（过度失效），且直接引用该 asset 的普通 clip 反而不失效
  - 位置: src/features/edit/core/project.py:3172-3182
  - 证据:
    ```python
    for cc_dict in self.comp_clips.values():
        ...
        if hasattr(cc, 'source_asset_id') and cc.source_asset_id == asset_id:
            for m_dict in self.render_cache_manifests.values():
                m = RenderCacheManifest.from_dict(m_dict) if isinstance(m_dict, dict) else m_dict
                if isinstance(m, RenderCacheManifest):
                    affected_clips.add((m.timeline_id, m.clip_id))
    ```
  - 影响: 只要有一个 comp clip 引用该 asset，工程内所有 clip 的缓存统统置 STALE，无关片段被迫重渲染（性能）；而普通 clip→asset 的直接引用在此模型层不可见，调用方若依赖此方法做精确失效会漏。
  - 建议: 仿照 invalidate_render_cache_for_color_stack（3099 行用 `m.composite_id == graph_id` 精确匹配），按 cc.graph_id 匹配 manifest.composite_id。
  - 置信度: 中

- [AUD-17110] [Suggestion] MediaAsset.solid_color 回退色不一致：color 缺失回退黑色，color 格式损坏回退青色
  - 位置: src/features/edit/core/project.py:336-341
  - 证据:
    ```python
    c = self.metadata.get("color", [0.0, 0.0, 0.0])
    if not isinstance(c, (list, tuple)) or len(c) < 3:
        return (0.0, 0.6, 0.8)
    return (float(c[0]), float(c[1]), float(c[2]))
    ```
  - 影响: docstring 声明"缺失时回退为黑色"，但格式异常（外部数据/旧工程）时渲染出默认青色 (0.0, 0.6, 0.8)，两种回退语义并存，缺陷数据被静默替换成显眼色而非统一处理。
  - 建议: 两处统一（都回退黑色，或都回退工厂默认并在 docstring 说明）；异常路径顺带打 warning。
  - 置信度: 高

- [AUD-17111] [Suggestion] dirty 以 modified_at > created_at 近似，保存后/撤销回已保存状态时仍恒为 True
  - 位置: src/features/edit/core/project.py:4561-4568
  - 证据:
    ```python
    @property
    def dirty(self) -> bool:
        """... A project is considered dirty if it was modified after creation."""
        return self.modified_at > self.created_at
    ```
  - 影响: 不可变模型上任何一次（甚至被 undo 抵消的）变更都会推高 modified_at；除非保存路径重置 created_at，否则保存后 dirty 依旧为 True，用户会一直看到"未保存更改"提示（ProjectDocument 协议语义受损）。
  - 建议: 由持久化/服务层在保存成功后重置基准（replace(created_at=modified_at)），或改用显式 dirty 标志/revision 比较而非时间戳。
  - 置信度: 中（未验证所有保存路径是否重置时间戳）

- [AUD-17112] [Suggestion] SmartBin offline 过滤与 offline_assets 属性对每个素材做同步 os.path.exists 磁盘 IO，UI 反复查询大工程时卡顿
  - 位置: src/features/edit/core/project.py:711-717（另见 4614-4617）
  - 证据:
    ```python
    elif self.filter_type == "offline":
        # 优先使用 is_offline 字段，回退到文件系统检查
        for asset in project.assets.values():
            if asset.is_virtual:
                continue  # Virtual assets don't have real file paths
            if asset.is_offline or not os.path.exists(asset.path):
                assets.append(asset)
    ```
  - 影响: 每次 Smart Bin 刷新对 N 个素材做 N 次 stat；媒体池树刷新、属性面板轮询时重复触发，千级素材工程在慢速存储（网络盘）上明显卡 UI 线程。
  - 建议: 以 is_offline 字段为准（由 update_offline_status 定期/事件刷新），或在后台线程批量 stat 并缓存结果。
  - 置信度: 高（IO 模式是事实；卡顿程度依赖素材规模与存储）

- [AUD-17113] [Suggestion] invalidate_render_cache_for_color_stack 三层嵌套（graphs×nodes×manifests）且逐 clip 再全量扫描、重复 from_dict 反序列化，调色热路径复杂度偏高
  - 位置: src/features/edit/core/project.py:3090-3105
  - 证据:
    ```python
    for graph_id, graph_data in self.compositions.items():
        ...
        for node in graph_data.get("nodes", []):
            if node.get("type") == "topos_color" and node.get("data", {}).get("color_stack_id") == stack_id:
                # Find clips that use this graph
                for cache_id, m_dict in self.render_cache_manifests.items():
                    m = RenderCacheManifest.from_dict(m_dict) if isinstance(m_dict, dict) else m_dict
    ...
    for timeline_id, clip_id in affected_clip_ids:
        result = result.invalidate_render_cache_for_clip(timeline_id, clip_id)
    ```
  - 影响: 每个含 topos_color 节点的图都触发一次全量 manifest 反序列化，随后每个受影响 clip 再各自全量扫描并复制整个 manifests dict——多 clip 工程上呈近似平方复杂度；该失效路径挂在调色参数变更链路上，属性能敏感区。
  - 建议: 先解析一次 manifest 列表并按 composite_id 建索引，再一次性批量置 STALE 后单次 replace。
  - 置信度: 中（复杂度事实明确；实际调用频率取决于 service 层是否在参数拖动时同步调用）

- [AUD-17114] [Suggestion] create_comp_clip_from_timeline_clip 的 timeline 参数缺类型注解
  - 位置: src/features/edit/core/project.py:3760-3764
  - 证据:
    ```python
    def create_comp_clip_from_timeline_clip(
        self,
        timeline_id: str,
        clip_id: str,
        timeline=None,
        name: str | None = None,
    ) -> tuple[EditProject, str, str, str]:
    ```
  - 影响: 文件其余签名注解完备，此处裸参数削弱类型检查与 IDE 提示（.clinerules 强类型要求）。
  - 建议: `timeline: "TimelineSequence | None" = None`（同 reset_clip_composite 的写法）。
  - 置信度: 高

- [AUD-17115] [Suggestion] set_render_cache_manifest 对无 cache_id 的 legacy dict 以空字符串作键，多个 legacy 条目互相覆盖
  - 位置: src/features/edit/core/project.py:2974-2976、3017
  - 证据:
    ```python
    elif isinstance(manifest, dict):
        manifest_dict = manifest
        cache_id = manifest.get("cache_id", "")
    ...
    new_manifests[cache_id] = manifest_dict
    ```
  - 影响: 两个不带 cache_id 的 legacy dict 会落同一 `""` 键，后写覆盖先写；且空键条目在按 cache_id 清理/查询时语义含糊。
  - 建议: cache_id 为空时生成 uuid（或拒绝并记 warning），避免空键合并。
  - 置信度: 中

- [AUD-17116] [Suggestion] remove_asset 不清理关联 color_stacks / compositions / render_cache_manifests，孤儿数据随工程持久化
  - 位置: src/features/edit/core/project.py:1365-1409
  - 证据:
    ```python
    # 移除素材
    new_assets = dict(self.assets)
    del new_assets[asset_id]

    # 从所有 bin 中移除引用
    new_bins = dict(self.bins)
    for bin_id, bin_obj in self.bins.items():
        if asset_id in bin_obj.asset_ids:
            new_bins[bin_id] = bin_obj.with_asset_removed(asset_id)
    ```
    （color_stacks（get_color_stack_for_asset 2736-2741 按 asset_id 关联）、compositions（source_asset_id）与 manifests 均不动；invalidate_render_cache_for_asset 存在但未被调用。）
  - 影响: 删除素材后其调色栈/合成图/缓存清单成为孤儿并写入工程文件，文件膨胀且 get_color_stack_for_asset 类查询可能命中已删素材的残留栈。
  - 建议: remove_asset 内联动清理（或至少提供 cleanup_orphans 辅助），颜色栈删除时可保留 pending-undo 语义由命令层决定。
  - 置信度: 中

- [AUD-17117] [Suggestion] remove_comp_clip 未失效引用该图的渲染缓存，删除后 VALID 指纹缓存仍可被查询返回
  - 位置: src/features/edit/core/project.py:4266-4300
  - 证据:
    ```python
    if isinstance(cc, CompClipData):
        # 删除 MediaAsset
        new_assets.pop(cc.asset_id, None)
        # 删除 CompositeGraphData
        new_compositions.pop(cc.graph_id, None)
        # 从 bins 中移除
        ...
    del new_comp_clips[comp_clip_id]
    ```
    （同文件提供 invalidate_render_cache_for_composite（3107）但此处未调用。）
  - 影响: 指纹化 VALID 缓存按设计"保留供 undo 恢复"，但 comp clip 被真正删除后这些缓存成为永久孤儿；若时间线 clip 的 composite_id 未同步清空，播放可能继续命中已删图的缓存帧。
  - 建议: 删除时调用 invalidate_render_cache_for_composite(cc.graph_id)（或在注释中显式说明保留语义并确保调用方清 clip.composite_id）。
  - 置信度: 低（是否产生可见问题取决于调用方是否同步重置时间线 clip）

### 通过项

- 线程安全：全模块 frozen dataclass + 工厂方法纯转换，无可变共享状态写入路径（AUD-17100/17108 所列别名副作用除外），无锁需求，设计符合模块声明的不可变原则
- GPU 资源生命周期：不涉及 OpenGL/ModernGL 资源创建与释放（纯数据模型层，无 GPU 泄漏面）
- GLSL·HDR 正确性：本文件无着色器/HDR 像素路径（不适用；颜色元数据仅透传探测结果，含 color_primaries/transfer/matrix/range/is_hdr 持久化，无无理由 clamp）
- 信号防递归：无 Qt 控件/信号连接（不适用）
- 输入验证：MediaAsset/ProjectBin/SmartBin/TimelineSequenceRef 的 __post_init__ 校验完备（asset_id/bin_id/name 非空、rating 0-5、flag/color_label/media_type/proxy_status/filter_type 白名单、分辨率/fps/采样率正数）；replace_media 验证目标路径存在；_fps_to_float 对异常 fps 归一
- 状态提交一致性（部分通过）：所有常规更新路径均为 copy-on-write（dict(...) + replace），revision 语义按注释约定由 EditProjectService 统一递增，模型层不越权（缺陷见 AUD-17100/17101/17102/17116/17117）
- 异常与取消路径：probe_media_metadata/_probe_audio_info 探测失败均降级返回并带日志；set_render_cache_manifest 清理段有 (TypeError, ValueError) 保护且注释说明不阻断工程变更
- 性能模式（部分通过）：get_unfiled_asset_ids/get_subclips_* 等线性扫描规模可控；缺陷见 AUD-17112/17113
- 类型注解：整体完备（含返回类型与 Optional 细化），仅个别遗漏（AUD-17114）与一处注解失真（AUD-17105）

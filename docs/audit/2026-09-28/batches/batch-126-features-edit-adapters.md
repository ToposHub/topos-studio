# 批次 batch-126 审查报告：src/features/edit/adapters/

- 审查日期：2026-09-28
- 编号区间：AUD-12600–AUD-12699
- 批次主题：P1 Edit interchange adapters（FCPXML / OTIO / ImportBuilder / 共用类型）
- 文件数：5，共 1,139 行
- 生产接线核实：`src/app/controllers/file_controller.py`（`import_interchange_timeline` / `_export_interchange`，P1-16 已接 File 菜单）；`flatten_nested_sequence` 仅测试引用，生产未接线。
- 依赖语义核实：`src/shared/media/metadata.py` 中 `Rational.to_fps() == num/den`（帧率语义）；`EditTrack.transitions/is_enabled`、`TimelineSequence.markers/subtitle_tracks/all_tracks/_rebuild`、`TransitionInstance.transition_type/duration`、`TimelineMarker.frame/name/marker_id` 均存在，adapter 使用的属性合法。

汇总：Critical 2 · Major 9 · Suggestion 13。

---

## src/features/edit/adapters/__init__.py (行数: 7)

### 发现

（无）

### 通过项
- 通过（无逻辑）：仅模块级 docstring，无任何导入/导出符号，不存在可审查的逻辑。

---

## src/features/edit/adapters/fcpxml_adapter.py (行数: 443)

### 发现

- [AUD-12600] [Critical] frameDuration 帧率/帧时长语义颠倒：真实 FCPXML 导入时间全错，导出的 frameDuration 写成秒级错误值
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:68-80（另见 213-219、331）
  - 证据:
    ```python
    def _rational_to_fcpxml_fps(rational: Rational) -> str:
        """Rational → FCPXML frameDuration 字符串（如 ``24000/1001s``）。"""
        return f"{rational.num}/{rational.den}s"

    def _fcpxml_fps_to_rational(frame_duration: str) -> Rational:
        s = frame_duration.rstrip("s")
        if "/" in s:
            num, den = s.split("/", 1)
            return Rational(int(num), int(den))
    ```
    而 `_FCPXML_FPS_MAP`（L56-65）与 `Rational.to_fps()==num/den`（已核实 metadata.py:69-76）证明该 Rational 是**帧率**语义（如 30000/1001 = 29.97fps）。
  - 影响: Apple FCPXML 的 `frameDuration` 是**每帧的秒数**（如 24fps 写 `100/2400s`、29.97 写 `100/3000s`）。导入真实 FCP 文件时 `"100/2400s"` 被解析为 `Rational(100,2400)`，`to_fps()≈0.0417`，`_time_to_frames` 全部按 1/24 倍换算，所有 offset/duration/tcStart 换算成 ~576 倍偏差的垃圾帧号（渲染数据错误）；导出方向则把帧率 30000/1001 直接写成 `frameDuration="30000/1001s"`（≈30 秒/帧），FCP 打开即帧率错误。自产自读往返恰好自洽，掩盖了该缺陷（现有测试即如此通过）。
  - 建议: 帧时长与帧率互为倒数：导入应为 `Rational(den, num)`（帧时长→帧率），导出 `frameDuration` 应写 `f"{fps.den}/{fps.num}s"`；用真实 Apple 样例（`100/2400s`）补往返测试。
  - 置信度: 高

- [AUD-12601] [Critical] `_frames_to_time` 分母多乘 `fps.den`：NTSC 帧率导出的 offset/duration 缩小约 1001 倍
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:100-106
  - 证据:
    ```python
    def _frames_to_time(frames: int, fps: Rational) -> str:
        """整帧 → FCPXML time 字符串。"""
        # 用 fps 的精确分子分母表达，避免浮点误差
        total_num = frames * fps.den
        # seconds = total_num / (fps.num * fps.den) => 化简
        # 用 frames * den / num 秒，表达为有理数
        return f"{frames * fps.den}/{fps.num * fps.den}s"
    ```
  - 影响: 正确值为 `frames*den/num` 秒；分母多乘一个 `den` 后结果变成 `frames/num` 秒。整数帧率（den=1，如 24/25/30）恰好不受影响；NTSC（den=1001，29.97/23.98/59.94 极常用）导出的每个 offset/duration/tcStart 都缩小 1001 倍，导出文件完全不可用。即使按本 adapter 自家方言做帧往返（write→read→`_time_to_frames`）也只还原出 `frames/den` 帧，NTSC 下自往返即失败。
  - 建议: 改为 `f"{frames * fps.den}/{fps.num}s"`（或约分后输出）；补 NTSC 帧率往返单测。
  - 置信度: 高

- [AUD-12602] [Major] import 第二次 `ET.parse` 与 `int()` 宽度转换无异常防护，外部数据可触发未捕获异常直达 Qt 槽（PyQt 未捕获槽异常会 qFatal 中止进程）
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:182, 220-221
  - 证据:
    ```python
        tree = ET.parse(str(source))
        root = tree.getroot()
    ...
            width = int(fmt_el.get("width", 1920))
            height = int(fmt_el.get("height", 1080))
    ```
  - 影响: `inspect` 只捕获 ParseError/FileNotFoundError（L124），不触碰 width/height；`import_to_canonical` 对 `width="1920.0"` 或其他非法值抛 `ValueError`、对文件在 inspect 后被替换/删除抛 `ParseError/OSError`。生产调用点 `file_controller.py` 的 `adapter.import_to_canonical(path_sel)` 外层**无 try/except**（导出路径有，导入路径没有，已核实 file_controller.py:7006 附近），异常在 Qt 槽内未处理，PyQt 默认 qFatal 中止整个程序——崩溃由一份畸形第三方文件触发。
  - 建议: import 内对 parse 与属性转换统一 try/except 并降级为 `InterchangeReport` error 诊断（与 inspect 同风格），或至少在 controller 侧补 try/except。
  - 置信度: 高

- [AUD-12603] [Major] 模块声称"必须支持"的 marker/transition/字幕导入完全未实现，且无任何 DROPPED 报告条目；多 sequence 静默丢弃
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:235-278, 291
  - 证据:
    ```python
            for child in spine:
                tag = _localname(child.tag)
                if tag == "asset-clip":
                    ...
                elif tag == "gap":
                    ...
            sequences.append(seq)
            break  # 首版只取第一个 sequence
    ```
  - 影响: 文件头（L7-16）声明首版"必须支持"marker、简单 cross dissolve、基础字幕，但 spine 遍历只处理 `asset-clip` 与 `gap`，其余元素（title/marker/transition/role/lane）被静默跳过；`break` 只取第一个 sequence，其余 event/sequence 全部丢弃。两者均无 `InterchangeEntry(DROPPED/CONVERTED)` 条目，直接违反本模块自述与 TAEP §9.4"绝不静默丢信息"：用户在 preview 对话框看到的是完整保留的假象。
  - 建议: 对未处理的 spine 子元素逐类追加 DROPPED/REQUIRES_USER_ACTION 条目；多 sequence 至少记一条 DROPPED（含名称与数量）。
  - 置信度: 高

- [AUD-12604] [Major] 悬空 `ref` 的 asset-clip 生成空 `asset_id` 的 clip，报告却标 PRESERVED
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:238-239, 250-267
  - 证据:
    ```python
                if tag == "asset-clip":
                    aid = child.get("ref", "")
                    asset = assets_meta.get(aid, {})
                    ...
                    clip = ClipInstance(
                        clip_id=f"imp-{clip_idx}", asset_id=aid, ...
                    ...
                    entries.append(InterchangeEntry(
                        category=InterchangeEntryCategory.PRESERVED, element="asset-clip",
                        description=f"clip {clip.clip_id} <- asset {aid}",
                    ))
    ```
  - 影响: 外部数据引用了不存在的资源 id 时（拼写/裁剪过的 FCPXML 常见），`assets_meta.get(aid, {})` 静默得到空 dict，media_type 兜底为 "video"，产出 `asset_id=""` 或悬空 id 的 clip，且报告标 PRESERVED——假阳性报告；下游 `import_builder` 只能把它归入 unmapped（其 UI 文案是 "kept as-is"），工程里留下引用不存在素材的 clip。
  - 建议: `assets_meta` 未命中时改记 `REQUIRES_USER_ACTION`/`DROPPED` 条目，并可生成 `AssetRelinkRequest` 而不是伪装成已保留。
  - 置信度: 高

- [AUD-12605] [Major] `except Exception: pass` 吞掉 frameDuration 解析错误，静默回退 24fps 且无报告
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:214-219
  - 证据:
    ```python
            fd = fmt_el.get("frameDuration")
            if fd:
                try:
                    fps = _fcpxml_fps_to_rational(fd)
                except Exception:
                    pass
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"。非法 frameDuration（如 `"100/0s"` 会触发 `Rational.__post_init__` 的 ValueError）被吞掉后 fps 保持默认 24，后续所有帧换算基于错误帧率，且 InterchangeReport 没有任何诊断/条目——静默数据错误。
  - 建议: 捕获具体异常（ValueError/ZeroDivisionError）并写入 report 诊断（severity=error 或 converted 条目）。
  - 置信度: 高

- [AUD-12606] [Major] 用标准库 ElementTree 解析不可信 XML，无实体膨胀/二次膨胀防护
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:122, 182
  - 证据:
    ```python
            tree = ET.parse(str(source))
            root = tree.getroot()
    ```
  - 影响: FCPXML 是外部文件（第三方工程交换），Python 文档明确 `xml.etree.ElementTree` 对 billion laughs / quadratic blowup 属易受攻击类别（新版 expat 部分缓解实体放大，但二次膨胀与深层嵌套仍可造成内存/CPU 放大）。打开一份恶意/损坏文件即可让导入流程长时间占用或 OOM。项目铁律"外部数据须验证"。
  - 建议: 引入 `defusedxml.ElementTree`（或手动禁用 DTD 的 parser），并对文件大小/元素数设置上限。
  - 置信度: 中

- [AUD-12607] [Major] 导出的 `<transition>` 缺 offset/effect 引用、`<marker>` 挂在 spine 上，不符合 FCPXML schema
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:384-393, 401-405
  - 证据:
    ```python
                for trans in getattr(track, "transitions", ()):
                    if getattr(trans, "transition_type", "") == "cross_dissolve":
                        ET.SubElement(spine, "transition", {
                            "name": "Cross Dissolve",
                            "duration": _frames_to_time(getattr(trans, "duration", 12), fps),
                        })
    ...
        for marker in getattr(sequence, "markers", ()):
            ET.SubElement(spine, "marker", {
                "start": _frames_to_time(marker.frame, fps),
    ```
  - 影响: FCPXML 的 spine `transition` 必须带 `offset` 并引用 resources 中的 `<effect>`；`marker` 必须挂在 clip/asset-clip 上而非 spine。当前输出即使时间值正确（另见 AUD-12600/12601）也无法被 FCP/其他工具接受或会整块丢弃，而报告却标 PRESERVED。此外 transition 追加在所有 clip 之后，与 spine 顺序模型不符。
  - 建议: transition 写 offset + 在 resources 注册 effect（dissolve）并引用；marker 归属到对应 asset-clip（按 frame 区间），无法归属时标 DROPPED。
  - 置信度: 中

- [AUD-12608] [Suggestion] `_time_to_frames` 解析失败静默返回 0；`int()` 截断与 docstring 声称的 floor 不符（负值）
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:83-97
  - 证据:
    ```python
    def _time_to_frames(time_str: str, fps: Rational) -> int:
        """FCPXML time（秒的有理数，如 ``1250/2400s``）→ 整帧。

        按第 4 章合同：开始点 floor。
        """
        ...
        except (ValueError, ZeroDivisionError):
            return 0
        return int(seconds * fps.to_fps())  # floor
    ```
  - 影响: 非法时间串把 clip 静默落到帧 0（无诊断）；`int()` 对负数向零截断（-2.5→-2），并非 floor（-3），与注释/合同不符，负 tcStart/offset（pre-roll）时差一帧。
  - 建议: 失败时记诊断条目；用 `math.floor`。
  - 置信度: 高

- [AUD-12609] [Suggestion] `supported = ... or version.startswith("1.")` 使版本白名单形同虚设
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:134-135, 52
  - 证据:
    ```python
        version = root.get("version", "")
        supported = version in SUPPORTED_FCPXML_VERSIONS or version.startswith("1.")
    ```
  - 影响: `SUPPORTED_FCPXML_VERSIONS` 列出的所有 1.x 已被 `startswith("1.")` 完全覆盖，白名单永不起判定作用；未来 1.12+/未测试的 1.x 也会被标 supported，inspect 对不支持的版本给出误导结论（无版本警告条目）。
  - 建议: 只用白名单；白名单外版本标 supported=False 或 supported=True + CONVERTED/警告条目。
  - 置信度: 高

- [AUD-12610] [Suggestion] `import_to_canonical` 对同一文件解析两次（inspect 一次 + 自己再 parse 一次）
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:176, 182
  - 证据:
    ```python
        inspection = self.inspect(source)
        if not inspection.supported:
            ...
        tree = ET.parse(str(source))
    ```
  - 影响: 大型 FCPXML（多 event/多资源的真实工程）IO+parse 成本翻倍，且两次 parse 之间存在 TOCTOU（AUD-12602 的二次 parse 失败窗口即来源于此）。
  - 建议: 解析一次传递 root，inspect 提供可复用已解析树的内部入口。
  - 置信度: 高

- [AUD-12611] [Suggestion] `has_video` 只查直接子元素 `media-rep`，而取 src 用 `.//media-rep`，两处口径不一致
  - 位置: src/features/edit/adapters/fcpxml_adapter.py:195, 199
  - 证据:
    ```python
            has_video = asset_el.find("media-rep") is not None or asset_el.get("hasVideo", "1") == "1"
            ...
            media_rep = asset_el.find(".//media-rep")
            if media_rep is not None:
                src = media_rep.get("src", "") or ""
    ```
  - 影响: 真实 FCPXML 中 `media-rep` 嵌套在 `<media>` 下（非直接子元素），`find("media-rep")` 恒为 None，视频判定退化为只看 hasVideo 属性；`.//media-rep` 却能取到 src。视频/音频分类与 src 提取用不同视图，易产生"标成 audio 但取到视频 src"这类错位。
  - 建议: 统一用 `.//media-rep`（或解析 `<media>` 层级）。
  - 置信度: 中

### 通过项
- 线程安全：纯函数式转换 + 局部可变状态，无共享可变状态、无 Qt；`_FCPXML_NAMESPACE` 常量未使用（真实 FCPXML 无默认命名空间，`_localname` 兜底处理带 ns 的 tag）。
- GPU 资源生命周期：不涉及 ModernGL/OpenGL。
- GLSL·HDR 正确性：不涉及着色器与 HDR 数据。
- 信号防递归：无 Qt 信号。
- 状态提交一致性：只产出候选 `ImportResult` 不直接 commit，符合 TAEP §9.2 preview/confirm 设计。
- 导出资源 id 映射（r1=format、r2..=asset）与 `asset_extents` 时长下限计算正确；asset 写文件前 `mkdir(parents=True)` 且显式校验 output_path。
- 音频/字幕导出降级均按 §9.4 记 CONVERTED/REQUIRES_USER_ACTION 条目（导入侧缺失见 AUD-12603）。

---

## src/features/edit/adapters/import_builder.py (行数: 289)

### 发现

- [AUD-12612] [Suggestion] remap 注册 basename 键，同名不同目录素材存在错连风险
  - 位置: src/features/edit/adapters/import_builder.py:226-233, 242-244
  - 证据:
    ```python
        for key in (
            external_id,
            raw_path,
            local,
            os.path.basename(local) if local else "",
        ):
            if key and key not in remap:
                remap[key] = topos_id
    ...
            new_id = remap.get(clip.asset_id) or remap.get(
                url_to_local_path(str(clip.asset_id))
            )
    ```
  - 影响: basename 是弱键：两个不同目录的同名文件（如 A/clip.mp4 与 B/clip.mp4）时 `remap["clip.mp4"]` 只指向先注册者；任何以裸文件名为 asset_id 的 adapter（当前 FCPXML/OTIO 都用完整 id/url，暂不可达，属未来 adapter 触发面）会把 clip 静默连到错误素材——媒体错连比丢连更糟。
  - 建议: 移除 basename 键，或注册时若 basename 已存在且指向不同 asset 则标记 ambiguous 并拒绝该弱匹配。
  - 置信度: 中

- [AUD-12613] [Suggestion] 导入落地与预览行收集在调用线程执行阻塞 IO/逐素材探测（生产为 UI 线程同步调用）
  - 位置: src/features/edit/adapters/import_builder.py:139, 184-194
  - 证据:
    ```python
            rows.append((name, local or str(desc.get("path") or ""), bool(local) and os.path.exists(local)))
    ...
            if local and os.path.exists(local):
                ...
                project = project.import_asset(
                    path=local,
                    media_type=media_type,
                    metadata=probe(local),
    ```
  - 影响: `probe_media_metadata`（project.py:4769，内部走 FileIO/视频探测）对每个素材同步执行，`os.path.exists` 对网络/慢盘路径可能阻塞。生产调用链 `file_controller.import_interchange_timeline`（UI 槽，QFileDialog 后直接调用，已核实）在主线程同步跑 `collect_asset_rows` + `build_project_from_import_result`：几十个 4K 视频的工程导入会让 UI 冻结数秒以上。
  - 建议: builder 保持 core，但在 controller 侧把 collect+build 放入任务队列/线程（与项目渲染线程模式一致），或 probe 结果并发/惰性化。
  - 置信度: 高（代码路径）/ 中（实际冻结时长取决于素材数与磁盘）

- [AUD-12614] [Suggestion] `probe(local)` 无异常防护，单个损坏素材可使整个导入失败
  - 位置: src/features/edit/adapters/import_builder.py:186-194
  - 证据:
    ```python
                project = project.import_asset(
                    path=local,
                    media_type=media_type,
                    metadata=probe(local),
                    display_name=display_name,
                )
    ```
  - 影响: 用户已确认 preview 后进入落地阶段，若某个存在但损坏的媒体让 probe 抛异常（`probe_media_metadata` 内部 FileIO/PyAV 路径的失败行为未在本次范围内核实），异常上抛使整个导入中途失败且无部分结果/回滚提示。
  - 建议: 对 probe 单独 try/except，失败降级为空 metadata + summary 记 warning。
  - 置信度: 低（依赖 probe 内部行为，未核实）

- [AUD-12615] [Suggestion] 内部回调/容器类型注解用 `Any` 弱化（timeline 类型明确可得）
  - 位置: src/features/edit/adapters/import_builder.py:236, 239-250
  - 证据:
    ```python
        sequences_out: dict[str, Any] = {}
        ...
        def _remap_track(track: Any) -> Any:
    ```
  - 影响: `EditTrack`/`TimelineSequence` 可从 `src.features.edit.core.timeline_v2` 导入（本文件已在函数内延迟导入 project/timeline 类型），`Any` 使 `_rebuild`、`replace(track, clips=...)` 的参数拼错无法被类型检查发现。
  - 建议: 用真实类型标注（延迟导入已存在先例）。
  - 置信度: 高

### 通过项
- 线程安全：纯 core、无 Qt；全部经 `replace()` 生成新的 frozen `EditProject`，无原地修改共享状态。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：均不涉及。
- 状态提交一致性：不直接 commit，返回新 project + 完整 sequence 供上层提交；timeline_id 冲突时 uuid 后缀去重（L258-260）；离线素材与在线素材统一按路径去重。
- 异常与取消路径：`url_to_local_path` 对空串/非 file 协议有防护；Windows 盘符 file:///C:/ 处理正确（含正则）。
- 输入验证：desc 缺 name/path/media_type 均有兜底链（name→basename→external_id→"Imported Media"）；unmapped clip 记入 `unmapped_clip_ids` 并透传 UI，不静默丢弃。
- `url_to_local_path` 对 `file://localhost/path` 取 path 部分（netloc 丢弃）行为可接受；离线占位 `topos://interchange/...` 命名空间设计合理。
- 性能：`get_asset_by_path` 线性扫描在循环内为 O(n²)，但导入素材规模（几十至几百）下可接受，不单列。

---

## src/features/edit/adapters/interchange_types.py (行数: 157)

### 发现

- [AUD-12616] [Suggestion] 协议与结果类型大量使用 `Any` 承载 timeline/sequence（可用具体类型或 TYPE_CHECKING 前置类型）
  - 位置: src/features/edit/adapters/interchange_types.py:99-100, 153-155
  - 证据:
    ```python
        sequences: tuple[Any, ...]  # 候选 TimelineSequence（可能多个 event）
        assets: tuple[dict[str, Any], ...]  # 候选 MediaAsset 描述（未实例化）
    ...
        def export_from_canonical(
            self, sequence: Any, *, options: dict[str, Any] | None = None
        ) -> ExportResult:
    ```
  - 影响: 接口合同靠注释而非类型表达；adapter 实现方（如 fcpxml_adapter 访问 `sequence.video_tracks`/`markers`）拼写错误无法被静态检查发现（本次审计在 otio/fcpxml adapter 中发现的属性误用类问题即属此风险面）。
  - 建议: `if TYPE_CHECKING: from src.features.edit.core.timeline_v2 import TimelineSequence` 并以字符串注解引用，消除循环导入顾虑。
  - 置信度: 高

### 通过项
- 线程安全：全部 frozen dataclass + tuple 字段，天然不可变、可跨线程传递。
- GPU/GLSL/HDR/信号防递归：不涉及。
- 状态提交一致性：`ImportResult` docstring 明确"不直接 commit，preview 后经 command compiler 提交"的合同（生产调用侧实际经 `media_pool_panel.set_project` 直落、未见 command/undo 参与,file_controller.py:7036-7045——合同未被强制，属接线层问题，本文件无责，记录备查）。
- 异常路径：`has_errors`/`dropped_count`/`requires_action` 聚合逻辑正确；`to_dict` 序列化完整。
- 输入验证：`InterchangeEntry`/`AssetRelinkRequest` 字段语义清晰、无非法状态。

---

## src/features/edit/adapters/otio_adapter.py (行数: 243)

### 发现

- [AUD-12617] [Major] OTIO 导入 `timeline_start` 取自 clip 自身 duration，且忽略 RationalTime.rate：多 clip 全部错位堆叠、非 24fps 素材时长错误
  - 位置: src/features/edit/adapters/otio_adapter.py:116-125, 107
  - 证据:
    ```python
                sr = child.source_range
                # OTIO 用 RationalTime（帧），转整帧
                timeline_start = int(getattr(child, "duration", sr.duration).value) if hasattr(sr, "duration") else 0
                timeline_duration = int(sr.duration.value) if hasattr(sr, "duration") else 0
                source_start = int(sr.start_time.value) if hasattr(sr, "start_time") else 0
    ```
  - 影响: `child.duration` 是该 clip 自身的（裁剪后）时长，不是其在轨道上的位置：两个 100 帧 clip 都得到 timeline_start=100，互相堆叠、与源时间线完全不符（OTIO 正确做法是 `track trimmed_range_in_parent` 或按前序 item 累加）。`.value` 的单位是 `rate`（该 RationalTime 的率）帧，代码直接当 24fps 帧使用且序列 fps 硬编码 `Rational(24, 1)`（L107），29.97/25fps 的 OTIO 时间线时长与位置再错一层。导入结果为垃圾 placement。
  - 建议: 用 `track.children` 索引 + `trimmed_range_in_parent()` 求 timeline_start；从 `timeline.duration().rate`（或 global_start_time.rate）构造序列 fps Rational。
  - 置信度: 高

- [AUD-12618] [Major] 音频 clip 收集进 `a_clips` 后从不建轨：音频静默丢失，而报告条目却标 PRESERVED
  - 位置: src/features/edit/adapters/otio_adapter.py:105-106, 129-136, 138-144
  - 证据:
    ```python
        v_clips: list[ClipInstance] = []
        a_clips: list[ClipInstance] = []
        ...
                if track_kind == "video":
                    v_clips.append(clip)
                else:
                    a_clips.append(clip)
                entries.append(InterchangeEntry(
                    category=InterchangeEntryCategory.PRESERVED, element="clip",
                    description=f"imported otio clip {clip.clip_id}",
                ))
        ...
        seq = TimelineSequence(
            timeline_id="otio-seq", ...
            video_tracks=(v_track,) if v_track else (),
        )
    ```
  - 影响: 音频轨的 clip 全部进了 `a_clips`（含 PRESERVED 报告条目），但构造 `TimelineSequence` 时只建 video_tracks，`a_clips` 从未使用——音频内容整轨静默丢失且报告谎称保留，双重违反 TAEP §9.4。此外所有视频轨都折叠进单轨 "v1"，`track_idx` 未用。
  - 建议: 建 a1 音频轨；或至少为每条音频轨记 DROPPED/CONVERTED 条目；多轨道按 track_idx 建 v1..vN/a1..aN。
  - 置信度: 高

- [AUD-12619] [Major] OTIO 导出 `target_url=f"file:///{asset_id}"` 用内部 uuid 伪造 URL，任何第三方工具都无法重连媒体
  - 位置: src/features/edit/adapters/otio_adapter.py:173-177
  - 证据:
    ```python
                mr = otio.schema.ExternalReference(
                    target_url=f"file:///{clip.asset_id}",
                    available_range=src,
                )
    ```
  - 影响: `clip.asset_id` 是 Topos 工程内部素材 id（导入重映射后是 uuid），写出的引用形如 `file:///3f2a...`，磁盘上不存在。OTIO 交换的核心价值（外部工具按 target_url 找媒体）完全落空，且报告只有一条笼统的 PRESERVED "exported to OTIO"，未披露引用不可解析。
  - 建议: 导入上下文里查 `MediaAsset.path`（或让调用方传入 asset_id→path 映射）生成真实 file URL；查不到路径时记 REQUIRES_USER_ACTION。
  - 置信度: 高

- [AUD-12620] [Suggestion] MissingReference 的 `target_url` 为 `None` 时 `getattr` 默认值不生效，asset_id 变成字符串 `"None"`
  - 位置: src/features/edit/adapters/otio_adapter.py:121
  - 证据:
    ```python
                asset_id = getattr(getattr(child, "media_reference", None), "target_url", f"otio-{clip_idx}")
    ```
  - 影响: `getattr(obj, "attr", default)` 仅在属性**不存在**时用默认值；OTIO 的 `MissingReference.target_url` 属性存在但值为 `None`，于是 `asset_id = str(None) = "None"`——下游 offline 素材路径/external_ref 记为 "None"，报告与 relink 信息不可读且多个 clip 混到同一键。
  - 建议: `asset_id = getattr(mr, "target_url", None) or f"otio-{clip_idx}"`。
  - 置信度: 高

- [AUD-12621] [Suggestion] OTIO 导出静默丢弃音频轨/markers/transitions（无任何报告条目），写文件前也不建父目录（与 FCPXML 行为不一致）
  - 位置: src/features/edit/adapters/otio_adapter.py:165-181
  - 证据:
    ```python
        for track in sequence.video_tracks:
            otio_track = otio.schema.Track(name=track.name, kind="Video")
            ...
        otio.adapters.write_to_file(timeline, output_path)
    ```
  - 影响: 只遍历 `video_tracks`，audio/markers/transitions 不导出也不记 DROPPED/CONVERTED 条目（报告仅一条 "exported to OTIO"），违反本模块沿用的 §9.4 报告合同（FCPXML 导出侧对同类降级均有条目）；`write_to_file` 前无 `mkdir(parents=True)`，用户选新目录时直接抛 OSError（controller 侧有 except 兜底成错误弹窗，但与 FCPXML 成功路径行为不一致）。
  - 建议: 补降级条目；写文件前 mkdir。
  - 置信度: 高

- [AUD-12622] [Suggestion] `inspect` 的 `asset_count` 统计的是轨道数而非素材数
  - 位置: src/features/edit/adapters/otio_adapter.py:74
  - 证据:
    ```python
                asset_count=len(timeline.tracks) if hasattr(timeline, "tracks") else 0,
    ```
  - 影响: `timeline.tracks` 是 Stack（轨道集合），条数与素材数无关；inspect 面板显示的"资源数"误导用户。
  - 建议: 遍历 `timeline.each_clip()` 统计 media_reference（或 clip 数）。
  - 置信度: 高

- [AUD-12623] [Suggestion] `flatten_nested_sequence` 无环检测（循环引用触发 RecursionError）、忽略嵌套 clip 的 source_start 裁剪、只处理 video_tracks
  - 位置: src/features/edit/adapters/otio_adapter.py:218-239
  - 证据:
    ```python
        def _resolve_clips(track):
            new_clips = []
            for clip in track.clips:
                nested = sequences_by_id.get(clip.asset_id)
                if nested is not None and hasattr(nested, "video_tracks"):
                    offset = clip.timeline_start
                    for ntrack in nested.video_tracks:
                        for nclip in ntrack.clips:
                            from dataclasses import replace
                            new_clips.append(replace(
                                nclip,
                                clip_id=f"{clip.clip_id}_{nclip.clip_id}",
                                timeline_start=nclip.timeline_start + offset,
                            ))
    ```
  - 影响: `sequences_by_id` 含环（A 引 B、B 引 A，可来自导入的嵌套 OTIO）时无限递归 RecursionError；展平只加 `clip.timeline_start` 偏移，未消费外层 clip 的 `source_start`（对嵌套段的入点裁剪丢失）；嵌套的 audio_tracks 不参与展平。该函数当前仅测试引用（生产未接线），风险为潜伏型。
  - 建议: 递归传入 visited 集合检测环；展平时按 source_start 裁剪嵌套范围；audio 轨同法展平；`from dataclasses import replace` 移到模块级。
  - 置信度: 中

### 通过项
- 线程安全：无共享可变状态、无 Qt。
- GPU/GLSL/HDR/信号防递归：不涉及。
- 可选依赖处理：`_otio_available` 优雅降级（inspect/import 返回带诊断的 report，export 显式 RuntimeError），controller 侧入口预检（otio_available）已核实，未安装不阻塞主程序。
- OTIO inspect 的 `except Exception` 捕获后将错误信息写入 diagnostics 返回（非吞掉），符合错误显式化要求。
- `NESTED_SEQUENCE_POLICY` 仅策略常量文档，无逻辑风险。

---

## 批次总结

- Critical（2）：AUD-12600（FCPXML frameDuration 帧率/帧时长语义颠倒，真实 FCP 文件导入时间全错、导出 frameDuration 秒级错误）、AUD-12601（`_frames_to_time` 分母多乘 den，NTSC 导出时间缩小约 1001 倍，自家往返亦失败）。
- Major（9）：AUD-12602/12603/12604/12605/12606/12607（fcpxml_adapter）、AUD-12617/12618/12619（otio_adapter）。
- Suggestion（13）：AUD-12608–12611（fcpxml）、AUD-12612–12615（import_builder）、AUD-12616（interchange_types）、AUD-12620–12623（otio）。
- 共性结论：两处 Critical 均属"帧率有理数"的时间换算语义/公式错误，现有测试只做自产自读往返（写读同套错误函数），未对真实 Apple 样例（`100/2400s`）与 NTSC 帧率断言，故未暴露；报告合同（§9.4 绝不静默丢信息）在导入侧与 OTIO 导出侧多处未落实。

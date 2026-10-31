# 批次报告：batch-273 [P2] src/features/teaching_animation/core//

编号区间：AUD-27300–AUD-27399。审查日期：2026-09-28。
文件数：4，合计 1,912 行，全部通读。
模块性质：纯数据契约/模板模块（无 Qt 信号、无 OpenGL、无着色器代码），因此 GPU 生命周期、GLSL/HDR、信号防递归三个维度在本批次不适用或以"通过（无相关代码）"记录。

汇总：Critical 0 / Major 5 / Suggestion 11。

---

## src/features/teaching_animation/core/__init__.py (行数: 253)

纯 re-export 聚合出口：转出口 contracts/commands/model/migration/template_registry/layout/design_tokens/executable_template/builtin_templates/template_commands/html_layer_commands/motion_graphic_generator_registry/graphics/text_layout/html_layer/html_template/audio_plan/composition_schema/professional/style_schema/hierarchy/components/asset_provider/font_registry/validator 共 30 个子模块的类与函数，自身无运行逻辑（无逻辑，通过）。已核对同目录子模块均存在，导入无失效。

### 发现

- [AUD-27300] [Suggestion] `__all__` 与实际导入面严重不一致，约半数公共名未列入导出
  - 位置: src/features/teaching_animation/core/__init__.py:160-253
  - 证据:
    ```python
    from .layout import (
        AlignMode, AutoLayout, LayoutChild, LayoutDirection, Rect, SizingMode,
        resolve_grid, resolve_stack,
    )
    ...
    from .builtin_templates import (
        BUILTIN_TEMPLATES, CHECKLIST, COMPARISON, CTA, ...
    ```
    而 `__all__`（160-253 行，共 93 项）中不含 `Rect`、`BUILTIN_TEMPLATES`、`ExecutableTemplate`、`ApplyTemplateCommand`、`AudioClip`、`AssetDescriptor`、`Mask`、`Affine2d`、`FontRegistry`、graphics/text_layout/html_layer 系列等大量导入名。
  - 影响: `from ... import *` 与静态分析工具看到的公共 API 残缺；下游（含 AI 参数导出、模板宿主）依赖 `__all__` 时会漏掉可用符号，接口契约不稳定。
  - 建议: 将 `__all__` 与全部导入名对齐（或用脚本断言 `set(__all__) == set(导入名)`），移除双份维护漂移。
  - 置信度: 高

### 通过项
- 纯转出口，无文件 IO、无线程/GPU/信号相关逻辑，无副作用（通过，无逻辑）
- 导入的子模块均实际存在（已核对目录），`ParamSpec` 等被引用符号签名匹配

---

## src/features/teaching_animation/core/asset_provider.py (行数: 353)

### 发现

- [AUD-27301] [Major] `LocalAssetProvider.resolve` 每次未命中缓存即全量 `read_bytes()` 计算 sha256，无任何缓存
  - 位置: src/features/teaching_animation/core/asset_provider.py:269
  - 证据:
    ```python
    def resolve(self, asset_id: str) -> AssetDescriptor | None:
        ...
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ```
    配合 `AssetProviderRegistry.resolve` 只在首次命中后写入 `_descriptors` 缓存（第 167 行），每个新注册表实例对每个资产都要整读一次文件。
  - 影响: 4K 视频/长音频资产可达 GB 级，整读+哈希在调用线程上阻塞数秒；preflight 按资产逐个调用 resolve，若发生在交互路径（构建 provider 后立即 preflight）将卡顿 UI 或拖慢渲染准备阶段；内存峰值 = 文件大小。
  - 建议: 分块流式读取（`iter chunk` 喂 `sha256().update()`）；按 `(path, mtime, size)` 缓存哈希；或把哈希计算移到显式的离线审计步骤，resolve 仅在需要时惰性计算。
  - 置信度: 高（成本与代码路径确定；调用频率取决于宿主，中）

- [AUD-27302] [Major] `path.read_bytes()` 的 OSError 未处理，单文件 IO 错误可击穿整个 preflight
  - 位置: src/features/teaching_animation/core/asset_provider.py:269
  - 证据:
    ```python
    candidate = (self._root / raw_path).resolve()
    if candidate != self._root and self._root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None
    ...
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ```
    `_path` 用 `is_file()` 判存在（TOCTOU），此后 `read_bytes()` 无 try/except；权限不足、文件在判定后被删除/替换都会抛 OSError，沿 `provider.resolve → AssetProviderRegistry.resolve（第 165-168 行）→ preflight` 一路上抛。
  - 影响: 模块自述目标是"missing provider / unresolved asset 是可诊断错误而不是坏渲染"，但一个资产 IO 异常会让 `preflight` 整体崩溃（返回类型是问题元组，不是异常），诊断性丢失。
  - 建议: `read_bytes` 包 try/except OSError，失败时返回 `license=MISSING` 描述符（与现缺文件路径一致），让 preflight 产出结构化 issue 而非异常。
  - 置信度: 高

- [AUD-27303] [Major] 预览模式下缺失文件资产不产生任何 preflight 诊断
  - 位置: src/features/teaching_animation/core/asset_provider.py:251-258, 184-191
  - 证据:
    ```python
    if path is None:
        return AssetDescriptor(
            asset_id=asset_id,
            ...,
            license=AssetLicenseStatus.MISSING,
            source_uri=str(item.get("path", "")),
        )
    ```
    而 preflight 中：
    ```python
    if for_delivery and descriptor.license != AssetLicenseStatus.OK:
        issues.append(AssetPreflightIssue(
            asset_id, "license_blocks", ...))
    ```
    `license=MISSING` 仅在 `for_delivery=True` 时报告；`for_delivery=False`（预览）时 provider 存在、seek 只查帧区间，缺失文件的资产得到零 issue。
  - 影响: 场景引用的本地文件被移动/删除后，预览 preflight 显示"全部正常"，问题推迟到渲染阶段才暴露为坏渲染——与模块 docstring"diagnosable error instead of a broken render"的目标相悖。
  - 建议: 对 `AssetLicenseStatus.MISSING` 增加独立 code（如 `missing_file`）并在预览模式下也报告（可降级为非阻断 warning 级 issue）。
  - 置信度: 中（行为由代码确定；是否为有意设计存疑，docstring 表明非有意）

- [AUD-27304] [Suggestion] `AssetProviderRegistry` 注册与解析无并发保护
  - 位置: src/features/teaching_animation/core/asset_provider.py:143-169
  - 证据:
    ```python
    def register_provider(self, provider: AssetProvider) -> None:
        self._providers[provider.provider_id] = provider

    def resolve(self, asset_id: str) -> ...:
        ...
        for provider in self._providers.values():
            resolved = provider.resolve(asset_id)
            if resolved is not None:
                self._descriptors[asset_id] = resolved
    ```
    `_providers`/`_descriptors` 为普通 dict，无锁；`resolve` 边迭代 `_providers.values()` 边可能被其他线程 `register_provider`。
  - 影响: 若宿主在工作线程 preflight 的同时注册 provider（如延迟注册解码器），可能触发 "dict changed size during iteration" 的 RuntimeError；描述符缓存写入也可能竞争。
  - 建议: 加 `threading.Lock` 保护注册/解析，或在使用契约中明确"注册完成后才可解析"并断言。
  - 置信度: 中（竞争窗口取决于宿主注册时序）

- [AUD-27305] [Suggestion] `from_dict`/`resolve` 对持久化数据做 `int()`/`float()` 强转，无类型验证
  - 位置: src/features/teaching_animation/core/asset_provider.py:101-102, 265-266
  - 证据:
    ```python
    width=int(data.get("width", 0)), height=int(data.get("height", 0)),
    duration_frames=int(data.get("duration_frames", 0)), frame_count=int(data.get("frame_count", 1)),
    ...
    duration = int(metadata.get("duration_frames", item.get("duration_frames", 0)) or 0)
    frame_count = int(metadata.get("frame_count", item.get("frame_count", 1)) or 1)
    ```
    手工编辑/损坏的项目 JSON 中出现字符串数值（如 `"width": "big"`）时 `int()` 直接抛 ValueError，且未知 kind 静默降级为 RASTER（第 85-89 行 `_kind` 捕获后回退，无任何记录）。
  - 影响: 场景加载路径因单个畸形字段抛出不带上下文的 `invalid literal for int()`；kind 静默降级可能让资产走错解码通道。
  - 建议: 统一 `_to_int(value, default)` 容错转换并记录告警；未知 kind 至少在 metadata 里保留原始值。
  - 置信度: 高（代码路径确定，触发依赖外部数据）

- [AUD-27306] [Suggestion] `AssetProviderRegistry.resolve` 返回类型与实际不符，用 `type: ignore` 掩盖
  - 位置: src/features/teaching_animation/core/asset_provider.py:153, 163
  - 证据:
    ```python
    def resolve(self, asset_id: str) -> tuple[AssetDescriptor, AssetProvider] | None:
        ...
        return descriptor, None  # type: ignore[return-value]
    ```
  - 影响: 签名承诺 provider 非 None，调用方（如 preflight 第 189 行 `if provider is None`）必须处理 None——类型检查器与签名互相矛盾，掩盖了真实的 Optional 语义。
  - 建议: 返回类型改为 `tuple[AssetDescriptor, AssetProvider | None] | None`，删除 ignore。
  - 置信度: 高

- [AUD-27307] [Suggestion] 描述符缓存（含 sha256/license）永不失效
  - 位置: src/features/teaching_animation/core/asset_provider.py:154, 167
  - 证据:
    ```python
    descriptor = self._descriptors.get(asset_id)
    ...
    self._descriptors[asset_id] = resolved
    ```
    缓存无 TTL/失效机制；首次 resolve 后文件内容变化（替换素材、改许可证元数据）不会反映到后续 preflight。
  - 影响: 交付前审计可能基于过期哈希/许可证状态放行，或对已修复的文件继续误报。
  - 建议: 缓存键附带 `(mtime, size)` 校验，或提供显式 `invalidate(asset_id)`。
  - 置信度: 中

- [AUD-27308] [Suggestion] `MotionAssetPack.content_hash` 既无派生也无校验逻辑
  - 位置: src/features/teaching_animation/core/asset_provider.py:321, 325-332
  - 证据:
    ```python
    content_hash: str = ""
    ...
    def __post_init__(self) -> None:
        if not self.pack_id:
            raise ValueError("pack_id cannot be empty")
        if self.version <= 0:
            raise ValueError("pack version must be positive")
        ids = [a.asset_id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("pack asset ids must be unique")
    ```
    docstring 称"audited bundle with license + content hash"，但 `__post_init__` 只校验 id/版本，`content_hash` 可为任意串或空串且从不与 assets 实际内容比对。
  - 影响: 审计承诺（TM-07）形同虚设：篡改包内资产不会被 content_hash 发现。
  - 建议: 提供 `compute_pack_hash()`（对排序后的 asset sha256 汇总）并在加载审计路径中校验。
  - 置信度: 高（缺失为事实；是否属本批次范围外的后续计划标低——这里按当前代码判定）

### 通过项
- GPU 资源生命周期：纯元数据模块，注释与实现一致（不解码、不建 GPU 对象），无泄漏面
- GLSL/HDR 正确性：无着色器/HDR 代码
- 信号防递归：无 Qt 信号
- 路径安全：`_path` 拒绝绝对路径并做 resolve 后的根目录逃逸校验（第 238-244 行），符号链接经 `.resolve()` 收敛
- 状态提交一致性：不触碰 undo/redo/revision，供命令层组合使用
- 冻结 dataclass（AssetDescriptor/SeekResult/AssetPreflightIssue/MotionAssetPack）不可变，无共享可变状态
- `preflight` 的 for_delivery 许可证阻断逻辑（PREVIEW_ONLY/UNKNOWN/MISSING 均阻断）符合 TM-07
- 类型注解完整（除 AUD-27306 所述返回类型）

---

## src/features/teaching_animation/core/audio_plan.py (行数: 247)

### 发现

- [AUD-27309] [Major] `AudioRenderPlan.to_timeline` 裁剪后 fade_in+fade_out 可超过剩余时长，`replace` 触发 `__post_init__` ValueError
  - 位置: src/features/teaching_animation/core/audio_plan.py:151-158（配合 44-58 行校验）
  - 证据:
    ```python
    mapped.append(replace(
        clip,
        start_frame=timeline_start + overlap_start - scene_in_frame,
        duration_frames=duration,
        source_start_frame=clip.source_start_frame + skipped,
        fade_in_frames=max(0, min(clip.fade_in_frames - skipped, duration)),
        fade_out_frames=max(0, min(clip.fade_out_frames, duration)),
    ))
    ```
    而 `AudioClip.__post_init__`（第 57-58 行）要求：
    ```python
    if self.fade_in_frames + self.fade_out_frames > self.duration_frames:
        raise ValueError("fade in+out cannot exceed clip duration")
    ```
    复现：clip `duration_frames=100, fade_in=50, fade_out=50`（合法），`to_timeline(scene_in_frame=0, scene_out_frame=60)` → duration=60、skipped=0 → fade_in=min(50,60)=50、fade_out=min(50,60)=50 → 100 > 60 → 抛 ValueError。
  - 影响: 完全合法的音频计划在场景裁剪（timeline 入出点缩短）时崩溃，阻断"场景→时间线"映射与导出路径；两 fade 各自 clamp 但未联合约束。
  - 建议: 联合压缩，如 `fade_in = min(fade_in, duration // 2)`、`fade_out = min(fade_out, duration - fade_in)`，或按比例缩放两者；并补该边界单测。
  - 置信度: 高（`dataclasses.replace` 会重新走 `__init__`→`__post_init__`，已数值复算）

- [AUD-27310] [Suggestion] `AudioClip.from_dict`/`AudioRenderPlan.from_dict` 对持久化数据强转无验证，`clips` 非 list 时静默清空
  - 位置: src/features/teaching_animation/core/audio_plan.py:94-103, 170
  - 证据:
    ```python
    start_frame=int(data.get("start_frame", 0)), duration_frames=int(data.get("duration_frames", 1)),
    gain=float(data.get("gain", 1.0)),
    ...
    clips=tuple(AudioClip.from_dict(c) for c in data.get("clips", []) if isinstance(c, Mapping)),
    ```
    `int()/float()` 对畸形字符串抛裸 ValueError；`clips` 若为字符串，逐字符过滤后得到空元组，音频静默丢失（与"不得静默丢弃"的模块声明相悖）。
  - 影响: 手工编辑/损坏的场景 metadata 在 `read_audio_render_plan`（第 221-226 行，无 try/except）处抛出难定位异常，或更糟——clips 类型错误时音频无提示消失。
  - 建议: 容错转换器 + 对 `clips` 非 list 的情况显式抛错或记录诊断。
  - 置信度: 高

### 通过项
- 线程安全：全部 frozen dataclass，不可变，无共享可变状态
- GPU/GLSL/HDR/信号：无相关代码
- 输入验证（构造期）：`AudioClip.__post_init__` 覆盖空 id、负帧、fade 越界、gain/duck_amount 范围；`AudioRenderPlan` 校验采样率、master_gain、clip id 唯一性
- 增益包络数学：`gain_at` 在 fade 边界（第 0 帧、尾帧、fade 重叠区）行为正确，float 除法无整数截断问题
- `to_timeline` 的 seek 偏移保留（`source_start_frame + skipped`）与窗口裁剪逻辑（除 AUD-27309 外）正确
- `preflight_audio`：missing/unsupported/out_of_range/silent_fallback 判定自洽，`blocking` 语义正确；`scene_duration_frames<=0` 有显式校验
- 类型注解完整

---

## src/features/teaching_animation/core/builtin_templates.py (行数: 1059)

### 发现

- [AUD-27311] [Major] steps 模板声明 `columns` 参数但构建函数从未读取，参数静默失效
  - 位置: src/features/teaching_animation/core/builtin_templates.py:209（声明）, 175-177（实际逻辑）
  - 证据:
    ```python
    ParamSpec("columns", "integer", default=0, description="Max columns (0 = auto)."),
    ```
    而 `_build_steps` 中：
    ```python
    columns = min(len(parsed), 4)
    card_children = [LayoutChild(f"card_{i + 1}", ...) for i in range(len(parsed))]
    card_rects = resolve_grid(card_children, grid_canvas, columns=columns, gap=24.0)
    ```
    全文 grep 确认无 `params.get("columns")`（唯一出现即声明处与局部变量）。
  - 影响: 参数 schema 用于 UI/AI 导出（ParamSpec 注释自述），用户或 AI 设置 `columns=2/3` 完全无效，布局始终按 `min(N,4)` 渲染——功能性缺陷且破坏可参数化契约的可信度。
  - 建议: 读取 `int(params.get("columns") or 0)`，`<=0` 时回退 `min(len(parsed), 4)`；或删除该 ParamSpec。
  - 置信度: 高

- [AUD-27312] [Suggestion] title_card 的 scene_id 直接切片用户标题：None 会 TypeError，且未清洗、可重复
  - 位置: src/features/teaching_animation/core/builtin_templates.py:68
  - 证据:
    ```python
    scene_id=f"tpl_title_{params.get('title', 'x')[:8]}".replace(" ", "_"),
    ```
    同函数其他用途均包 `str()`（第 64、69 行），唯独此处对原始值切片。
  - 影响: 调用方显式传 `{"title": None}`（UI 空值常见编码）即抛 `TypeError: 'NoneType' object is not subscriptable`；标题含 `/`、`#` 等字符会进入 scene_id；前 8 字符相同的不同标题产生重复 scene_id。
  - 建议: `str(params.get("title") or "x")` 后再切片，并过滤到 id 安全字符集；重复冲突可加短哈希后缀。
  - 置信度: 高（行为确定；None 输入是否出现取决于宿主）

- [AUD-27313] [Suggestion] data_chart 对负值与过小 `max_value` 的处理产生错误柱形/溢出
  - 位置: src/features/teaching_animation/core/builtin_templates.py:422-426, 448-449
  - 证据:
    ```python
    max_value = float(params.get("max_value", 0) or 0)
    if max_value <= 0:
        max_value = max(values)
    if max_value <= 0:
        max_value = 1.0
    ...
    h = max(1.0, (v / max_value) * chart.height)
    ```
    负值 v 一律得到 1px 向上柱（应在基线下方或显式报错）；用户给的 `max_value` 小于数据最大值时 `h > chart.height`，柱形越过标题区无钳制。
  - 影响: 教学场景常见的负数数据（温差、差值）渲染成视觉错误的 1px 柱；手动 max_value 误填时布局溢出。
  - 建议: 对 `v < 0` 要么校验拒绝要么向基线下绘制；`h = min(h, chart.height)` 钳制并在数据超界时报告诊断。
  - 置信度: 高（几何计算确定，影响为视觉层面）

- [AUD-27314] [Suggestion] 列表型模板不限制条目数，纵向内容可无提示溢出画布
  - 位置: src/features/teaching_animation/core/builtin_templates.py:756-766（checklist，另见 formula_proof 628-637、product_feature 935-943、comparison 523-530、process_flow 340-353）
  - 证据:
    ```python
    cursor_y = title_rect.y + title_rect.height + 16.0
    for index, item in enumerate(items, start=1):
        ...
        cursor_y += row_h + gap
    ```
    循环无可用高度约束；process_flow 垂直方向 `cell_h = max(40.0, ...)` 钳下限后 cursor 累加同样越界。
  - 影响: 条目多时（如 20 条 checklist）元素延伸到画布外被裁切，无分页、无告警，模板输出看起来"缺内容"。
  - 建议: 计算可用高度并在溢出时截断+生成溢出指示（"+N more"）或直接 raise 供上层提示。
  - 置信度: 高（逻辑确定；属设计取舍标为 Suggestion）

- [AUD-27315] [Suggestion] 以 `len(text)*常数` 估算文本宽度，CJK/宽字符会溢出胶囊与按钮
  - 位置: src/features/teaching_animation/core/builtin_templates.py:877, 992
  - 证据:
    ```python
    tag_w = max(80.0, len(tag_text) * 14.0 + 32.0)
    ...
    btn_w = max(160.0, len(btn_label) * 18.0 + 64.0)
    ```
    本包 text_layout 已提供 `cjk_aware_measurer`，此处未用。
  - 影响: 中文标签每字符实际宽度接近字号（16-20px），按 14/18px 估宽必然偏窄，tag 文本溢出胶囊、按钮文字溢出按钮——对中文教学场景（本项目核心场景）常见。
  - 建议: 复用 `cjk_aware_measurer`（或按 CJK 字符计 2 倍宽度）估算宽度。
  - 置信度: 中（宽度常数与实际字号的差距未实测，但方向确定）

### 通过项
- 线程安全：模块级模板常量为冻结数据class；`create_builtin_template_registry()` 每次新建注册表，无共享可变状态
- GPU/GLSL/HDR/信号：无相关代码
- 输入验证：`_split_list`/`_parse_numbers`/`_split_events` 对错误类型显式 raise ValueError；steps/checklist/timeline 空列表显式拒绝；`ParamSpec.enum` 用于 direction/pointer_direction 且构建函数对未知值有 else 兜底（第 816-817 行）
- 几何退化保护：`_element` 对宽高 `max(1.0, ...)` 钳制（第 30 行）；tutorial_callout 的 box_h 有下限路径
- 元素 id 唯一性：各模板内 element_id 命名无冲突（title/card_N/left_N/step_N 等前缀隔离）
- z_index 层级一致（背景/边框/文字分层正确）
- 类型注解完整（`_accent` 返回 Any 为小瑕疵，不单列）
- 浮点字面量规范（全部带 .0）

---

## 批次汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-27301, AUD-27302, AUD-27303, AUD-27309, AUD-27311 |
| Suggestion | 11 | AUD-27300, AUD-27304, AUD-27305, AUD-27306, AUD-27307, AUD-27308, AUD-27310, AUD-27312, AUD-27313, AUD-27314, AUD-27315 |

优先修复顺序建议：AUD-27309（合法数据即崩溃）→ AUD-27311（参数契约失效）→ AUD-27301/27302（资产 IO 阻塞与击穿）→ AUD-27303（诊断缺失）。

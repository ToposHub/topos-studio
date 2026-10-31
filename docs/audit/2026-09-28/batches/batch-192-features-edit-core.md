# 批次报告：batch-192 — src/features/edit/core（转场/标题模板/工作区预设）

任务区间：AUD-19200–AUD-19299。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
本批 7 个文件均为无 Qt 依赖的纯逻辑/数据模块，无信号、无 undo/redo 提交点；GPU 相关文件为 pixel-free 描述（`GpuTransitionMaskSpec`），不持有 GL 资源，符合分层约束。

---

## src/features/edit/core/title_templates.py (行数: 163)

### 发现
- [AUD-19200] [Suggestion] 用户模板存储目录依赖 `os.getcwd()`，换启动目录后模板"消失"
  - 位置: src/features/edit/core/title_templates.py:36
  - 证据:
    ```python
    def title_templates_dir() -> str:
        """用户标题模板目录（不存在则创建）。"""
        path = os.path.join(os.getcwd(), "Presets", "User", "title_templates")
        os.makedirs(path, exist_ok=True)
        return path
    ```
  - 影响: 用户已保存的标题模板落在"启动时工作目录"下。从 Finder/服务/不同终端目录启动（cwd 常为 `/` 或用户主目录）时，同一份用户数据定位到另一目录，模板列表为空，用户视角等同数据丢失（实际文件仍在旧 cwd）。
  - 建议: 改为稳定锚点（应用数据目录 / 用户主目录 / 项目根解析函数），并做一次性旧目录迁移。注：`custom_transitions.py:85` 的 `Presets/User/transitions` 是同一模式（超出本批文件，建议一并治理）。
  - 置信度: 高

- [AUD-19201] [Suggestion] `save_title_template` 失败路径残留 `.tmp` 文件；`vars(style)` 兜底可能抛未处理 TypeError
  - 位置: src/features/edit/core/title_templates.py:133-140
  - 证据:
    ```python
        "style": style.to_dict() if hasattr(style, "to_dict") else vars(style),
    }
    path = os.path.join(title_templates_dir(), f"{preset_id}.json")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    ```
  - 影响: `json.dump` 对不可序列化对象抛 TypeError、磁盘满抛 OSError 时，`os.replace` 不会执行且 `{preset_id}.json.tmp` 残留在 Presets 目录（`list_user_title_templates` 按 `.json` 后缀过滤，不影响加载，但垃圾文件累积）。异常直接穿透给 UI 调用方。
  - 建议: try/except 包裹写段，失败时 `os.remove(tmp)`（best-effort）；`vars()` 兜底分支实际不可达（`CaptionStyle.to_dict` 恒存在，已核实 captions/model.py:253），可删除或改为显式报错。
  - 置信度: 高

### 通过项
- 输入验证：`_parse_template` 对坏 JSON/缺 label/坏 style 逐文件容错跳过并记日志，宽 except 有 noqa 说明且不吞 KeyboardInterrupt 类系统错误路径（属单文件容错设计，符合规范）
- 原子写：`os.replace(tmp, path)` 保证落盘原子性
- 路径安全：`_slug` 过滤 `[^0-9A-Za-z_-]`，`delete_title_template` 无路径穿越风险
- 线程安全：仅 UI 线程读写本地 JSON，无共享可变状态
- 状态一致性：无 undo/redo 参与点；文件即持久化，保存/删除语义完整
- 类型注解完整

---

## src/features/edit/core/transition_catalog.py (行数: 173)

### 发现
- [AUD-19202] [Major] `dip_to_color` 是合法用户转场 base，但空间/composite 判定不继承它——基于 dip 的用户转场在预览与导出中均退化为普通溶解，dip 效果不渲染
  - 位置: src/features/edit/core/transition_catalog.py:153-157
  - 证据:
    ```python
    def is_composite_transition(transition_type: str) -> bool:
        """Return whether a transition needs a two-source composite pass."""
        return transition_type in COMPOSITE_TRANSITION_TYPES or is_spatial_transition(
            transition_type
        )
    ```
    交叉证据（调用方，均经核实）：`custom_transitions.py:44-57` 允许 `dip_to_color` 作 base 但 `_SPATIAL_BASES` 不含它；`timeline_export.py:6763` 与 `timeline_preview_renderer.py:2698` 均以 `trans.transition_type not in COMPOSITE_TRANSITION_TYPES: continue` 过滤（`user_*` 类型不在该冻结集合内）；dip 颜色 pass 以字符串相等判断（`timeline_preview_renderer.py:1810` `if transition_type == "dip_to_color"`），用户类型不匹配；GPU 判定 `timeline_export.py:7445-7451` 同样只检查内置集合 + `is_user_transition_spatial(...) is True`。
  - 影响: 用户按 C2 文档创建 `base: "dip_to_color"` 的自定义转场后：CPU 双源预合成循环跳过它 → 只剩 `transition_map` 的逐侧 opacity 交叉淡化；GPU 路径因它不在 `safe_single_source_transitions` 也不在双源判定内而整帧回退 CPU。结果：转场渲染为普通 cross dissolve，dip-to-color 效果完全丢失（功能缺陷，输出与用户配置不符）。
  - 建议: 在 `is_composite_transition`/`is_full_frame_transition` 中统一解析用户转场 base（新增 `is_user_transition_dip` 或返回 base 类别），并让预览/导出/GPU 三处过滤复用集中判定，替代各自内联的集合判断。
  - 置信度: 高（机制三处调用点均已 Read 核实；归类 Major 而非 Critical：仅影响用户自定义 dip base 这一子集功能，主路径渲染正确）

- [AUD-19203] [Suggestion] `GPU_FULL_FRAME_TRANSITION_TYPES` 与 `FULL_FRAME_TRANSITION_TYPES` 完全重复且全库无引用
  - 位置: src/features/edit/core/transition_catalog.py:111-116
  - 证据:
    ```python
    GPU_FULL_FRAME_TRANSITION_TYPES = frozenset({
        "blur_dissolve",
        "defocus_dissolve",
        "film_dissolve",
        "smooth_cut",
    })
    ```
  - 影响: 与 `FULL_FRAME_TRANSITION_TYPES`（105-110 行）逐项相同，属复制粘贴定义；全库 grep 无任何引用（死代码）。将来新增全画面转场只改其一即造成 GPU/CPU 目录漂移——正是本模块 docstring 声明要防止的问题。
  - 建议: 删除该常量，GPU 侧直接复用 `FULL_FRAME_TRANSITION_TYPES`；若确需分集合，从同一来源推导。
  - 置信度: 高

- [AUD-19204] [Suggestion] `is_composite_transition` / `is_full_frame_transition` 全库无调用者，实际判定逻辑被调用方内联重复
  - 位置: src/features/edit/core/transition_catalog.py:148-157
  - 证据:
    ```python
    def is_full_frame_transition(transition_type: str) -> bool:
        """Return whether a transition composes both sources over the full frame."""
        return transition_type in FULL_FRAME_TRANSITION_TYPES
    ```
    （grep src+tests+scripts 仅命中定义处；`timeline_export.py:6763`、`timeline_preview_renderer.py:2698`、`timeline_export.py:7445-7451` 各自手写集合判断）
  - 影响: 集中判定函数形同虚设，三处内联实现已开始漂移（见 AUD-19202 中用户转场处理不一致），维护风险明确。
  - 建议: 修复 AUD-19202 的同时让三个调用点改用集中 helper，消除内联重复。
  - 置信度: 高

### 通过项
- 输入验证：`transition_label` 对未知类型回退 type id 本身，不抛异常
- `is_spatial_transition` 的函数级 import 规避了循环依赖；调用频率为每转场每帧一次（非每像素），sys.modules 缓存后开销可忽略
- frozen dataclass + `field(default_factory=dict)` 无可变默认值共享问题
- 类型注解完整

---

## src/features/edit/core/transition_effects.py (行数: 525)

### 发现
- [AUD-19205] [Suggestion] 高斯 sigma 上限 96 与 kernel 上限 127 不匹配，大模糊被截断归一化
  - 位置: src/features/edit/core/transition_effects.py:385-391 与 245
  - 证据:
    ```python
    sigma = min(
        96.0,
        blur_ratio * min(first.shape[0], first.shape[1]) * pulse ** 0.72,
    )
    ...
    blurred_first = _gaussian_blur_frame(first, sigma)
    ```
    而 `_gaussian_blur_frame`（245 行）`kernel_side = max(3, min(127, int(math.ceil(sigma * 6.0)) | 1))`。
  - 影响: sigma=96 按 6σ 准则需要 ~577 宽 kernel，被钳到 127；OpenCV 在给定 kernel 内截断并重新归一化，实际模糊强度显著弱于 sigma 声称值，defocus/blur dissolve 在大帧、高峰值进度时"糊不开"，转场观感弱于设计意图（质量缺陷，非崩溃）。
  - 建议: 两者用同一上限推导（如 `sigma_cap = 127/6`），或 kernel 用可分离两次高斯逼近大 sigma。
  - 置信度: 中

- [AUD-19206] [Suggestion] `_mix_effect`/`_apply_radial_blur`/`_apply_rgb_split`/`_blend_transition_frames` 未校验输入 ndim，与 `_apply_directional_motion_blur` 防护不一致
  - 位置: src/features/edit/core/transition_effects.py:150 与 411-422 与 471-480（对照 64 行）
  - 证据:
    ```python
    # 64 行（有防护）：
    if frame is None or getattr(frame, "ndim", 0) != 3 or frame.shape[2] < 3:
        return frame
    # 150 行（无防护）：
    source_color = source[..., :3].astype(np.float32)
    ```
  - 影响: 2D 灰度帧进入无防护函数时 `source[..., :3]` 退化为行切片（形状 (H,3)），后续 warp/混合输出静默错乱而非报错。当前两个调用点（timeline_export.py:6825、timeline_preview_renderer.py:2746）在调用前有形状对齐/`_match_frames`，主路径不可达；但 API 为模块导出函数，防护不一致埋雷。
  - 建议: 在各公共/私有效果入口统一 `ndim == 3` 早退检查，或抽公共 `_as_3ch` 前置。
  - 置信度: 中

### 通过项
- HDR 正确性：float 路径不 clamp 到 [0,1]（127-141、138-141、216-219 行），仅整数 dtype 按 iinfo 量化，符合"HDR 内部数据不得无理由 clamp"铁律；`_blend_transition_frames` 的 2.2 gamma 编解码有注释说明且为近似设计
- 异常路径：`_apply_smooth_cut` 捕获 `(cv2.error, ValueError)` 回退普通混合、`smooth_cut` 有 ImportError 回退（368-380 行），保证坏帧不炸导出；`_float_param` 统一 NaN/Inf/类型容错（35-45 行）
- 输入验证：形状不一致抛 ValueError（188-189 行）；progress 全部 clamp+容错
- 性能：光流分析限制在 ≤320px 缩略图（279-281 行）后上采样；radial 采样数 3-9 有界；`import cv2` 惰性加载
- 线程安全：纯函数无共享状态
- 类型注解完整

---

## src/features/edit/core/transition_mask.py (行数: 154)

### 发现
- [AUD-19207] [Major] 未知/无顶点类型回退到硬边方向 wipe，与 GPU 侧同类输入返回 None 不一致；full-frame 常数 mask 保护未覆盖 `cross_dissolve`/`dip_to_color`
  - 位置: src/features/edit/core/transition_mask.py:141-144
  - 证据:
    ```python
        vertices = _shape_vertices(transition_type)
        if vertices.size == 0:
            return compute_wipe_mask(width, height, progress, params)
    ```
    而 99-103 行注释明确常数 mask 的目的是防止"generic mask consumers 意外把它们变成方向/形状 wipe"：
    ```python
    if transition_type in FULL_FRAME_TRANSITION_TYPES:
        # Full-frame dissolves are composed by transition_effects.py.  This
        # constant mask keeps generic mask consumers from accidentally
        # turning them into a directional or shape wipe.
        return np.full((height, width), progress, dtype=np.float32)
    ```
  - 影响: `cross_dissolve`、`dip_to_color`（同为无几何 mask 的溶解类）不在 `FULL_FRAME_TRANSITION_TYPES` 保护集内，直接调用本函数会得到"从左擦入"的硬边 wipe——与函数语义和 GPU 侧（`build_gpu_transition_mask_spec` 对同类输入返回 None=无 mask）直接矛盾。经核实当前两个调用点（timeline_export.py:6763、timeline_preview_renderer.py:2698 均先经 `COMPOSITE_TRANSITION_TYPES` 过滤）不会传入这些类型，属潜伏 API 契约缺陷；任何新调用点（含 C2 文档声明的参数化渲染入口）传入即产生渲染错误。用户 `dip_to_color` base 经 `merge_user_transition_params` 解析后递归进入本分支同样落 wipe（当前同样被上游过滤拦截）。
  - 建议: 无顶点且非已知几何类型的输入回退 `np.full((h,w), progress)`（与 full-frame 分支一致）而非 wipe；或显式抛错迫使调用方归类。
  - 置信度: 中（不一致性与代码证据确凿；当前主调用路径不可达，故不升 Critical）

- [AUD-19208] [Suggestion] `_polygon_mask` 顶点级 Python 循环 × 全帧临时数组，每帧重算无缓存
  - 位置: src/features/edit/core/transition_mask.py:37-43（对照 70 行 heart 48 顶点）
  - 证据:
    ```python
    for ax, ay, bx, by in zip(x0, y0, x1, y1):
        crosses = (ay > y) != (by > y)
        denominator = by - ay
        safe_denominator = denominator if abs(float(denominator)) > 1e-8 else 1e-8
        edge_x = (bx - ax) * (y - ay) / safe_denominator + ax
        inside ^= crosses & (x < edge_x)
    ```
  - 影响: `iris_heart` 48 顶点 → 每帧 48 次迭代、每次多个 HxW 布尔/浮点临时数组；预览在 viewport 分辨率可承受，4K 导出时转场区间内每帧全分辨率重算（4K float32 单张 ~33MB，每帧多倍临时），CPU 合成路径的确定性热点。几何仅依赖 (w,h,type,progress)，可复用 wipe 侧的缓存思路。
  - 建议: 顶点循环前移到低分辨率/浮点精度不受影响的实现（如 cv2.fillPoly 上采样）、或对 (w,h,type) 缓存单位网格、或按 progress 级别缓存。
  - 置信度: 中

### 通过项
- 用户转场递归安全：base 受 `custom_transitions._BASE_TYPES` 白名单约束（仅内置类型），递归必一层终止，无环
- GPU/CPU 几何一致性：slide 注入 direction、zoom/spin 走 iris_circle、effect 类型走方向 wipe，与 `build_gpu_transition_mask_spec` 逐分支对齐（已交叉核对）
- 端点处理：progress 0/1 返回常数 0/1 mask，防浮点角点泄漏
- 输入验证：progress clamp；wipe 参数规整化委托 `get_wipe_params`
- 线程安全：纯函数；共享缓存数组只读（见 transition_wipe 通过项）
- 类型注解完整

---

## src/features/edit/core/transition_mask_gpu.py (行数: 192)

### 发现
- [AUD-19209] [Suggestion] 跨模块导入私有符号 `_shape_vertices`
  - 位置: src/features/edit/core/transition_mask_gpu.py:8
  - 证据:
    ```python
    from src.features.edit.core.transition_mask import _shape_vertices
    ```
  - 影响: 依赖他人模块的下划线私有实现；`transition_mask.py` 若重构 `_shape_vertices`（改名/改返回类型）会在运行时 ImportError/AttributeError，静态检查不报。
  - 建议: 在 `transition_mask.py` 将其升为公开 `shape_vertices`（或提供 `get_transition_vertices()` 公共入口），GPU 侧导入公开名。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：纯数据描述（pixel-free spec），不创建/持有 GL 对象，无释放问题，符合 deferred-release 架构
- GLSL/HDR 正确性：`_float_param` 拒 NaN/Inf（51 行）；progress/softness/mix 等全部 clamp 到 shader 假设的域；顶点数 >48 拒绝（防 uniform 越界），heart 恰 48 顶点通过
- 用户转场：先 merge 再递归 base，与 CPU 路径一致；slide/effect 的 direction 注入顺序（spread 后覆盖）保证方向不被实例参数改写
- 输入验证：wipe direction 经 `get_wipe_params` 规整后查表，5 键闭合无 KeyError
- 异常路径：`transition_type = str(transition_type or "")` 防 None/空
- 类型注解完整

---

## src/features/edit/core/transition_wipe.py (行数: 168)

### 发现
- [AUD-19210] [Major] `get_wipe_params` 对 `angle`/`softness` 的 `float()` 无异常防护，坏外部参数可崩渲染
  - 位置: src/features/edit/core/transition_wipe.py:46-51
  - 证据:
    ```python
    angle = float(p.get("angle", 0.0))
    # Normalise angle to [0, 360)
    angle = angle % 360.0
    softness = float(p.get("softness", 0.02))
    softness = max(0.0, min(0.5, softness))
    ```
  - 影响: `params` 混入来源含用户自定义转场 JSON（`custom_transitions.reload_user_transitions` 只校验 params 是 dict，不校验值类型，经 `merge_user_transition_params` 原样合并传入）及持久化的实例参数。`"angle": "45deg"` 或 `"softness": null` 之类值使 `float()` 抛 ValueError/TypeError → `compute_wipe_mask` → 预览帧渲染/导出中断。同项目 `transition_effects._float_param`（35-45 行）已示范安全模式，此处未用；`direction`/`reverse` 均已防护，唯这两个数值键裸转。
  - 建议: angle/softness 改用与 `_float_param` 等价的 try/except + isfinite + 默认值逻辑（或在用户转场加载时做数值类型校验）。
  - 置信度: 中（数据流已核实；触发需手写坏 JSON/坏持久化值）

- [AUD-19211] [Suggestion] `lru_cache` 缓存全分辨率数组，`_angle_coord` 以连续 float 角度为键
  - 位置: src/features/edit/core/transition_wipe.py:131-141 与 144-168
  - 证据:
    ```python
    @lru_cache(maxsize=16)
    def _axis_coord(width: int, height: int, horizontal: bool) -> np.ndarray:
    ...
    @lru_cache(maxsize=16)
    def _angle_coord(width: int, height: int, angle_deg: float) -> np.ndarray:
    ```
  - 影响: 4K 导出时单张 HxW float32 ~33MB；`_angle_coord` 按精确 float 角度值作键，角度参数被拖动/动画时会连续生成新键，16 张上限 ≈ 530MB 驻留（实际常见 2-4 张，`_axis_coord` 键空间仅 2/resolution，风险主要在 angle 变体多的工程）。缓存只增不减（无失效钩子）。
  - 建议: angle 键量化到 0.1° 粒度；或导出用毕显式 `cache_clear()`；或 maxsize 按字节预算设计。
  - 置信度: 低

### 通过项
- 输入验证（除上述两键）：direction 双拼写回退 + 白名单、reverse bool 化、softness clamp [0,0.5]、feather 除零防护（max(1.0,…)）
- 端点语义：progress 0/1 返回常数 mask，注释明确 feather 不外溢边界帧
- 缓存共享安全：两个缓存函数将返回数组 `writeable=False`，防止调用方污染缓存
- 数值稳定：`_angle_coord` span<1e-6 退化处理
- 线程安全：纯函数 + 只读缓存数组
- 类型注解完整

---

## src/features/edit/core/workspace_presets.py (行数: 262)

### 发现
（无）

### 通过项
- 通过（无逻辑缺陷）：纯声明式数据模块，不依赖 Qt，符合"声明/解释分离"分层
- 输入验证：`__post_init__` 校验完备——未知/未托管 dock、visible_docks 重复、缺 dock_areas 条目、非法 area 值、tabify 组 <2 成员、组含不可见 dock、focus_dock 不可见均抛带 preset_id 的 ValueError（77-119 行），四个内建预设构造期即被验证
- 一致性：`EDIT_WORKSPACE_MANAGED_DOCKS` 覆盖全部预设引用的 dock（逐一核对）；`_TIMELINE_HEIGHT` 与注释约定一致，Review 预设 300 ≥ 280
- 不可变正确性：frozen dataclass + Tuple 类型，无可变共享
- 查询 API：`get_workspace_presets` 未知 id 返回 None，不抛错
- 类型注解完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-19202, AUD-19207, AUD-19210 |
| Suggestion | 9 | AUD-19200, AUD-19201, AUD-19203, AUD-19204, AUD-19205, AUD-19206, AUD-19208, AUD-19209, AUD-19211 |

跨文件主题：3 个 Major 中 2 个（AUD-19202 / AUD-19207）同根——转场类型分类（composite/full-frame/spatial）对用户自定义转场的 base 继承不完整、且集中判定函数无人使用导致预览/导出/GPU 三处内联漂移，建议合并修复。

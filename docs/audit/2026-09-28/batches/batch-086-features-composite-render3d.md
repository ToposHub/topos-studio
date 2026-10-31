# batch-086 审查报告 — src/features/composite/render3d（核心 5 文件）

- 批次：batch-086 [P1] 主题 render3d 核心（animation / aov / camera_model / cryptomatte / __init__）
- 编号区间：AUD-8600–AUD-8699（本报告实际使用：AUD-8601 – AUD-8608）
- 审查日期：2026-09-28
- 文件数：5（合计 1,055 行），全部 Read 通读；关键结论已交叉核对调用方
  （src/features/composite/nodes/model3d_node.py、scene3d_node.py、camera3d_node.py，只读）与
  tests/composite/test_3d_animation.py、test_3d_aov.py、test_cryptomatte_select.py。
- 统计：Critical 0 / Major 2 / Suggestion 6

---

## src/features/composite/render3d/__init__.py (行数: 10)

### 发现
- 无（纯 docstring，无 import、无导出、无逻辑）。docstring 声明的 math_utils / primitives
  模块均实际存在于同目录（已 ls 核对），描述与现状一致。

### 通过项
- 通过（无逻辑）：仅包说明文档，不含任何可执行代码，无资源/线程/异常面。

---

## src/features/composite/render3d/animation.py (行数: 327)

### 发现
- [AUD-8601] [Major] AnimationState.update 的 finished 判定未应用 end_frame 裁剪，与 remap_frame_to_anim_time 结果不一致
  - 位置: src/features/composite/render3d/animation.py:172-178（对照 :81-82 的 remap 裁剪）
  - 证据:
    ```python
    # remap_frame_to_anim_time 内（:81-82）：
    if end_frame is not None and frame > end_frame:
        frame = end_frame
    # 而 update 内的 finished 判定用未裁剪的 frame 重算 raw（:172-178）：
    comp_time = max(0.0, (frame - self.start_frame) / fps if fps > 0 else 0.0)
    raw = comp_time * self.speed + self.time_offset
    if self.play_mode == PLAY_MODE_ONCE and raw >= clip_duration:
        self.finished = True
    else:
        self.finished = False
    ```
  - 影响: once 模式 + end_frame 裁剪时自相矛盾。例：fps=24、duration=5s、end_frame=48（映射
    2.0s），frame=200 时 remap 把 frame 钳到 48 → anim_time=2.0（动画中段），但 finished 判定用
    raw=8.33s≥5s → finished=True。docstring 明言 finished 用于「UI 可据此切回 bind pose」，即
    动画仍应显示中段姿势时被提前判定播完。另：模块 docstring（:12-15）声称
    model3d_node/scene3d_node/composite_page 由 AnimationState 驱动，实际 src 内无任何生产调用
    （model3d_node.py:665-672 直接调 remap_frame_to_anim_time，AnimationState 仅测试引用），
    属潜伏缺陷 + 文档与现实不符。
  - 建议: finished 判定复用 remap 的裁剪逻辑（先钳 frame 到 end_frame 再算 raw），或直接令
    finished = (anim_time 已达 clip_duration 且被钳制)；同时修正模块 docstring 中关于调用方的描述。
  - 置信度: 高（行为经反例推演确认；当前无生产调用方，影响为潜伏）

- [AUD-8602] [Suggestion] AnimationSamplerCache 以 id(clip) 作为缓存 key，依赖 CPython id 复用风险与调用方自觉 clear
  - 位置: src/features/composite/render3d/animation.py:253
  - 证据:
    ```python
    qt = self._quantize_time(anim_time)
    key = (id(clip), str(clip.name), qt)
    entry = self._cache.get(key)
    ```
  - 影响: CPython 会在对象释放后复用 id。旧 clip 释放、新 clip 恰好分配到同一地址且 key 中
    同名同量化时间时，会命中陈旧条目返回错误的骨骼矩阵（渲染错误）。当前唯一生产调用方
    model3d_node.py:428/:1147 在模型切换/释放时显式 clear()，实际风险被缓解；但模块自身契约
    脆弱（注释 :227-228 已自知 name 不唯一才引入 id），任何新调用方换 clip 不 clear 即踩坑。
  - 建议: 用弱引用（weakref.ref）持有 clip 或在 key 中加入单调递增的代际计数 / clip 内容签名；
    或在 API 文档中把「换 clip 必须 clear」提升为硬性契约。
  - 置信度: 中（机制确定，现实触发概率低）

- [AUD-8603] [Suggestion] 模块级 logger 从未使用，且以 __import__("logging") 非常规方式创建
  - 位置: src/features/composite/render3d/animation.py:25
  - 证据:
    ```python
    logger = __import__("logging").getLogger(__name__)
    ```
  - 影响: 死代码；`__import__` 内联写法绕过常规 import 静态分析（lint/IDE 均不可见），不符合
    项目强类型/可读性约定，后续维护者难以发现可复用 logger。
  - 建议: 改为顶部 `import logging` + `logger = logging.getLogger(__name__)`，或删除未用变量。
  - 置信度: 高

- [AUD-8604] [Suggestion] 公共 API 类型注解不完整（select_clip / clip_names / _CacheEntry / sample）
  - 位置: src/features/composite/render3d/animation.py:205（local_matrices: dict）、:238-242
    （sample -> dict）、:305（select_clip(clips, ...)）、:325（clip_names(clips) -> list）
  - 证据:
    ```python
    @dataclass
    class _CacheEntry:
        local_matrices: dict   # {node_name: 4x4}
    ...
    def select_clip(clips, name: str):
    ...
    def clip_names(clips) -> list:
        return [c.name for c in clips]
    ```
  - 影响: clips 的元素类型（List[AnimationClip]）、sample 返回的 Dict[str, np.ndarray] 均未标注，
    违反项目强类型约定，IDE 补全/静态检查失效。
  - 建议: 补齐 `List["AnimationClip"]`、`Dict[str, "np.ndarray"]` 等泛型注解。
  - 置信度: 高

### 通过项
- 线程安全：AnimationSamplerCache 明确「线程安全由调用方保证（render thread 独占）」（:216-217），
  且调用方 model3d_node 在渲染线程独占使用，契约清晰，无隐藏共享。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：纯 numpy 无 GL/Qt 依赖，N/A。
- 异常与取消路径：remap 对 fps<=0、clip_duration<=1e-6 均有防护（:75-76）；pingpong/loop 的
  Python 取模对负 raw 落点正确（结果恒在 [0, period]/[0, duration)）。
- 性能模式：LRU 实现正确（命中移尾 :257-259、超限淘汰首项 :270-273）；命中/未命中均返回
  深拷贝（:261/:274），缓存不可变性保持；hit/miss 统计口径正确。
- 状态一致性：signature()（:187-194）字段齐全且 end_frame None 归一为 -1.0，适合做 cache key。

---

## src/features/composite/render3d/aov.py (行数: 461)

### 发现
- [AUD-8607] [Suggestion] IDAllocator 文档承诺「跨会话一致」，实现却是按分配（遍历）顺序递增的 id
  - 位置: src/features/composite/render3d/aov.py:203-221
  - 证据:
    ```python
    class IDAllocator:
        """稳定的 object/material id 分配器（master_plan §21.12 F9-4）。

        给每个对象/材质分配唯一整数 id（从 1 开始，0 保留给背景）。
        同一名称多次分配返回相同 id（稳定，跨帧/跨会话一致）。
        """
        def __init__(self):
            self._name_to_id: Dict[str, int] = {}
            self._next_id: int = 1
    ```
  - 影响: id 稳定性完全取决于 allocate() 首次调用顺序。若两次会话/两次渲染中场景图求值或
    遍历顺序不同（节点增删、图重排），同一对象/材质会拿到不同 id（经 encode_id_to_rgba 写入
    id pass），跨渲染的 id matte 匹配（Cryptomatte 选选、分级复用）会静默失效。同包内
    cryptomatte.py 已有按名稳定的 murmurhash 方案，两个 id 体系语义不一致。
  - 建议: 收窄文档承诺（仅同会话同顺序稳定），或改为按名称哈希分配（对齐 Cryptomatte 实践），
    或在持久化时随项目保存 id 映射。
  - 置信度: 中（机制确定；实际是否漂移取决于求值顺序稳定性，未在本次范围内验证求值顺序）

- [AUD-8608] [Suggestion] 类型注解不完整（裸 tuple 返回、IDAllocator.__init__ 缺 -> None）
  - 位置: src/features/composite/render3d/aov.py:176（encode_id_to_rgba -> tuple）、:210
    （def __init__(self):）、:168/:263/:368/:435（signature() -> tuple）
  - 证据:
    ```python
    def encode_id_to_rgba(object_id: int) -> tuple:
    ...
        def __init__(self):
            self._name_to_id: Dict[str, int] = {}
    ```
  - 影响: 返回值结构（Tuple[float, float, float, float] / 具体签名字段）未标注，弱化项目
    强类型约定。
  - 建议: 补齐泛型返回注解与 __init__ 的 -> None。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 生命周期 / GLSL / 信号防递归：纯数据层（docstring :7 声明并核实无 GL/PyQt import），N/A。
- 输入验证：encode_id_to_rgba 对负数/超 24bit 钳制（:188）；ObjectRenderFlags.signature 对
  softness 钳 [0,1]、intensity 钳非负（:268-269，与「>1 增强对比」文档一致）；render_flags_from_role
  对未知 role 回退 solid（:283，老项目可渲染）。
- 一致性：render_flags_from_role ↔ render_role_from_flags 四种角色往返一致（逐一推演）；
  AOVConfig.signature 排序稳定；AOV_EXPORTABLE_PASSES 注释明确「未实现的 pass 不得伪装可导出」，
  与 graph_preflight/scene3d_node/file_output_node 的用法吻合。
- 异常路径：本模块自身无 raise；id 编解码 encode/decode 对称（round(r*255) 可逆），decode
  当前仅测试使用，无生产 NaN 风险面。
- 性能模式：enabled_passes/is_any_extra_enabled 逻辑正确（beauty-only → False）。

---

## src/features/composite/render3d/camera_model.py (行数: 101)

### 发现
- [AUD-8605] [Major] gate_fit 的 fill 与 overscan 语义相对 Maya 标准约定互换（fill 取较大 gate、overscan 取较小 gate，方向相反）
  - 位置: src/features/composite/render3d/camera_model.py:77-82
  - 证据:
    ```python
    if key == "horizontal":
        return horizontal_gate_height
    if key == "fill":
        return max(spec.height_mm, horizontal_gate_height)
    if key == "overscan":
        return min(spec.height_mm, horizontal_gate_height)
    return spec.height_mm
    ```
  - 影响: docstring（:68-71）声称命名「matches common DCC camera-match workflows」，但按 Autodesk
    Maya 官方文档：Fill = "Fits the resolution gate within the film gate"（输出窗含于胶片门 → 取
    较小有效高度，裁剪填充），Overscan = "Fits the film gate within the resolution gate"（胶片门
    含于输出窗 → 取较大有效高度，显示更多）。代码恰好取反。例：full_frame 36×24、输出 16:9，
    horizontal_gate_height=20.25mm：Maya Fill → 20.25mm，代码 fill → 24mm；50mm 镜头下 vfov
    约 23.0° vs 27.2°。camera3d_node 的 cam_gate_fit 参数直接暴露 "fill"/"overscan" 选项
    （camera3d_node.py:60-62），且 focal↔fov 互转（fov_from_focal_and_filmback /
    focal_from_fov_and_filmback）与 scene_payload 的渲染相机均经由本函数 → 用户选择这两个模式做
    摄影机匹配时构图/FOV 系统性错误（horizontal/vertical 两模式不受影响，默认 vertical 安全）。
  - 建议: 交换 fill/overscan 两分支（fill → min、overscan → max），并补一条 36×24@16:9 的
    往返单测锁定语义；或在文档中显式声明采用的非 Maya 约定及理由。
  - 置信度: 高（已对照 Autodesk 官方文档核验两种模式的包含方向，并经几何推导与具体数值反例确认）

- [AUD-8606] [Suggestion] world_unit_scale 对未知单位静默回退 1.0（米）
  - 位置: src/features/composite/render3d/camera_model.py:44-47
  - 证据:
    ```python
    def world_unit_scale(unit: str) -> float:
        """Return the number of meters represented by one input world unit."""

        return WORLD_UNIT_TO_METERS.get(str(unit).strip().lower(), 1.0)
    ```
  - 影响: 单位字符串属外部输入（项目文件/导入适配器可写入任意值），未知单位（如 "yard"、"公尺"
    变体）会被静默当作米，相机距离/场景尺度悄然错 10% 量级，无任何提示，违背「外部数据须验证」。
    UI 侧 cam_unit 为受限 option（camera3d_node.py:66-68）缓解了手工输入，但持久化路径未设防。
  - 建议: 未知单位时 logger.warning 并/或回退到文档声明的默认值前记录；或返回 None 由调用方决定。
  - 置信度: 中（回退本身确定；实际出现未知单位的概率取决于加载路径）

### 通过项
- 线程安全 / GPU 生命周期 / GLSL / 信号防递归 / undo-redo：纯函数 + frozen dataclass，无状态，N/A。
- 数学正确性：horizontal 模式 width/aspect 推导正确；fov_from_focal_and_filmback 对 focal 下钳 0.1、
  focal_from_fov_and_filmback 对 fov 钳 (0.1, 179) 避免 tan 奇异（:91/:100）；aspect 除零以 1e-6 防护（:74）。
- 输入验证：filmback_spec 对 custom 尺寸下钳 0.1mm、空名回退 custom（:54-61）；preset 表与
  camera3d_node 的 option labels 一一对应（已比对）。
- 类型注解完整（camera_model.py 全文件有注解 + from __future__）。

---

## src/features/composite/render3d/cryptomatte.py (行数: 156)

### 发现
- 无（未发现达到 Suggestion 及以上的问题）。

### 通过项
- 哈希正确性：murmurhash3_32 逐段对照 MurmurHash3 x86 32-bit 参考实现核验——4 字节 little-endian
  分块、tail 三档 fallthrough（:45-54）、h1 ^= len、fmix 三段雪崩（:56-61）与移位掩码均一致；
  Cryptomatte 约定的 seed=0 + 位保持 float（struct pack/unpack，:68）符合规范。
- 异常与取消路径：from_names 对 32-bit 哈希碰撞 raise ValueError（:111-115），唯一生产调用方
  scene3d_node.py:103-111 已 try/except 并转为 {"status": "collision"} 结构化结果——异常路径被
  显式设计并处理，非缺陷。
- 线程安全 / GPU 生命周期：模块自述且核实为纯 Python 数据契约，无 GL 分配，无共享可变状态
  （frozen dataclass + tuple）。
- 输入验证：_object_name 兼容 Mapping 多键与裸值并 strip（:140-147）；from_names 去重 + 排序，
  manifest/to_metadata 使用 sort_keys + 紧凑分隔符，JSON 确定性输出适合做签名/持久化。
- 类型注解完整（含 from __future__ annotations、Iterable/Mapping 泛型）。

---

## 附：编号使用清单

AUD-8601（animation.py, Major）、AUD-8602（animation.py, Suggestion）、AUD-8603（animation.py,
Suggestion）、AUD-8604（animation.py, Suggestion）、AUD-8605（camera_model.py, Major）、
AUD-8606（camera_model.py, Suggestion）、AUD-8607（aov.py, Suggestion）、AUD-8608（aov.py,
Suggestion）。AUD-8600、AUD-8609–AUD-8699 未使用。

参考资料（AUD-8605 语义核验）：
Autodesk Maya 帮助 — Create > Cameras > Camera（Film Fit / Fit Resolution Gate）：
https://download.autodesk.com/us/maya/2008help/Rendering/Create__Cameras__Camera.html

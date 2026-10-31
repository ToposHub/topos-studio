# batch-340 审查报告（src/shared/）

批次：batch-340 [P2] 主题 src/shared//
编号区间：AUD-34000–AUD-34099
审查文件：src/shared/keyframe.py（1,146 行，已 Read 通读全文）

---

## src/shared/keyframe.py (行数: 1146)

关键帧动画数据模型：Keyframe / KeyframeTrack / ColorKeyframeTrack /
VectorKeyframeTrack / DiscreteKeyframeTrack + 插值函数（linear/bezier/ease）+
序列化与工厂函数。纯内存数据层，被 UI 与渲染求值两侧共用（NodeSnapshot 携带至渲染线程）。

### 发现

- [AUD-34001] [Major] P-1 批量加载把排序/去重移出逐键异常保护，畸形 frame 类型可令整轨反序列化抛 TypeError 逃逸
  - 位置: src/shared/keyframe.py:398-405（配合 169-170、104）
  - 证据:
    ```python
    bulk: List[Keyframe] = []
    for kf_data in data.get("keyframes", []):
        try:
            bulk.append(Keyframe.from_dict(kf_data))
        except Exception:
            continue
    track.add_keyframes_bulk(bulk)
    ```
    而 `add_keyframes_bulk` 内部（169-170）执行 `self.keyframes.extend(keyframes)` + `self.keyframes.sort()`；`Keyframe.from_dict`（104）取值 `frame=data.get("frame", 0)` 不做 int 归一。
  - 影响: 工程文件中单个 keyframe 的 frame 为字符串（如手改/损坏 JSON）时，构造阶段不报错，`sort()` 经 `__lt__` 比较 str/int 抛 TypeError——该异常发生在 try 块之外，直接逃逸 `from_dict`，导致整轨（进而整工程，视上游是否兜底）加载失败。S1 注释声称的"逐键容错"语义被 P-1 优化破坏，属回归。value 类型错误（如标量轨存了 list）则延迟到求值期在 `interpolate()` 内抛 TypeError。
  - 建议: 在 `Keyframe.from_dict` 内将 frame 归一为 int（非数值则抛 ValueError 让逐键保护接住），value 按轨道类型校验；或将 `add_keyframes_bulk` 的异常面纳入逐键容错并在跳过时记录日志。
  - 置信度: 高（代码路径确定；触发依赖畸形外部文件）

- [AUD-34002] [Major] 宽泛 `except Exception` 静默丢弃关键帧且无日志，违反 .clinerules「不得宽泛 except 掩盖领域错误」
  - 位置: src/shared/keyframe.py:399-403
  - 证据:
    ```python
    for kf_data in data.get("keyframes", []):
        try:
            bulk.append(Keyframe.from_dict(kf_data))
        except Exception:
            continue
    ```
  - 影响: 除预期的 ValueError/TypeError/KeyError 外，`Keyframe.from_dict` 内部任何 bug（如 AttributeError）也被吞掉，动画数据静默丢失、无任何日志；用户保存后重开发现关键帧消失且无法排查。同文件 Color/Vector/Discrete 三个 from_dict（528、667、763）均捕获 `(TypeError, ValueError, KeyError)`，唯独此处最宽。
  - 建议: 收窄为 `(TypeError, ValueError, KeyError)`，并对每个被跳过的 keyframe 记 `logging.warning`（含 param_name 与原因）。
  - 置信度: 高

- [AUD-34003] [Major] `interpolate()` 的 exact-match / HOLD / frame_range<=0 早退路径绕过 [min_val, max_val] clamp，与 S2 声明的一致性目标冲突
  - 位置: src/shared/keyframe.py:824-840（对照 868、234-236、360-363）
  - 证据:
    ```python
    # Exact match
    if frame == kf1.frame:
        return kf1.value
    if frame == kf2.frame:
        return kf2.value

    frame_range = kf2.frame - kf1.frame
    if frame_range <= 0:
        return kf1.value
    ...
    if kf1.interpolation == InterpolationType.HOLD:
        return kf1.value
    ```
    计算路径末尾才有 `return max(min_val, min(max_val, value))`（868）；`get_value` 对首/末关键帧边界显式 `_clamp_range`（234-236、247-249）。
  - 影响: 中间关键帧恰在求值帧（bisect_right 使 kf1 命中该帧）或 HOLD 段时，越界存储值（遗留工程文件、ParamDef 范围收缩后的 min/max）不经 clamp 直接返回 `Any` 原值进入渲染参数；相邻帧又被 clamp，造成逐帧值跳变与超范围 shader 参数。`get_value_float` 的 BEZIER/EASE 整帧混合（360-363）经 `interpolate()` 早退同样泄漏未 clamp 值（其 LINEAR/HOLD 分支则已 clamp，行为不对称）。S2 注释明确以"get_value/get_value_float 与 interpolate() 一致 clamp"为目标，此为漏网点。
  - 建议: 三个早退 `return kf1.value` / `kf2.value` 统一改为 `max(min_val, min(max_val, ...))`（或经 `_clamp_range` 同款表达式），使全路径 clamp 一致。
  - 置信度: 高（不一致性确定；实际影响依赖存在越界存储值）

- [AUD-34004] [Major] ColorKeyframeTrack 不校验分量有限性（Vector 轨道有 isfinite 校验），NaN/Inf 可入轨并传播到渲染
  - 位置: src/shared/keyframe.py:427-433（对照 558-560）
  - 证据:
    ```python
    value = keyframe.value
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Color keyframes require four RGBA components")
    normalized = Keyframe(
        frame=int(keyframe.frame),
        value=tuple(float(component) for component in value),
    ```
    对照 VectorKeyframeTrack._normalize_value（558-560）：
    ```python
    result = tuple(float(component) for component in value)
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"{self.value_type} keyframes require finite components")
    ```
  - 影响: Python json 默认读/写 NaN 字面量；上游任何一处除零或坏数据产生的 NaN 分量可经 `add_keyframe`（含 `from_dict` 逐键路径——其 ValueError 捕获拦不住合法 float NaN）进入色彩参数，求值/插值全程传播 NaN 至 shader，输出未定义（黑块/闪烁），且保存-重载后持续存在。四类轨道中仅色彩轨道缺此防线，属校验不一致。
  - 建议: 与 `_normalize_value` 对齐，对四个分量增加 `math.isfinite` 校验并抛 ValueError。
  - 置信度: 中（缺口确定；NaN 实际出现依赖上游数据）

- [AUD-34005] [Suggestion] Color/Vector 轨道每次求值整体重建 `_component_track`，播放热路径上每帧每参数分配 4×N 个对象
  - 位置: src/shared/keyframe.py:458-478、486-496（Vector 同构 591-611、619-631）
  - 证据:
    ```python
    def get_value(self, frame: int,
                  default: Any = (0.0, 0.0, 0.0, 1.0)) -> Tuple[float, ...]:
        fallback = self._normalize_default(default)
        return tuple(self._component_track(index).get_value(
            int(frame), default=fallback[index]) for index in range(4))
    ```
    `_component_track` 每次遍历全部 keyframes 重建 KeyframeTrack + N 个 Keyframe（460-470）。
  - 影响: 每帧求值（60fps × 多个动画化色彩/向量参数 × K 个关键帧）产生 4K 个临时对象/帧量级的分配与 GC 压力，与文件内 P-1/P-3 注释反复强调的热路径优化方向相悖（未测量，非算法性退化）。
  - 建议: 按 keyframes 数量/修订号缓存分量轨道并失效重算；或提供直接对分量数组做插值的求值函数，绕过对象重建。
  - 置信度: 中

- [AUD-34006] [Suggestion] `validate_track()` 对 Color/Vector/Discrete 轨道不适用：tuple 值与 float 比较抛 TypeError，且只校验 legacy bezier 控制点
  - 位置: src/shared/keyframe.py:1096-1117
  - 证据:
    ```python
    # Check value range
    if kf.value < track.min_val or kf.value > track.max_val:
        errors.append(...)

    # Check bezier control points
    if kf.interpolation == InterpolationType.BEZIER:
        for cp_name, cp in [("bezier_in", kf.bezier_in), ("bezier_out", kf.bezier_out)]:
    ```
  - 影响: 四类轨道为平行 dataclass（非子类）但结构同构，误传 Color/Vector 轨道时 `kf.value < track.min_val` 对 tuple 抛 TypeError 而非返回错误清单；BEZIER 新权威格式 handle_left/right 无任何校验（x 越段未夹紧时渲染端 `_resolve_handle` 会静默夹紧，校验器却不知情）。
  - 建议: 按 value_type 分派到各轨道自己的校验实现，或收窄签名并在入口 isinstance 检查；补充 handle 越界检查项。
  - 置信度: 高

- [AUD-34007] [Suggestion] 四个轨道类逻辑平行复制（replace-in-place/append/sort、remove、get_keyframe_at、extrap 序列化块四处重复），已现行为漂移
  - 位置: src/shared/keyframe.py:441-446、574-579、705-710（同构块）；374-377、509-512、644-647、743-746（extrap 序列化重复）
  - 证据:
    ```python
    for index, existing in enumerate(self.keyframes):
        if existing.frame == normalized.frame:
            self.keyframes[index] = normalized
            return
    self.keyframes.append(normalized)
    self.keyframes.sort()
    ```
    该块在 ColorKeyframeTrack / VectorKeyframeTrack / DiscreteKeyframeTrack 中逐字重复（KeyframeTrack 150-155 亦同）；且 Color/Vector/Discrete 未获得 KeyframeTrack 的 `add_keyframes_bulk` 优化，from_dict 仍是 O(n²log n) 逐键排序。
  - 影响: 维护面扩大；同类逻辑修一处漏三处的风险已经显现（见 AUD-34004 校验不一致、bulk 优化未同步）。
  - 建议: 提取共享基类/mixin（add/remove/get_keyframe_at/extrap 序列化），类型差异留在 `_normalize_value` 钩子；顺带让三类型复用 `add_keyframes_bulk`。
  - 置信度: 高

- [AUD-34008] [Suggestion] 类型注解缺口：局部列表无注解、声明 `-> float` 却返回 Any、工厂函数返回 Any
  - 位置: src/shared/keyframe.py:459、826-828、784-785、801-802
  - 证据:
    ```python
    def _component_track(self, component: int) -> KeyframeTrack:
        scalar_keyframes = []          # 无注解
    ```
    ```python
    if frame == kf1.frame:
        return kf1.value               # value: Any，签名 -> float
    ```
    ```python
    def make_keyframe_track(param_name: str, value_type: str = "scalar",
                            min_val: float = 0.0, max_val: float = 1.0) -> Any:
    ```
  - 影响: `Any` 透传削弱静态检查价值（越界/非数值值正是 AUD-34001/34003 的成因之一）；`scalar_keyframes` 依赖推断。
  - 建议: 声明 `scalar_keyframes: List[Keyframe] = []`；插值函数返回前做 `float(...)` 归一；工厂返回 `Union[KeyframeTrack, ColorKeyframeTrack, VectorKeyframeTrack, DiscreteKeyframeTrack]` 或定义 Track 协议。
  - 置信度: 高

### 通过项

- 线程安全：纯内存数据模型，无模块级可变共享状态、无锁需求；列表读写在 GIL 下无撕裂视图（渲染线程按 NodeSnapshot 拷贝使用的契约下安全）
- GPU 资源生命周期：本文件不涉及任何 ModernGL/OpenGL 资源
- GLSL/HDR 正确性：clamp 目标为参数 UI 域 [min_val, max_val] 而非 (0,1)，属参数语义 clamp，不违反 HDR 不-clamp 铁律（例外路径见 AUD-34003）
- 信号防递归：无 Qt 控件/信号，不涉及
- 序列化 round-trip：Keyframe 的 handle_left/right、tangent_mode、legacy bezier 与四类轨道的 value_type/dimension/extrap 均可无损往返（Vector dimension 由 value_type 正确回推）
- 取消路径：无异步/可取消操作
- 二分查找前提：keyframes 有序不变量由 add_keyframe/add_keyframes_bulk 维护，get_value/get_value_float/_value 的 bisect 使用正确（kf1=[i-1]、kf2=[i] 边界判断正确）
- 标量热路径性能：get_value/get_value_float 已用 bisect O(log n)（P-3）、批量加载单次排序+线性去重（P-1）、bezier 二分固定 24 次迭代，符合实时基准要求
- Color/Vector/Discrete from_dict 异常处理：逐键捕获具体异常类型 `(TypeError, ValueError, KeyError)`，无宽泛 except（KeyframeTrack 例外见 AUD-34002）
- 输入验证：Vector 轨道校验维度与分量有限性、Discrete 轨道校验 value_type 与值类型、get_value_ramp 校验 fps/sample_rate/num_samples 非正

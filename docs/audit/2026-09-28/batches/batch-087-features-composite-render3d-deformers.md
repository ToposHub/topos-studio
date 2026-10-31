# batch-087 审查报告 — src/features/composite/render3d/deformers/

批次：batch-087 [P1] · 编号区间 AUD-8700–AUD-8799
审查日期：2026-09-28
文件：`__init__.py` (42) / `base.py` (149) / `cpu.py` (322)，共 513 行，全部通读。
交叉验证：`src/features/composite/render3d/fields.py`、`src/features/composite/render3d/primitives.py`（MeshData）、唯一调用方 `src/features/composite/nodes/deformer3d_node.py`。

汇总：Critical 1 / Major 4 / Suggestion 5。

---

## src/features/composite/render3d/deformers/cpu.py (行数: 322)

### 发现

- [AUD-8701] [Critical] Stretch 变形在 UI 允许的参数范围内可产生 NaN/inf 顶点并上传 GPU
  - 位置: src/features/composite/render3d/deformers/cpu.py:246-249
  - 证据:
    ```python
    amount = float(spec.params.get("amount", 1.3))
    center = spec.center
    stretch_f = 1.0 + (amount - 1.0) * weights
    squash_f = 1.0 / np.sqrt(stretch_f)  # 体积守恒：s_xy = 1/sqrt(s_axis)
    ```
  - 影响: `weights` 已含 `spec.strength`（`_compute_weights` 第 68 行 `w * float(spec.strength)`）。UI（deformer3d_node.py:66 `strength max_val=5.0`、:73 `amount min_val=-5.0`）允许 amount<1 且 strength>1/(1-amount)，例如 amount=0.5、strength=2.5 时 stretch_f=-0.25，`np.sqrt` 负数得 NaN（仅 RuntimeWarning，非异常），squash_f 为 NaN/inf 并写入顶点；调用方 except 子句（deformer3d_node.py:306/320 只捕 ValueError/TypeError/LinAlgError）不会拦截，NaN 顶点直接进 MeshData → VBO → 渲染输出损坏（黑块/消失/花屏）。
  - 建议: 对 stretch_f 加下界保护（如 `np.maximum(stretch_f, 1e-6)` 或对 amount、strength 组合做域校验并抛 ValueError），或在上传前做有限性检查（`np.isfinite`）。
  - 置信度: 高

- [AUD-8702] [Major] Taper/Stretch 非均匀缩放后法线未按 inverse-transpose 变换，原样返回旧法线
  - 位置: src/features/composite/render3d/deformers/cpu.py:229-235、248-257
  - 证据:
    ```python
    scale = 1.0 + (amount - 1.0) * 0.5 * (t + 1.0) * weights
    pts = vertices.copy()
    other_axes = [i for i in range(3) if i != axis]
    for a in other_axes:
        rel = pts[:, a] - center[a]
        pts[:, a] = center[a] + rel * scale
    return pts.astype(np.float32), normals.astype(np.float32)
    ```
  - 影响: 非均匀（且逐顶点不同的）缩放改变 surface 切平面，正确法线应乘缩放的 inverse-transpose 并归一化；直接返回原法线导致 Taper/Strength 变形后的曲面光照/着色错误（法线与几何不一致）。调用方（deformer3d_node.py:302/316）直接把 `deform_result.normals` 上传 VBO 渲染，无处修正。
  - 建议: 对 taper/stretch 按每顶点 Jacobian 的 inverse-transpose 变换法线后 `renormalize`；或在文档/返回标志中明示法线为近似值（联动 AUD-8704）。
  - 置信度: 高

- [AUD-8703] [Major] evaluate_deformer 对未实现/未知 deformer_type 静默 no-op（SHEAR/SPHERIFY 已导出但无实现）
  - 位置: src/features/composite/render3d/deformers/cpu.py:291-298、319-321
  - 证据:
    ```python
    _DEFORMERS = {
        DEFORMER_BEND: _deform_bend,
        DEFORMER_TWIST: _deform_twist,
        DEFORMER_BULGE: _deform_bulge,
        DEFORMER_TAPER: _deform_taper,
        DEFORMER_STRETCH: _deform_stretch,
        DEFORMER_NOISE: _deform_noise,
    }
    ...
    func = _DEFORMERS.get(spec.deformer_type)
    if func is None:
        return vertices.astype(np.float32), normals.astype(np.float32)
    ```
  - 影响: `base.py:39-40` 定义并经 `__init__.py` 导出 `DEFORMER_SHEAR`/`DEFORMER_SPHERIFY`，但 `_DEFORMERS` 无对应实现；拼错类型名（如 "Bend"）同样静默跳过变形，无日志无异常，违反"不得掩盖领域错误"铁律，用户会看到"变形器不生效"却无诊断。当前 UI（deformer3d_node.py:38-40）只暴露 6 种类型，属潜伏缺陷。
  - 建议: 未知类型时 raise ValueError（调用方已有 except ValueError 降级+diagnostics 路径）或至少 logger.warning；未实现的 SHEAR/SPHERIFY 从导出中移除或补实现。
  - 置信度: 高

- [AUD-8704] [Suggestion] 每个变形器执行都强制 astype 全量拷贝，栈式/大网格下多余内存带宽
  - 位置: src/features/composite/render3d/deformers/cpu.py:317、322
  - 证据:
    ```python
    if not spec.enabled or vertices.shape[0] == 0:
        return vertices.astype(np.float32), normals.astype(np.float32)
    ...
    return func(spec, vertices.astype(np.float32), normals.astype(np.float32), weights)
    ```
  - 影响: `astype(np.float32)` 默认 copy=True，即使已是 float32 也整份拷贝；`DeformerStack.evaluate` 上一级已 astype(copy=True)，栈内每个变形器再各拷两次（顶点+法线），N 顶点网格每帧多 4×O(N) 拷贝，与 4K 实时基准目标相悖。
  - 建议: 用 `np.asarray(..., dtype=np.float32)` 或 `astype(np.float32, copy=False)`（各 `_deform_*` 已自行 copy，安全性不受影响）。
  - 置信度: 高

- [AUD-8705] [Suggestion] _axis_index 对非法轴名静默回退 Y 轴
  - 位置: src/features/composite/render3d/deformers/cpu.py:40-41
  - 证据:
    ```python
    def _axis_index(axis: str) -> int:
        return _AXIS_INDEX.get(axis.lower(), 1)
    ```
  - 影响: axis 参数来自节点参数（外部输入），非法值（如 "Y "、"w"）不报错而是按 Y 轴变形，结果与用户意图不符且难排查。当前 UI 用下拉限定 x/y/z，属潜伏输入验证缺口。
  - 建议: 非法轴抛 ValueError，与"外部数据须验证"铁律一致。
  - 置信度: 高

### 通过项
- 线程安全：纯函数式 numpy，无共享可变状态、无 GL/PyQt 调用，符合"纯 numpy"声明
- GPU 资源生命周期：不涉及（本模块无 GL 对象）
- 非破坏性：所有变形器返回新数组（copy/astype），不修改调用方输入
- Bend/Twist 法线与顶点做同一旋转（旋转保持法线有效），数学一致（_rotate_around_axis 为标准右手旋转）
- Bulge radial 模式对 center 处顶点有除零保护（dist<1e-9 → 1.0，方向归零不产生 NaN）
- 空顶点守卫（_compute_weights n==0、evaluate_deformer shape[0]==0）
- Noise 确定性（无 numpy.random，seed/time 可重复，符合文档承诺）
- 类型注解完整（函数签名均有精确注解）

---

## src/features/composite/render3d/deformers/base.py (行数: 149)

### 发现

- [AUD-8706] [Major] DeformerStack.evaluate 无条件返回 recomputed_normals=True，违反 DeformerResult 契约
  - 位置: src/features/composite/render3d/deformers/base.py:148-149（对照 96、133-137）
  - 证据:
    ```python
    return DeformerResult(vertices=verts, normals=norms,
                          recomputed_normals=True)
    ```
    而类契约（第 96 行）：`recomputed_normals: 是否重算了法线（False=近似继承原法线）。`
  - 影响: 栈内变形器从未按面重算法线——Bend/Twist 仅旋转、Taper/Stretch/Bulge/Noise 返回近似或原封法线（见 AUD-8702），但结果被标记为"已重算"。依赖该标志决定是否执行 master_plan §16.3 "deformer stack → normal update" 步骤的调用方会跳过法线重算，带着错误法线上传渲染。空栈路径（133-137）正确标 False，非空路径语义失实。
  - 建议: 各变形器返回自己是否正确变换了法线，栈内取与/或语义如实上报；或统一改为 False 并由调用方决定重算。
  - 置信度: 高

- [AUD-8707] [Major] spec.selection_set_id 在求值中被完全忽略，selection_mask 一刀切作用于栈内所有变形器
  - 位置: src/features/composite/render3d/deformers/base.py:141-147（对照 docstring 126-127）；cpu.py:65-66
  - 证据:
    ```python
    for spec in self.deformers:
        if not spec.enabled:
            continue
        from .cpu import evaluate_deformer
        mask = selection_mask
        verts, norms = evaluate_deformer(spec, verts, norms, mask)
    ```
    docstring 承诺：`selection_set_id 非空的 deformer 只影响 mask=True 的顶点。`
  - 影响: 代码从不读取 `spec.selection_set_id`；调用方传入的单一 mask 被同等施加给栈内每个变形器（含 selection_set_id 为空、本应影响全部顶点的变形器），与文档承诺的 F3 选择集联动语义相反。当前唯一调用方不传 mask（deformer3d_node.py:299/313），属潜伏功能缺陷。
  - 建议: 按 selection_set_id 解析每变形器各自的 mask（空 id → None），或在文档中删除未实现承诺。
  - 置信度: 高

- [AUD-8708] [Suggestion] DeformerSpec.signature() 遗漏 selection_set_id，且 params 含不可哈希值时会 TypeError
  - 位置: src/features/composite/render3d/deformers/base.py:81-86
  - 证据:
    ```python
    return (
        self.deformer_type, bool(self.enabled), float(self.strength),
        str(self.space), str(self.axis), self.center.tobytes(),
        tuple(sorted(self.params.items())) if self.params else (),
        field_sig,
    )
    ```
  - 影响: (1) 签名不含 selection_set_id，选择集变化不会使缓存失效；(2) params 值若为 list/ndarray/dict（API 未禁止），tuple 无法哈希，用作 dict key 时抛 TypeError。当前无调用方使用 signature()（调用方自带 _deformer_param_sig），属潜伏问题。
  - 建议: 签名加入 selection_set_id；params 值做归一化（ndarray → tobytes，list → tuple）后再入签名。
  - 置信度: 中

- [AUD-8709] [Suggestion] params 字段类型注解未参数化
  - 位置: src/features/composite/render3d/deformers/base.py:64
  - 证据:
    ```python
    params: dict = field(default_factory=dict)
    ```
  - 影响: 弱类型注解，IDE/静态检查无法推断键值类型，与项目强类型规范（所有函数有类型注解）不完全一致。
  - 建议: 改为 `params: Dict[str, Any] = field(default_factory=dict)` 并在 docstring 约定值域（float/str）。
  - 置信度: 高

### 通过项
- 线程安全：纯数据结构，无共享全局可变状态，无 GL/PyQt 调用
- GPU 资源生命周期 / GLSL·HDR：不涉及（无 GL、无颜色数据 clamp）
- 信号防递归：无信号
- 输入验证：__post_init__ 对 center 强制 asarray(float32).reshape(3)，非 3 元素即抛错
- 非破坏性：evaluate 不改写源 mesh（astype copy=True），空栈路径返回拷贝
- 循环依赖处理正确（evaluate 内延迟导入 cpu，注释说明原因）
- DeformerStack.is_empty 空列表/全禁用语义正确；signature 含 enabled 便于缓存失效

---

## src/features/composite/render3d/deformers/__init__.py (行数: 42)

### 发现

- [AUD-8710] [Suggestion] 模块 docstring 公开接口与实际导出不一致；导出了无实现的 SHEAR/SPHERIFY 常量
  - 位置: src/features/composite/render3d/deformers/__init__.py:6、24-25、29-42
  - 证据:
    ```python
    FieldSpec / linear_field / radial_field / box_field — Falloff 控制（fields.py）
    ...
        DEFORMER_SHEAR,
        DEFORMER_SPHERIFY,
    ```
  - 影响: (1) docstring "公开接口" 列出 FieldSpec/linear_field/radial_field/box_field，但 import 与 `__all__` 均未导出，按 docstring 使用 `from ...deformers import FieldSpec` 会 ImportError；(2) `DEFORMER_SHEAR`/`DEFORMER_SPHERIFY` 被导出为公开 API，但 cpu.py 无实现（见 AUD-8703），导出即承诺、承诺不兑现。
  - 建议: 从 `fields` re-export FieldSpec 等以匹配文档，或修正 docstring；SHEAR/SPHERIFY 在补实现前暂不导出。
  - 置信度: 高

### 通过项
- 通过（无逻辑）：纯 re-export 模块，import 列表与 `__all__` 完全一致，无运行时逻辑、无状态

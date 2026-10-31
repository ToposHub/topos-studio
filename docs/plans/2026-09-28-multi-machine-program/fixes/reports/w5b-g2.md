# 批 A Critical 修复报告（w5b-g2）

- 修复日期：2026-09-29
- 修复人：ZCode subagent（w5b-g2）
- 证据：docs/audit/2026-09-28/batches/batch-088-features-composite-render3d.md、
  batch-089-features-composite-render3d.md、batch-092-features-composite-render3d.md
- 白名单：src/features/composite/render3d/hdri.py、material_model.py、scene_payload.py
- 新测试：tests/composite/test_fix_render3d_aud5b.py

## 汇总

| 编号 | 严重度 | 状态 |
|------|--------|------|
| AUD-88050 | Critical/bug | **FIXED** |
| AUD-8901  | Critical/bug | **FIXED** |
| AUD-9200  | Critical/bug | **FIXED** |

---

## AUD-88050 [Critical] 辐照度卷积遗漏 sin(theta) 立体角权重 — FIXED

**根因**：`_IRRADIANCE_CONVOLVE_FS`（hdri.py）的球面 Riemann 求和累积项只乘了
Lambert 余弦 `cos_theta`，漏掉立体角面积元因子 `sin_theta`
（dω = sinθ·dθ·dφ），把 (φ,θ) 等步长网格当成了等面积采样：极点样本超权、
赤道欠权，漫反射 IBL 能量与方向分布系统性错误。

**修复**（src/features/composite/render3d/hdri.py:127-133）：

```glsl
// 修复前
irradiance += texture(u_env, sample_dir).rgb * cos_theta;  // cosine 加权
// 修复后
irradiance += texture(u_env, sample_dir).rgb * cos_theta * sin_theta;
```

归一化 `π·Σ/N` 保持不变——该系数使 cubemap 存储 **E/π**（learnopengl 约定），
与消费端 core/shaders.py:6485-6486 的 `diffuse_ibl = irradiance * albedo`
（Lo = albedo/π · E）精确配套；只补权重、不动约定。

**数值验证**（tests/composite/test_fix_render3d_aud5b.py，
`_irradiance_shader_mirror` 用 numpy 逐行镜像该 GLSL 求和，含切线空间
T/B/N 方向构造，对解析可积环境场做收敛/对称性检查）：

| 环境场 | 法线 | 解析值（存储量 E/π） | 镜像结果 |
|--------|------|---------------------|----------|
| 均匀 L≡1 | +Y / +X / −Y / (1,1,1)/√3 | 1.0 | ≈1.0（rel<0.3%），任意方向一致（对称性） |
| 天空 L=max(0,ω_y) | +Y | 2/3（E=2π/3） | ≈0.665 ✓ |
| 天空 L=max(0,ω_y) | −Y | 0（半球内 L≡0） | ≈0 ✓ |
| 天空 L=max(0,ω_y) | +X | 2/(3π)≈0.2111（E=2/3） | 0.21107 ✓ |
| 均匀 L≡1（旧权重，无 sinθ） | +Y | —（不收敛到解析值） | ≈2.0（能量翻倍，证明权重 load-bearing） |

另有源码级回归断言：shader 累积表达式必须同时含 `cos_theta` 与 `sin_theta`。

---

## AUD-8901 [Critical] material_override_to_dict 静默丢弃 list — FIXED

**根因**：`material_override_to_dict`（material_model.py）的 isinstance 分支链
只认 ndarray / numpy 标量 / str/int/float/bool / None。材质面板
（material_panel.py:348、:371）用普通 Python list 写 `base_color`
（`[spin.value() for spin in self._rgb]`、`preset.model.base_color.tolist()`），
不匹配任何分支被无声丢弃 → `encode_material_overrides` 产出的节点参数 JSON
缺 base_color → 工程保存/重载后用户改色全丢。会话内 `apply_override` 用
`np.asarray` 接受 list，故会话内正常、重载即丢，极难察觉。

**修复**（src/features/composite/render3d/material_model.py:375-399）：
新增 `(list, tuple)` 分支，转为 JSON-safe list；元素若是 numpy 标量统一
`.item()` 规约（避免 json.dumps 抛 TypeError）：

```python
elif isinstance(value, (list, tuple)):
    scalar_values[str(key)] = [
        item.item() if isinstance(item, (np.floating, np.integer)) else item
        for item in value
    ]
```

**反序列化侧对齐**：`material_override_from_dict` 对白名单字段本就原样透传
list（读写不对称的缺口在序列化侧，已闭合）；`apply_override` /
`apply_override_to_material_data` 用 `np.asarray(value).reshape(3)` 消费 list。
测试验证 encode→decode 往返后 base_color 保留，且两侧 apply 均恢复正确颜色。
原有 ndarray/numpy 标量/str 分支有回归测试保护，行为不变。

---

## AUD-9200 [Critical] ModelPayload.signature 遗漏 material_params — FIXED

**根因**：`ModelPayload.signature()`（scene_payload.py:366）覆盖
transform_sig / material_overrides / morph_weights / hidden / locked / solo /
render_flags / anim，但不包含 317 行定义的
`material_params`（material_mode / normal_format / orm_layout /
texture_filter / texture_wrap / emissive_intensity）。这些值也不在
transform_sig（model3d_node.py:1051-1056）。用户改材质控制参数 → 签名不变 →
Scene3D content_sig 缓存命中 → 直接回传陈旧 `_gpu_texture`（渲染错误）。

**修复**（src/features/composite/render3d/scene_payload.py:389-390）：
签名新增一项，与 material_overrides 的处理风格一致（key 排序 + 值 str 规约
保证可哈希；model3d_node.py:857-864 填充的值全为 str/float）：

```python
tuple(sorted((str(key), str(value))
             for key, value in (self.material_params or {}).items())),
```

`material_params=None` 与 `{}` 同签名（均表示"无参数"，不影响输出）。
测试参数化覆盖全部 6 个控制键：任一变化必改签名；相同参数签名稳定；
签名仍可哈希。

---

## 验证记录

py_compile（全部通过）：

```
$ .venv/bin/python -m py_compile \
    src/features/composite/render3d/hdri.py \
    src/features/composite/render3d/material_model.py \
    src/features/composite/render3d/scene_payload.py
PY_COMPILE OK
```

新测试：

```
$ .venv/bin/python -m pytest tests/composite/test_fix_render3d_aud5b.py -v
19 passed in 0.27s
```

邻接既有测试（hdri / material / payload / render3d 相关，grep tests/ 选取）：

```
$ .venv/bin/python -m pytest \
    tests/composite/test_material3d_model.py tests/composite/test_scene_payload.py \
    tests/composite/test_material_panel.py tests/composite/test_model3d_material_upgrade.py \
    tests/composite/test_material_state_cache.py tests/composite/test_3d_phase1_correctness.py \
    tests/composite/test_scene3d_node_coverage.py tests/composite/test_global_3d_scene_settings.py
377 passed in 2.95s

$ .venv/bin/python -m pytest \
    tests/composite/test_p8_gpu_optimization.py tests/composite/test_3d_gaps_p0_p1.py \
    tests/composite/test_model_import_cache.py tests/composite/test_scene3d_skeleton_integration.py
79 passed in 3.10s
```

既有测试无失败，无需按协议处理旧行为锁定。
（备注：tests/composite/test_material3d_model.py:224
`test_json_roundtrip_keeps_scalar_and_slot` 旧版就 set 了 base_color list 但
未断言其往返存活——这正是 AUD-8901 得以潜伏的原因；该测试不被本修复破坏，
新测试补上了缺失的断言。）

## 变更文件

- src/features/composite/render3d/hdri.py（AUD-88050，+4/-1 行）
- src/features/composite/render3d/material_model.py（AUD-8901，+14/-1 行）
- src/features/composite/render3d/scene_payload.py（AUD-9200，+6/-1 行）
- tests/composite/test_fix_render3d_aud5b.py（新建，19 个测试）

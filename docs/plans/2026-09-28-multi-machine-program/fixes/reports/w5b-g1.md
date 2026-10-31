# W5B-G1 修复报告：批 A（Critical）shared/effects pass 反馈回路

- 日期：2026-09-29
- 工人：W5B-G1
- 证据：docs/audit/2026-09-28/batches/batch-264-features-shared-effects.md
- 白名单：src/features/shared/effects/effect_pass_runner.py
- 新测试：tests/shared/test_fix_effect_pass_aud5b.py

## 结论总表

| 编号 | 严重级 | 状态 | 位置 |
|------|--------|------|------|
| AUD-26401 | Critical/bug | **FIXED** | effect_pass_runner.py `_run_separable`（原 1050-1085） |
| AUD-26402 | Critical/bug | **FIXED** | effect_pass_runner.py `_run_glow`（原 1238-1286） |

两条均复核属实（对照 FX-M02 在 `_run_glow` 已有的 pass-2 防护写法 1227-1246），
按修复方向第一方案（pass 序列运行时动态分配输入/输出/scratch）实施根因修复。
无分支操作、无 commit/push。

---

## 1. AUD-26401 — `_run_separable` H/V pass 固定写 scratch_b/scratch_a [FIXED]

### 根因（复核属实）

全分辨率 separable 路径（原 1052-1085）无条件采用固定路由
H: input→scratch_b、V: scratch_b→scratch_a，且 V pass 将链式输入绑到
unit 1 作 `u_original`。调用方（`edit_gpu_preview_renderer.py` 的
`current_glo = runner.run(spec, current_glo, ...)`）把前一效果输出直接作为
后一效果输入，于是：

- 输入 == scratch_a（链 [sharpen → blur]，sharpen 单 pass 输出 scratch_a）：
  V pass 写 scratch_a 的同时经 unit 1 采样 scratch_a → GL 反馈回路；
- 输入 == scratch_b（链 [sharpen → denoise → blur]，denoise 输入 scratch_a
  输出 scratch_b）：H pass 写 scratch_b 的同时经 unit 0 采样 scratch_b →
  同类反馈回路。

渲染到同时被采样的附件属未定义行为；本项目在 `_run_single_pass` 注释中
已记载同类症状（"two sharpens → black screen"）。对照
`_run_single_pass`（958-961）、`_run_separable_downsampled`（1179-1182）与
`_run_erode_dilate`（1484）均已做输入/输出错开防护，唯独全分辨率
separable 路径缺失。

### 修复（运行时动态分配 H/V 目标）

`_ensure_scratch` 之后按输入别名重选两个 pass 的写入目标，保持逐 pass
约束（H 目标 ≠ 输入；V 目标 ∉ {H 目标, 输入}），三张 scratch 足以保证
任意输入组合无读写同纹理：

```python
if input_glo == self._scratch_a.glo:
    h_tex, h_fbo = self._scratch_b, self._scratch_b_fbo
    v_tex, v_fbo = self._scratch_c, self._scratch_c_fbo
elif input_glo == self._scratch_b.glo:
    h_tex, h_fbo = self._scratch_a, self._scratch_a_fbo
    v_tex, v_fbo = self._scratch_c, self._scratch_c_fbo
else:
    # 外部纹理或前一效果的 scratch_c 输出——与 b(H)/a(V) 均不别名。
    h_tex, h_fbo = self._scratch_b, self._scratch_b_fbo
    v_tex, v_fbo = self._scratch_a, self._scratch_a_fbo
```

- H pass 渲染进 `h_fbo`，V pass 以 `h_tex.use(0)` 为 `u_texture`、
  `input_glo`（unit 1）为 `u_original` 渲染进 `v_fbo`；
- 返回值与 `_last_output_tex` 由固定 `scratch_a` 改为 `v_tex`；
- 外部输入路径保持原 b→a 路由逐字节不变（golden-frame/parity 不受影响）；
- V pass 的 mask/mix 语义（u_original = 纯净输入）原样保留。
- 覆盖输入为 scratch_c 的链（如 glow(FX-M02 路由) → blur）：走 else 分支
  的 b→a 规范路由，同样无别名。

fast-tier（`_run_separable_downsampled`）本就有 combine 输出错开防护，
不在本条范围，未改动。

### 回归测试（tests/shared/test_fix_effect_pass_aud5b.py）

- `TestSeparableTargetSelection`：四种输入别名（a/b/c/外部）的路由表
  （写目标序列 + 输出 glo）+ 通用不变量
  `test_no_read_write_alias_for_any_input`——用事件记录器（每次 FBO 绑定/
  纹理绑定/draw 均记录）直接检验 GL 不变量：**任一 draw 时，当前帧缓冲
  附件纹理不得绑定在任何采样单元上**（绑定持续到被覆盖，故按累积
  unit→texture 映射保守检查；这些路径在 draw 前重绑全部采样单元，无误报）。
- `test_v_pass_samples_h_output_and_pristine_input`：确认重路由后 V pass
  unit 0 = H 输出、unit 1 = 纯净输入（mask/mix 契约未破坏）。
- 真实 GL 链（`TestRealChainNoFeedback`）：
  - [sharpen → blur(mix=0)]：blur 输出必须逐字节等于其输入（mix=0 →
    V shader 直通 u_original）。旧代码在本机驱动上实测失败（反馈回路
    使 u_original 读到 clear 后的 0 → 输出被破坏）。
  - [sharpen → denoise → blur(mix=0)]：同上，覆盖输入 == scratch_b 场景。

---

## 2. AUD-26402 — `_run_glow` pass 1 固定写 scratch_b [FIXED]

### 根因（复核属实）

FX-M02 的 scratch_c 防护（原 1243-1246）只覆盖 pass 2 的输出别名
（输入==scratch_a 时）。pass 1（threshold + horizontal blur）无条件写
scratch_b（原 1249），而链 [效果1 → 效果2 → glow]（效果2 为
`_run_single_pass`，输入 scratch_a，输出 scratch_b）时 glow 的输入即
scratch_b：pass 1 写 scratch_b 的同时经 unit 0 采样 scratch_b——同一
反馈回路类缺陷，bloom 通道读到 clear 后的 0，输出黑帧/损坏。

### 修复（pass 1 目标同样按输入别名动态分配）

把 FX-M02 的输出选择扩展为「两个 pass 目标联合分配」，维持不变量
`bloom ≠ input` 且 `output ∉ {input, bloom}`（三元互异）：

```python
if input_glo == self._scratch_a.glo:
    bloom_tex, bloom_fbo = self._scratch_b, self._scratch_b_fbo
    out_tex, out_fbo = self._scratch_c, self._scratch_c_fbo
elif input_glo == self._scratch_b.glo:
    bloom_tex, bloom_fbo = self._scratch_a, self._scratch_a_fbo
    out_tex, out_fbo = self._scratch_c, self._scratch_c_fbo
else:
    bloom_tex, bloom_fbo = self._scratch_b, self._scratch_b_fbo
    out_tex, out_fbo = self._scratch_a, self._scratch_a_fbo
```

- pass 1 渲染进 `bloom_fbo`；pass 2 的 `self._scratch_b.use(1)` 改为
  `bloom_tex.use(1)`（u_bloom_tex 绑定 pass-1 实际目标），仍用 moderngl
  原生 `Texture.use(unit)`（保留 2026-09-17 standalone-context 安全性
  修复的写法）；
- 外部输入路径（bloom=b / out=a）与 FX-M02 路径（输入==a → out=c）的
  既有行为逐字节不变——tests/edit/test_effect_pass_runner_glow_feedback.py
  全部原样通过；
- 输入为 scratch_c 的链（glow → glow 场景，第一次 glow 输出 c）走 else
  分支规范路由，无别名。

### 回归测试

- `TestGlowTargetSelection`：四种输入别名的双 pass 路由表 + 输出 glo +
  `test_no_read_write_alias_for_any_input` 通用不变量（对**两个** pass
  生效）；`test_input_is_scratch_b_pass1_avoids_scratch_b` 额外断言
  unit 1 绑定的是 pass-1 目标（scratch_a）而非输入 scratch_b。
- 真实 GL 链 [sharpen → denoise → glow(threshold=2.0)]：threshold 高于
  任何 f1 亮度 → bloom 恒 0 → glow 输出必须逐字节等于其输入。旧代码
  在本机驱动上实测失败（bloom 采样自身写入目标，输出被破坏）。

---

## 验证记录（全部实际执行）

1. `py_compile`：
   ```
   $ .venv/bin/python -m py_compile src/features/shared/effects/effect_pass_runner.py
   COMPILE_OK
   ```
2. 新测试：`tests/shared/test_fix_effect_pass_aud5b.py` → **20 passed**
   （含 3 个真实 GL 链测试，本机 GL 可用未跳过；0 skipped）。
3. 旧代码反向验证（证明测试确锁旧行为，非空转）：临时以 `git show
   HEAD:` 的修复前版本替换源文件后运行新测试 → **9 failed, 11 passed**，
   失败项恰为两条发现对应的选择契约/不变量/真实 GL 逐字节断言
   （separable scratch_a/scratch_b 路由与不变量、glow scratch_b 路由与
   不变量、3 条真实 GL 链）。随后恢复修复版并重新 `py_compile` + 全绿。
   （该操作仅读取 git 对象 + 文件级拷贝还原，无任何分支/commit 操作。）
4. 邻接既有测试（grep tests/ 中 EffectPassRunner/glow/separable/blur/
   effect 相关）：
   - tests/edit/test_effect_pass_runner_glow_feedback.py（FX-M02 契约）→ 3 passed
   - tests/edit/test_gpu_blur_channel_mask_contract.py → 6 passed
   - tests/edit/test_effect_surface_precision.py → 10 passed（另在组合跑中重复一次）
   - tests/edit/test_effect_golden_frame_parity.py → 11 passed
   - tests/edit/test_effect_pass_runner_fallback_lifecycle.py → 3 passed
   - tests/edit/test_effect_material_parity.py → 16 passed
   - tests/edit/test_timeline_export_effects.py → 29 passed
   - tests/shared/test_dctl_and_preview.py → 35 passed
   - tests/shared/test_effect_phase2_specs.py + tests/shared/test_effect_spec.py
     + tests/edit/test_effect_capability_diagnostics.py
     + tests/edit/test_effect_catalog_capabilities.py → 组合 177 passed

   合计 0 失败、0 跳过锁定旧行为的用例：既有测试均锁定「外部输入走规范
   路由 / FX-M02 输出契约」，与本次动态分配修复兼容，无需协议处理。

## 备注 / 未越界项

- 同批 AUD-26403~26406（Suggestion）不在本批任务范围，未改动。
- `_run_glow` 文档串与尾部 unit-1 注释同步更新为 bloom 动态路由语义。

# L03 双源转场接缝 · 收口报告（2026-10-25）

> 角色：Mac 主集成者。计划 §8 L03 / §2.2 单一写入者文件窄改。
> 试点：X01 clock_wipe（mask 类）+ X09 true_push（首个双源重采样类）。

## 落地的接缝（按 patches/proposal_l03_transition_seams.md 方案 A/B）

| 文件 | 变更 |
|---|---|
| `src/features/edit/transition_packs/library_v1/cores.py`（新） | 生产核：clock_wipe_mask（极角推进）+ push_frame（双源平移，含 seam_softness 接缝羽化与 _safe_float 家族防御）；**暂存区组件 *_core.py 改为再导出本模块**（单一事实源，禁双份漂移） |
| `transition_catalog.py` | 注册 lib1_clock_wipe / lib1_true_push（spatial=True）；新增 RESAMPLE_TRANSITION_TYPES 与 LIB1_TRANSITION_TYPES 集合 |
| `transition_mask.py` | 分派：lib1_clock_wipe→生产核；lib1_true_push→常数 progress mask（通用 mask 消费方安全语义） |
| `transition_mask_gpu.py` | spec 新字段 center/resample_mode/push_direction；kind=5 时钟（angle 复用为 start_angle、reverse 为逆时针、softness 为归一角宽×2π——与 wipe 长边口径差异已注释）；push spec resample_mode=1 |
| `gpu_transition_shaders.py` | 新 uniform u_center/u_resample_mode/u_push_dir；procedural_mask kind=5 分支（atan 北 0 顺时针正，aspect 校正）；main() resample push 分支（双源 UV 位移+接缝羽化；轴向单位向量 bug 自查修复） |
| `edit_gpu_preview_renderer.py` / `timeline_gpu_export_renderer.py` | 两处 _set_transition_uniforms 增 3 个新 uniform（成员存在性守护，旧 spec 链接剔除不受影响） |
| `timeline_preview_renderer.py` / `timeline_export.py` | CPU 合成分支：RESAMPLE 类型在 mask 路径前分流到 push_frame（预览 uint8/导出 float 两口径）；metal 后端对 LIB1 类型显式 `_warn_metal_fallback("lib1_transition_metal_unimplemented")` 回退 CPU（MSL 未实现，禁静默错渲染/禁替换 Cross Dissolve） |

## 证据（tests/edit/test_lib1_transitions.py 15 绿 + 既有套件 86 绿）

环境同 L02（GL 4.1 / RX 6800 standalone）。命令：

    .venv/bin/python -m pytest tests/edit/test_lib1_transitions.py tests/content_library_v1/ -v
    .venv/bin/python -m pytest tests/edit/test_native_transitions.py tests/edit/test_transition_effects.py \
        tests/edit/test_transition_mask_gpu.py tests/edit/test_custom_transitions.py \
        tests/edit/test_transition_and_slide_transactions.py -v

- CPU：dispatch↔生产核逐位一致；端点精确；TransitionInstance 白名单接受新类型；
  **两源特征轨迹测试**（A/B 特征列同向移动——与 slide 的机器可辨差量）；
  坏参数（NaN/非法方向）回退不抛（AUD-19210 家族口径）。
- GPU spec：kind=5/resample 字段与防御；旧类型 spec 不受影响（回归断言）。
- **真实 GL**：转场 shader 全量编译含新 uniform；kind=5 mask 渲染↔CPU 核对拍
  （顺/逆时针两组，atol 1e-4）；push 渲染↔CPU 核对拍（atol 6e-3，含渐变源）；
  push 端点 p=0/1 精确还原 A/B。
- 回归：既有转场五套件 86 绿零新增失败。

## 口径与未验（如实）

- GL 对拍使用与生产一致的顶点约定（in_pos→v_texcoord）+ 翻转视图；完整
  `EditGpuPreviewRenderer._preblend` / GL 导出 `GpuWipeElement` 调用链与
  真实时间线端到端（AddTransition→预览→导出→保存重开/Undo）属 L20 验收。
- MSL kernel 未实现 kind=5/resample——按计划"缺后端明确拒绝"显式回退 CPU
  （_warn_metal_fallback），未静默替换；Metal 实现排 L14 批次或后续。
- 用户转场 JSON base 白名单（15 项）未扩——按提案随 L05 配方化一起。
- seam_softness 的 GL 羽化公式与 CPU 同式（已实现）；GL 侧羽化专项对拍
  未单列用例（CPU 侧已测，公式同构），L20 补端到端。

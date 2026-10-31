# 提案：L03 双源转场接缝（X01 mask 类 + X09 重采样类，给 Mac 主集成者）

> 状态：提案文本 + 暂存 CPU 核（component_ready），未改任何生产文件。
> 依据：L00 §5 接缝事实 + X01/X09 组件交付。

## 现状接缝（L00 盘点冻结）

现有转场管线单一合成形态：**几何 mask × progress + left·(1-m)+right·m**。
- mask 类（X01/X02/X03/X05–X08/X18）：mask 生成是纯几何函数，扩展点集中；
- 重采样类（X04/X09–X17）：需要**双源 UV 位移通道**，目前完全不存在
  （whip_pan 的 warpAffine 是 CPU 源侧特效预 pass，非双源重采样合成）。

## 建议窄接缝（不建平行时间线，不改 EffectSpec 为万能容器）

### A. mask 描述符扩展（X01 起）

1. `transition_catalog.py` 新增 `TransitionDefinition`（id=`lib1_clock_wipe`，
   category Motion，params schema 同组件 manifest；注册进
   `SPATIAL_TRANSITION_TYPES`）。
2. `transition_mask.py::compute_transition_mask` 新分支：先查 lib1 注册表
   （避免 if-chain 膨胀：`_LIB1_MASK_KERNELS = {"lib1_clock_wipe": clock_wipe_mask}`，
   核函数由 shared 层提供——见 §C 摆放）；坐标基复用 `_get_coord_grid`
   的 aspect 校正缓存。
3. `transition_mask_gpu.py::build_gpu_transition_mask_spec`：新 kind=5
   （CLOCK_WIPE）+ 字段 `center: tuple[float,float]`（建议进 spec dataclass，
   默认 (0.5,0.5) 保持既有构造兼容）；`start_angle` 复用 `angle` 字段
   （度），`clockwise` 复用 `reverse`，`softness` 复用 `softness`（归一语义
   与 wipe 同族，但乘 2π 而非长边比例——**语义差异写入字段注释**）。
4. `gpu_transition_shaders.py` procedural_mask() kind==5 分支：按
   `transitions/clock_wipe/clock_wipe.glsl` 草稿（atan/atan2 三角函数为
   首次进入转场 shader，编译门禁需真实覆盖）。
5. **MSL 同步**：`timeline_metal_export_renderer.py` `timeline_transition_mask`
   kernel 同步 kind=5（TransitionMaskParams struct 增 center 字段）。
   ⚠️ Metal iris 旧半径（2872 行）未跟上 AUD-16900 的先例证明三端失同步
   是真实风险——建议本包同时补齐 iris 的 Metal 半径对齐，作为三端同步
   纪律的第一个修复（可独立小提交）。

### B. 双源重采样通道（X09 起，首个 sample 子门）

1. `GpuTransitionMaskSpec` 增 `resample_mode`（0=none，1=push）+
   `push_direction`；或 effect_mode=6——**建议独立 resample_mode 字段**，
   避免与 motion-blur effect_mode 语义纠缠。
2. `gpu_transition_shaders.py` main()：resample_mode!=0 时跳过 mask 混合，
   走 push 分支（草稿见 `transitions/true_push/true_push.glsl`）；
   mask 语义=常数 1（spec 侧 kind=0 占位）。
3. CPU 侧：`transition_effects.py` 新增 `apply_push_transition(a, b, progress,
   params)`（基于 shared 层 push 核），`timeline_render_plan`/
   `timeline_export` 的合成分支按类型路由（与 full-frame 族同位）。
4. 预览/导出门控：`timeline_preview_renderer._build_gpu_transition_jobs` 与
   `timeline_export._is_gpu_two_source_transition` 放行新类型；
   spec 失败仍走既有整帧 CPU 回退（天然安全路径）。
5. MSL：`timeline_wipe_composite` kernel 增 push 分支（或独立 kernel）。

### C. 核摆放（shared 层）

CPU 核（clock_wipe/true_push）放 `src/features/edit/transition_packs/library_v1/`
（计划 §2.1 拟定路径），由 transition_mask/effects 的窄 dispatch 引用；
上层不反向导入 Composite。GLSL 核进 gpu_transition_shaders 常量块。

### D. 验收门（X01/X09 进产品前）

- 切点逐帧（0/1/2 帧、奇偶时长、start/center/end 对齐）；
- X09 源 UV 轨迹测试（两源特征点都移动——与 slide 的机器可辨差量）；
- 保存重开/Undo/锁轨零写入；真实 GL 预览+GL 导出逐帧对拍 CPU oracle；
  MSL 子集对拍（缺实现明确拒绝，不静默 Cross Dissolve）；
- `custom_transitions._BASE_TYPES` 白名单扩展（lib1_clock_wipe 等）随 L05
  配方化一起做，不在本提案内。

## 用户转场 JSON

新 base 进白名单前，用户 JSON 不能引用 lib1_* base（既有行为：未知 base
拒绝+warning，安全）。白名单扩展与参数 schema 校验（center/softness 数值
clamp 沿用 `_safe_float` 防御惯例 AUD-19210）随本提案 A.1 一起。

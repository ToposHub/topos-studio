# 提案：F16 gradient_map 的 N 停靠原生参数适配（给 Mac 主集成者）

> 组件：F16/C01（共享核）。状态：提案文本，未改任何生产文件。
> 触发条件：需要 >4 停靠的 gradient map 时。当前发布版固定 4 停靠（ABI 内合法）。

## 问题

`.tglsl @param` 四类（slider/checkbox/color/select）无法表达变长停靠点数组。
把 N 停靠拆成 N×(color+slider) 只在 N=4 时可读；N>4 时 UI 与程序缓存键都会失控。
计划红线：不把数组塞进现有 slider，也不静默把 4 停靠降级成另一份 Duotone。

## 建议的最小适配（宿主侧三处窄改，均由集成者串行实施）

1. **ParamDef 扩展一个 ptype**：`color_stops`——值为 `[(pos: float, rgb: tuple), ...]`
   列表（升序约束在 setter/UI 校验）。Composite `base_node.ParamDef.ptype` 枚举
   加一项 + `is_animatable()` 返回 False（列表关键帧另议，不在本提案）。
2. **Color 页 shader_compiler 的 uniform 展开协议**：`color_stops` 参数编译为
   `uniform vec3 u_P{n}_stop[{MAX_STOPS}]; uniform float u_P{n}_stop_pos[{MAX_STOPS}];`
   （MAX_STOPS=8 常量上限 + `u_P{n}_stop_count`），插件源侧提供配套 helper
   `vec3 gm_eval_stops(float t)`；低于上限的槽位用端点停靠填充。
   与既有裸名改写机制同层（shader_compiler.py:420-426 一带），不新增第二套改写器。
3. **Edit 页 EffectPassRunner 打包器**：`_pack_plugin_params` 对 `color_stops`
   展开为上述数组 uniform（effect_pass_runner.py:1599-1654 一带加一个分支）。

## 兼容

- 4 停靠版本（本试点交付）不受影响：全部参数仍是四类标量。
- `color_stops` 参数的 `parameter_schema_hash` 把列表 canonical JSON 计入
  （manifest.parameter_schema_hash 已按此语义实现）。
- 未升级宿主载入带 `color_stops` 的工程：走既有未知参数保留策略
  （`preserve-payload-warn`，效果按 4 停靠回退渲染并警告），不静默丢层。

## 共享核摆放（C01 复用）

建议 GLSL 核（分段求值函数）放 shared effects 层（如 `src/features/shared/effects/`
新增 `library_kernels.py` 或并入 shaders 常量文件），Composite 的
`_LIB1_GRADIENT_MAP_FRAGMENT` 与 Color 页 `.tglsl` 引用同一份分段语义；
上层不反向导入 Composite 内部代码（L 计划 §2.1）。

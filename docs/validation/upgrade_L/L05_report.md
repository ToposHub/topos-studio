# L05 内容库入口与预设 · 收口报告（2026-10-25）

> 角色：Mac 主集成者。范围：三类各 1 个真预设（计划 §8 L05 最小口径）。

## 落地

1. **用户转场 base 白名单扩展**（custom_transitions.py）：lib1_clock_wipe /
   lib1_true_push 进 `_BASE_TYPES` 与 `_SPATIAL_BASES`（双源合成家族判定，
   user_* 继承进入 GPU 门）——用户 JSON 配方（P33-P56 载体）自此可用。
2. **user_* push 分流修复**：预览/导出 CPU 合成分支改按
   `resolve_transition_base_type` 判定 RESAMPLE——否则 user 配方静默退化
   为普通溶解（真实缺陷，随白名单扩展暴露并修复）。
3. **内置合成预设**：`presets/LIB1_Gradient_Tone.json`（lib1_gradient_map →
   lib1_edge_extend 链；preset_manager 顶层扫描机制，内置只读定位；
   误放 System/ 子目录经实测发现不被扫描——已修正）。
4. **P29 四色渐变配方**（presets/p29_quartet_gradient/）：源 manifest
   （recipe.json，不冒充宿主格式）+ convert.py 两类宿主载荷：
   Edit EffectInstance.params（plugin:LIB1_Gradient_Map，select 用 label
   口径）与 Color layer["plugins"] 实例条目（layer.py:566 四字段）。
   应用入口走既有 AddEffectCommand / 层插件机制；版本快照由 M07.1
   plugin_identity 链在应用时完成（不在转换层伪造）。

## 证据（tests/content_library_v1/test_l05_entries.py 7 绿 + 回归 126 绿）

- 真实用户 JSON：lib1 base 装载→resolve→CPU mask 经 base 解析与直接调用
  逐位一致；user_push base 解析进 RESAMPLE 集。
- 内置预设经真实 PresetManager.load_preset 加载，节点经 registry
  from_dict 反序列化。
- P29 载荷形状（两宿主口径）+ 参数对真实插件 @param schema 的
  min/max/类型对齐（真实 parser 校验）。

## 未验/后续（如实）

- Color .topos/sidecar 的真实落盘预设：.topos 序列化经引擎 save_router
  全链（M07 身份/快照），P29 的 Color 载荷形态已就绪、端到端落盘随 L20
  示例工程一起（不在本包伪造 .topos 字节）。
- 宏导入（P57+）与批量应用预检/事务：L18 批次。
- 内容面板分类/收藏/最近使用 UI：面板自动聚合已注册项（效果面板经
  catalog、节点面板经 registry、转场面板含 My Transitions 分组）——
  LIB1 项可见性依赖 L20 发布门后按 §10 状态推进，无新增旁路。

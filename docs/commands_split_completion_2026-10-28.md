# P6 commands.py 拆分·完成登记（2026-10-28）

## 终态

`src/features/edit/core/commands.py`（19,719 行单文件，127 类 + 13 函数）
已包化为 `commands/` 包，13 个文件共 20,208 行（+489 行为各模块
docstring/imports/logger/re-export 门面开销，逐刀对账可查）：

| 文件 | 行数 | 内容 |
|---|---|---|
| `__init__.py` | 1,151 | 撤销栈基础设施 750 行（EditCommand 协议/CommandStack/ProjectCommandStack/MacroCommand/ProjectMacroCommand）+ 九刀 re-export 门面 + `_MACRO_SUBCOMMAND_TYPES` 注册表 94 行 |
| `_support.py` | 71 | 真共享符号：assert_track_editable / ProjectTransactionResult / _clips_from_dicts / _transitions_from_dicts |
| `audio_commands.py` | 1,613 | 混音器 14 类 + 自动化 4 类 |
| `color_commands.py` | 752 | 调色栈 5 类 |
| `transition_commands.py` | 860 | 转场 3 类 |
| `marker_commands.py` | 519 | 标记 4 类 |
| `subtitle_commands.py` | 890 | 字幕 8 类 + helper 2 |
| `track_commands.py` | 1,061 | 轨道 10 类 |
| `asset_commands.py` | 2,943 | 素材/媒体库 29 类 + helper 4 |
| `effect_commands.py` | 2,072 | 参数/关键帧/效果栈 10 类 |
| `clip_commands.py` | 4,370 | 片段生命周期 16 类（含 MacroCommand 硬耦合三类）|
| `clip_props_commands.py` | 2,119 | 片段属性 14 类 |
| `ripple_commands.py` | 1,787 | 插入/波纹删除 6 类 |

git log 可追溯每簇搬移：91f2e340f(包化)/a11d2d73a(补记)/4a07f04f8(audio)/
472355cee(color)/8c8b724cf(transition+marker)/80d1c1db1(subtitle+track)/
f54a69ccf(asset)/92c4563d6(effect)/df38baf91(clip)/703383b02(props+ripple)。
每刀均通过：字节级正反向重构核验、序列化键不变、`_MACRO_SUBCOMMAND_TYPES`
90 键保序、tests/edit 全量失败集合 ⊆ 当次基线（终态 13 失败 = 既有基线
逐条相同）、ruff（E9+F）全绿、行数逐项对账。

## infra（stack.py）是否再拆的评估（计划步骤 4"单独评估，不做也成立"）

**结论：不再拆。** 理由：
1. `__init__.py` 已从 19,719 行降到 1,151 行（5.8%），可维护性目标已达成；
2. 留守内容 = 撤销栈基础设施 + 兼容门面，本身就是"栈模块"语义上最自然的
   包核；再拆 stack.py 后 `__init__.py` 仍需保留全部 re-import（137 个测试
   深导入的兼容面），文件数+1 而净收益≈0；
3. MacroCommand 对 Split/Overwrite/SetClipLinked 的 isinstance 硬耦合在
   第七刀已验证经 re-export 名字解析正确（61 项冒烟），infra 不动最稳。

## 遗留（登记，非阻塞）

- i18n 二轮 31 处（addMenu/getText/_Q 别名）在清单附录，待下轮批次；
- 计划预估"10+ 次往返"实际 10 刀完成（含 2 处测试适配：FX-M10 守卫升级
  为包级扫描、wd9g55 打桩随定义走）。

# EFR 复查：composite/core（E-rollup 模块行）

- 复查对象：`src/features/composite/core/` ｜ 提交 `47cbf450f21178b3cd1e09e0dda8a25c2d0ade78`
- 执行报告：`fixes/reports/efr-composite-core.md`
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show/grep/py_compile），未运行 pytest
- 复查人：GLM5.3 复查会话

## 七项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | ✅ PASS | 17 个文件 = 16 个 `src/features/composite/core/*.py` + 本批报告；`graph_snapshot.py`（PortRevision 所在）未触碰；REMEDIATION_PLAN.md 未触碰 |
| 2 | 未用导入删除抽查 | ✅ PASS | 抽 8 个被删绑定，当前全仓零引用；typing 收敛的文件-名字映射与报告逐条吻合（node_engine=TYPE_CHECKING、tile_scheduler=Dict+Optional、time_remap=List、media_drop_payload=Tuple、node_capabilities=FrozenSet） |
| 3 | 死代码删除抽查 | ✅ PASS | `get_all_node_types`/`get_all_categories` 全仓零引用；**PortRevision 保留理由成立**（测试类 `TestPortRevisionRegistration` 与 docs/composite_optimization_plan_2026-09-07.md:67 均以其点名）；`TileSizeRecommendation` 兼容 shim 保留于 tile_scheduler.py:112 |
| 4 | 注解零行为证明 | ✅ PASS | 41 处全部为签名行 `-> None` 追加；锁/线程契约文件（image_loader/undo_stack/node_engine/layer_stack_controller）方法体零改动（diff 中非 def 非 import 的改动行为 0） |
| 5 | 红线 | ✅ PASS | 裸 `except:` 当前 0 命中（报告称既有即 0，一致）；无信号/connect 改动；无 class 行改动；QThread 生命周期方法（run/cancel 等）仅加 `-> None` 不改返回语义 |
| 6 | py_compile | ✅ PASS | 当前 HEAD 抽 8 个改动文件（node_engine/image_loader/undo_stack/tile_scheduler/video_context/media_drop_payload/tile_padding_computer/layer_stack_controller）全部通过 |
| 7 | 综合结论 | **PASS** | 机械清理成立；报告自披露的"误删-复原"事件在净 diff 中无残留（已核验） |

## 抽查明细

### 未用导入（8 绑定）

| 被删绑定 | 所在文件 | 当前全仓 grep | 判定 |
|----------|---------|--------------|------|
| `import traceback` | image_loader.py | `image_loader.traceback` 零命中；文件内 `traceback` 残留 0 | 安全 |
| `import os` | node_plugin.py | `node_plugin.os` 零命中 | 安全 |
| `is_effect_node_type` | tile_padding_computer.py | 限定名零命中；该函数在定义处 node_capabilities.py 保留 | 安全 |
| `GraphSnapshot`（顶部导入） | tile_scheduler.py | `tile_scheduler.GraphSnapshot` 零命中；底部兼容 shim（TileSizeRecommendation）未动 | 安全 |
| `import numpy as np`（TYPE_CHECKING 块内） | video_context.py | `video_context.np` 零命中 | 安全 |
| `field` ×6 | asset_dependency / layer_provenance / node_capabilities / node_plugin / tile_scheduler / video_context | 各文件当前均无 `field(` 使用（media_drop_payload 与 tile_padding_computer 的 field **保留在位**——误删复原核实） | 安全 |
| `Tuple` / `FrozenSet` / `Dict`+`Optional` / `List` / `TYPE_CHECKING` | media_drop_payload / node_capabilities / tile_scheduler / time_remap / node_engine | 文件内残留 0 | 安全 |

### 死代码（2 函数）

| 被删定义 | 全仓 grep（src/tests/scripts/plugins/docs） | 判定 |
|----------|-------------------------------------------|------|
| `get_all_node_types` | 零命中（唯一历史痕迹为审计/报告文档） | 安全 |
| `get_all_categories` | 零命中 | 安全 |

未配对 def 移除仅此 2 个，与报告完全一致（41 个注解签名均有 +/- 配对）。

### 保留候选核实

- `PortRevision`（graph_snapshot.py:18）：当前存在；`tests/composite/test_fix_wd4g33_aud.py:787` 的 `TestPortRevisionRegistration` 与优化方案文档点名——保留理由属实。
- `tile_scheduler.py:112` `TileSizeRecommendation` re-export：在位（显式 Backward Compatibility 注释块）——保留正确。

## 发现

1. **（Note，正面）** 报告 §"过程记录"主动披露初轮误删 `media_drop_payload.py`/`tile_padding_computer.py` 的 `field` 并经导入冒烟当场复原——本次复查在当前 HEAD 验证两文件 `from dataclasses import dataclass, field` 完好在位、净 diff 无痕，自纠错记录与事实相符。
2. **（Note）** 报告统计与 git stat 一致：源码 +53/−75（145−92 报告行 / 75），16 文件清单逐一对上。

## 结论

**PASS**。删除证据、保留理由、注解纯度全部核验通过。建议勾选该 E-rollup 行。

# CW-B10 验收报告:Color Warper 后端

日期:2026-11-12。对应计划 `docs/plans/2026-11-12-color-warper-backend-plan.md`。
机器:macOS 24.3 x64,AMD Radeon RX 6800(GL 4.1),PyAV 18.1.0,Python 3.12.14。

## 1. 证据分级结论(计划 §5.4 要求五类分开陈述)

| 类别 | 结果 | 证据 |
| --- | --- | --- |
| 纯 UI 测试 | ✅ 通过 | `test_warper_resolve_ui.py` 22 项(既有)+ 本轮 UI 回归;模式隔离/选择/拖动/Luma 展示/手势隔离/浮窗/大画布 |
| 三份真实媒体 fixture | ✅ 通过(CPU 链路冒烟) | `report.json`:每 fixture 首/中锚点帧 × HSV/HSL/HSP:identity(≤2e-6)、全有限(无 NaN/Inf)、确实改变画面、重复求解逐位一致;等步长采样(步长 8,每帧 ~32k 像素),全帧逐像素在 GPU 生产路径 |
| 合成色彩科学夹具 | ✅ 通过 | `test_warper_color.py` 26 项(六扇区/环绕/灰阶/黑/负通道/RGB>1/极值/round-trip 阈值/HSP 常数);`test_warper_color_parity.py` 9 项(真实 GPU 执行生产 GLSL 单函数+整管线) |
| 实际 GPU 平台 | ✅ GL(AMD RX 6800);⏳ Metal 数值 parity 待 Metal 矩阵 | MSL 保持 legacy-only 源级 parity;space>0 在 Metal 后端按 capability 拒绝;图 Metal 后端本就拒绝 warper |
| 4K 性能 | ⏳ 待验收 | 本轮未跑 4K 播放基线(CPU 冒烟耗时见 report.json 仅供参考;生产为 GPU 路径);4K 素材已在 B00 sibling_assets 登记 |

## 2. 阶段测试台账

| 阶段 | 测试文件 | 数量 | 覆盖 |
| --- | --- | --- | --- |
| B01 模型/迁移 | test_warper_state.py / test_warper_state_layer.py | 41+9 | schema v2 round-trip、未知版本原文保留、冻结数组、重采样、capability、Layer 集成 |
| B02 命令/手势 | test_warper_commands.py / test_warper_command_adapter.py | 19+18 | 一次拖动一次 undo、零位移零历史、cancel 干净恢复、revision/目标保护、legacy 去重 |
| B03 色彩科学 | test_warper_color.py / test_warper_color_parity.py | 26+9 | 冻结坐标/常数/域、GPU 真实执行 parity |
| B04 HS 求解 | test_warper_hs_solver.py | 20 | 权重/AutoLock/接缝环绕/Pin/平滑/Pull-Push/Luma 升级 |
| B05 CL 双网格 | test_warper_cl.py | 17 | 坐标/角度/合成次序/灰阶单调/Auto Pin |
| B06 CW 求解 | test_warper_cw_solver.py | 16 | 权重/混合/Pin 保护/极端参数/确定性/HDR |
| B07 密度云 | test_warper_density.py | 11 | 真实采样/标签/代际/节流/取色坐标 |
| B08 渲染集成 | test_warper_render_integration.py | 6 | 管线编译/manifest/DTO/整管线 GPU parity |
| B09 导出/粘贴 | test_warper_export_paste.py | 8 | LUT 点态烘焙逐点对照/不可表达拒绝/图粘贴域 |

## 3. 已启用能力(真实图像处理证据)

- **HS 网格(HSV/HSL/HSP,含非方阵)**:GPU 管线渲染(legacy 路径 + v2 变体)、可撤销(B02)、可保存/复制/粘贴(B01/B09)、可导出 LUT(B09,逐点对照)。
- **HS 编辑工具**:位移/数值/分辨率重采样/Pin/Auto Lock/平滑/Pull-Push(Luma 第三分量求解已实现,GPU 渲染如实拒绝)。
- **CL/CW**:CPU 求解器与模型完整、可保存/复制;GPU 渲染与 LUT 表达按 capability 拒绝(诚实,不伪造)。
- **legacy HSV 旧工程**:画面零回归(镜像测试 + 源码逐字未动 + 95→309 项回归)。

## 4. 明确待验收项(不冒充完成)

1. B00 §4 实机 Resolve 复核清单(候选列表/单位/撤销次数等)——补齐前相关默认值仍标 Topos 声明。
2. Metal 数值 parity(MSL v2 变体未实现,Metal 后端拒绝 space>0)。
3. 4K 播放性能基线与 100 轮资源矩阵(需长时实机;GPU 资源生命周期已由既有 UPDATE_AUX_TEX/deferred-garbage 路径承载,`test_color_graph_cancel_resources` 回归通过)。
4. 密度云/取色的 UI 面板接线(B07 契约已测,面板消费待接入)。
5. CW 笔画/Pin 的 GPU 求解(B06 CPU 求解器已定;shader 化待后续批次)。

## 5. 复核命令

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/color/test_warper_*.py -q   # 全部 warper 后端+UI
.venv/bin/python scripts/validate_warper_b10.py                                       # 真实素材冒烟
.venv/bin/python scripts/probe_warper_fixtures_b00.py --skip-hash                      # 素材基线快速复核
```

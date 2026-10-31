# onnxruntime-directml 决策证据（W4-03 前置 / 开放问题 6，本机）

- 日期：2026-09-12 ｜ GPU：AMD Radeon RX 6800 ｜ ort 1.24.4 / Python 3.12
- 运行方式：**临时 venv**（`%TEMP%\topos_dml_probe_venv`），未进项目依赖。
  脚本 `scripts/probe_directml_windows.py`。
- 模型：Conv+Relu×2+Conv（16 通道，1×3×720×1280——AI 降噪类形状）。

## 实测数据

| 项 | CPU EP | DML EP |
|---|---|---|
| 激活 provider | CPUExecutionProvider | **DmlExecutionProvider**（真实走 GPU） |
| 热稳态推理 | 17.4~17.8 ms/iter | 15.6~17.3 ms/iter |
| session 创建 | 0.02 s | 0.36~0.71 s（含 DML shader 编译） |
| 输出一致性 | 基准 | max_abs_diff = 0.0（本模型） |
| 包体占用 | — | **+70.9 MB**（DirectML.dll 18.5 + onnxruntime.dll 21.1） |
| 许可 | — | MIT（无分发阻塞） |

## 对开放问题 6 的裁决输入

1. **技术可行性确认**：DML EP 在 AMD（RX 6800）上开箱即用，不需要
   NVIDIA——DirectML 正是计划 §2.3 说的「不依赖 NVIDIA 的 GPU compute
   通道」。
2. **性能边界诚实记录**：16 通道小模型上 DML 与 CPU 打平（GPU 利用率
   不足，kernel 启动开销占主导）。收益要到**重模型**（真实 AI 降噪
   ONNX，通常几十通道 × 多层）才能体现——引入依赖前应先用目标模型
   复测，不能用本 probe 的 10% 差距做引入/拒绝依据。
3. **体积是主要代价**：+71MB 进分发包。若引入，建议
   `pyproject` extras（`[project.optional-dependencies] directml`），
   基础安装不携带；能力探测失败显式诊断回退 CPU（W4-03 既有验收线）。
4. **建议的决策形态**（供拍板，不代替拍板）：技术可行 + 许可无阻塞 +
   体积可控（extras 化）；触发条件 = 有真实 ONNX 模型需求（如 AI 降噪
   立项）时引入，并先用该模型 A/B。纯「为 compute 而引入」不符合
   K-W4 语义，不建议。

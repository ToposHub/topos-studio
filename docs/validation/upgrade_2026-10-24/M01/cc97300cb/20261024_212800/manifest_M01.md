# M01 交付 manifest · 隔离并修复 Mac 运行依赖组合

> HEAD：`cc97300cb`；执行宿主：Darwin x86_64 / Python 3.12.13；日期：2026-10-24。
> 对应：升级计划 M §M01（P1，前置 M00）；对应缺口 M-I01。

## 1. 三个受支持安装组合（全部 pip check=0，证据 [pip_checks.log](pip_checks.log)）

| 组合 | 环境 | 依赖定义 | pip check | 关键验证 |
|---|---|---|---|---|
| 核心 UI/视频 | `.venv-m01-core`（387 包，[freeze](core_env_freeze.txt)） | 主 venv 冻结 − AI 栈闭包（`scripts/m01_core_freeze_filter.py` 计算） | **0 冲突** | M00 focused 门禁 156 绿（OCIO/Qt/解码全链）；torch/transformers 缺席时 AI 能力准确诊断（`ModelPackNotInstalledError` / vault `_check_dependencies=False` / nemo `find_spec→disabled`），核心应用可用 |
| AI worker · 组合A「ASR 主链」 | `.venv-m01-ai`（97 包，[freeze](ai_env_freeze.txt)） | `requirements-ai-worker.txt` | **0 冲突** | **真模型小样 PASS**：Qwen3-ASR-0.6B（HF 缓存）加载 + `transcribe()` 端到端执行（1s 合成样本，返回 `ASRTranscription`）；qwen_asr/sentence_transformers 导入 OK |
| AI worker · 组合B「TTS 专用」 | `.venv-m01-tts`（92 包，[freeze](tts_env_freeze.txt)） | `requirements-ai-worker-tts.txt` | **0 冲突** | numpy/transformers==4.57.3/qwen_tts 导入 OK |

构建入口：`scripts/m01_build_envs.sh [core|ai|tts|all]`；现有 `.venv` 原样保留供对照（未原地升级，其 pip check 仍报 M-I01 三处——作为开工期历史）。

## 2. 四项冲突逐项处置（原 M-I01 三处 + 过程中新发现一处）

| # | 冲突 | 处置 |
|---|---|---|
| 1 | pyannote-core 6.0.1 需 numpy>=2.0 vs 本机 1.26.4 | **移除**：pyannote 已被 R4-c（Qwen3-ASR+Sortformer）彻底替换（`model_pack_manager.py:223`），src 零直接导入；且 torch 2.2.2（x86_64 macOS 上限）与 numpy≥2 运行时不兼容，pyannote 6.x 在本宿主不可支持 |
| 2 | lightning 2.4.0 需 packaging<25 vs 26.3 | worker 环境 pin `packaging<25`；核心环境不装 lightning，packaging 26.3 不受影响 |
| 3 | qwen-tts 0.1.1 需 transformers==4.57.3 vs 4.57.6 | 组合A 按 qwen-asr 钉 4.57.6（不含 qwen-tts），TTS 拆组合B |
| 4（新发现） | **qwen-tts 全系（0.0.2–0.1.1）钉 4.57.3 vs qwen-asr 全系（0.0.1–0.0.6）钉 4.57.6** | 上游生态互斥：两个包在任何版本组合下无法共存（逐版本元数据验证）。分组合安装（A/B），各自 pip check=0；上游放宽钉子后可并回——**登记在案，本项目不可修** |

## 3. 宿主限制登记（不可支持项，非失败）

- **torch 上限 2.2.2**：Darwin x86_64 的 PyTorch 轮子止于 2.2.2（2.3+ 仅 arm64），`pip index versions torch` 实测确认。
- **nemo-toolkit（Sortformer 分离）本宿主不可用**：nemo 2.6.1 的 `SortformerEncLabelModel` 导入链需 torch≥2.4 的 `torch.distributed.tensor.parallel.SequenceParallel`；主 venv 同组合同样失败（该链在此宿主从未可导入）。旧 nemo 版本钉旧 transformers 与 qwen-asr 冲突，无可用组合。应用侧已有准确诊断（`transcript_provider.py:713-714` find_spec→"diarization disabled"）。
- **numba/llvmlite 钉版**：resolver 默认选 0.68/0.51+，后者无 x86_64 macOS 轮子且源码构建失败——钉主 venv 已验证的 0.60.0/0.43.0。

## 4. pyproject / requirements / constraints / CI 平台与 Python 差异核对

- **constraints.txt 与 Mac 主 venv 漂移**：42 包版本不同（numpy 2.5.2 vs 1.26.4、PyQt6 6.11.0 vs 6.10.2、av 18.1.0 vs 17.0.1、opencv 5.0.0.93 vs 4.11.0.86 等）——constraints 来自另一较新环境（numpy 2.x 闭包），与 Mac 主 venv 分属不同组合，两者各自内部自洽但互不匹配。**处置**：不重生成（跨平台重生成影响 CI clean-install 行为，超出 Mac 单机 M01 范围）；核心环境按主 venv 冻结复现（已验证组合）。登记为已知差异，跨平台统一归 M18 打包门禁处置。
- pyproject 核心依赖下限与 requirements.txt 一致；AI 栈从未进入两者（注释指引手动安装）——M01 将其固化为 `requirements-ai-worker*.txt` 两个可复现入口。
- 新增 CI `focused-logic-gate.yml`（M00）走 requirements.txt 干净安装，与核心组合互补。

## 5. 验收核对（M01 完成定义）

- [x] 各支持安装组合 pip check=0（3/3，独立验证环境重建）
- [x] 核心解码/OCIO/Qt 通过（156 focused 门禁绿于 `.venv-m01-core`）
- [x] 已启用 AI 真模型小样通过（Qwen3-ASR-0.6B 端到端 transcribe）
- [x] 冻结包记录实际版本/可选能力（3 份 freeze + 本 manifest）
- [x] 缺可选栈时功能准确诊断，核心应用可用（诊断探针三类入口全过）
- [x] 保留现有 venv 供对照，未原地升级

## 6. 未验收项/边界

- nemo Sortformer、qwen-tts+qwen-asr 同环境共存：上游/宿主限制（§2/§3），非本轮可关闭。
- 真模型小样仅 ASR（组合A）；TTS 真模型需 DashScope/HF 凭据与模型下载，本轮仅验证导入与依赖一致性——**待验收**，复验条件见 §3。
- constraints.txt 重生成（跨平台统一）：移交 M18。

## 回滚

删除 `.venv-m01-*` 三环境即回滚；`requirements-ai-worker*.txt`/`scripts/m01_build_envs.sh`/`scripts/m01_core_freeze_filter.py` 为新增文件；`.venv` 与 `constraints.txt`/`requirements.txt` 零改动。

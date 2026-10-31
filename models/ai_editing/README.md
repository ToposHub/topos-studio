# AI 剪辑模型包目录

本目录存放 AI 剪辑（`src/features/edit/ai_editing/`）所需的离线模型权重。

**约定**：每个模型包（pack）落地为一个子目录，子目录名 = `pack_id`（见
`src/features/edit/ai_editing/model_pack_manager.py` 的 `BUILTIN_SPECS`）。
把模型文件放入对应子目录后，还必须加载受控发布清单中的真实目录 SHA-256；只有完整性
校验通过，`ModelPackManager.require_usable(pack_id)` 才会识别为已安装。

> `models/` 已被 `.gitignore` 忽略（模型权重不入库）。本 README 与各子目录的
> `.gitkeep` 通过 `git add -f` 强制入库，用于保留目录结构与说明。

---

## 模型清单（R4-c：已接真实推理）

| pack_id（子目录名） | 用途 | 模型 | 大小 | 设备 | 许可证 | 状态 |
|---|---|---|---|---|---|---|
| `rapidocr_ocr/` | 画面文字识别（OCR） | RapidOCR multilingual v1 | ~15 MB | **CPU 可跑** | Apache-2.0（商用 OK） | ✅ R4-a 已接 |
| `qwen3_asr/` | 语音转录（ASR） | Qwen3-ASR-0.6B | ~1.2 GB | **MLX(Mac)/GPU** | **Apache-2.0** ✅ | ✅ R4-c 已接（中文最优） |
| `qwen3_asr_large/` | 语音转录（ASR） | Qwen3-ASR-1.7B | ~3.4 GB | GPU/vLLM | Apache-2.0 ✅ | ✅ R4-c 已接（准确率更高） |
| `qwen3_align/` | 词级对齐 | Qwen3-ForcedAligner-0.6B | ~1.7 GB | GPU | Apache-2.0 ✅ | ✅ R4-c 已接（多语言对齐） |
| `nemo_sortformer/` | 说话人分离 | NVIDIA Sortformer (diar_sortformer_4spk-v1) | ~0.9 GB | GPU | **Apache-2.0** ✅ | ✅ R4-c 已接（商用首选） |
| `whisperx_base/` | ASR 备用 | WhisperX + Whisper large-v3 | ~3 GB | GPU | MIT/模型条款需核对 | ✅ 自动路由备用 |
| `demucs_v4/` | 音乐分离预处理 | Demucs v4 `htdemucs` | ~1.5 GB | GPU 推荐 | MIT | ✅ 音乐覆盖严重时按需调用 |
| `qwen3_tts_customvoice/` | 字幕转语音 | Qwen3-TTS 12Hz 1.7B CustomVoice | ~3.4 GB | GPU 推荐 | Apache-2.0（核对权重条款） | ✅ 字幕轨道右键生成配音 |
| `qwen3_tts_tokenizer_12hz/` | TTS 编解码器 | Qwen3-TTS Tokenizer 12Hz | ~400 MB | CPU 可加载 | Apache-2.0（核对权重条款） | ✅ CustomVoice 必需 |
| `qwen3_tts_base/` | 授权音色克隆（可选） | Qwen3-TTS 12Hz 1.7B Base | ~3.4 GB | GPU 推荐 | Apache-2.0（核对权重条款） | ⏳ 后续接入 |

> 默认链路是 Qwen3-ASR + Qwen3-ForcedAligner + Sortformer。WhisperX 不参与默认推理，
> 仅作为语言、对齐或识别质量不足时的备用 provider；Demucs 只在音乐覆盖严重时运行。

### 4. Qwen3-TTS（字幕转语音）

```bash
pip install -U qwen-tts soundfile

# ModelScope（国内推荐）
modelscope download --model Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice \
  --local_dir models/ai_editing/qwen3_tts_customvoice
modelscope download --model Qwen/Qwen3-TTS-Tokenizer-12Hz \
  --local_dir models/ai_editing/qwen3_tts_tokenizer_12hz

# 启用真实推理
export TOPOS_TTS_REAL=1
```

在字幕轨道头部右键选择“字幕轨道转语音”后，系统会逐条合成字幕，按字幕时间码
合成为一个 WAV，自动导入媒体池并放到音频轨道。Qwen3-TTS 的 CustomVoice 支持
多语言、说话人和语气指令；未来可用 `qwen3_tts_base/` 加入用户授权的音色克隆。
如不需要 AI 配音，不安装该模型也不会影响普通剪辑或 ASR。

---

## 如何启用各模型

### 1. RapidOCR（OCR，最简单，推荐先试）

```bash
# 安装依赖（CPU 可跑，无需 GPU）
pip install rapidocr onnxruntime

# 模型文件放入本子目录（rapidocr 首次 import 时会自动下载到其默认缓存；
# 若想强制用本地模型，把 det/rec 权重放到 models/ai_editing/rapidocr_ocr/ 下）
cd models/ai_editing/rapidocr_ocr/
# 按 RapidOCR 文档放置 det.onnx / rec.onnx 等文件

# 启用真实推理
export TOPOS_OCR_REAL=1

# 在媒体池对素材执行 VISUAL_SEMANTIC_LOCAL 配方分析即可
```

### 2. WhisperX（可选备用 ASR）

```bash
# 依赖（需要 GPU；只在 Qwen 路由质量不足时使用）
pip install torch torchaudio whisperx

# 启用真实 WhisperX 备用 provider
export TOPOS_WHISPERX_REAL=1
```

### 3. Demucs v4（音乐覆盖严重时按需启用）

```bash
pip install demucs

# 一般不需要全局启用。右键“语音转文字”会在 Qwen 结果质量不足且
# Demucs 可用时自动尝试 vocals stem，再重新调用 Qwen3-ASR。
python -m demucs --two-stems=vocals -n htdemucs input.wav
```

---

## 路径与校验机制

- **默认根目录**：`<项目根>/models/ai_editing/`（由 `model_pack_manager.default_cache_dir()` 推导）
- **pack 子目录**：每个 `pack_id` 一个子目录；目录存在**不**代表已安装或可用。
- **发布完整性**：仓库只保存模型的版本/许可/来源元数据，不伪造权重 hash。内置 pack
  的 SHA-256 状态为 `release-manifest-required`；正式应用启动时必须加载受控发布清单
  （每个 pack 为 64 位十六进制 SHA-256）。正式包将它放在
  `models/ai_editing/release_manifest.json`，应用启动时加载；缺失或无效时安装与推理
  都会 fail closed。确定性 demo host 可
  显式使用目录存在模式，但能力状态会显示为 `demo`，不会宣称真实模型可用。

如需把模型放到其他位置（如外置硬盘），构造 `ModelPackManager(cache_dir=...)` 传入自定义路径即可。

---

## 2026 推荐替代模型（截至 2026-08 调研）

下面列出每个能力域的默认模型和可选备用模型。Qwen3-ASR 是默认 provider，
WhisperX/Demucs 由自动路由按需调用。

媒体池 recipe 仍只请求 Qwen3 默认能力；右键字幕动作使用 `transcript_auto` 路由，
并把最终选中的 provider、预处理器和选择原因写入分析 provenance。

### 🎙️ ASR 语音转文字

| pack_id | 模型 | WER | 语言 | 许可证 | 部署 | 推荐度 |
|---|---|---|---|---|---|---|
| `qwen3_asr` | **Qwen3-ASR-0.6B** | ~6.5% | **52 语言** | **Apache-2.0** ✅ | MLX(原生 Mac)/vLLM/GPU | ⭐⭐⭐⭐⭐ **中文最优** |
| `qwen3_asr_large` | **Qwen3-ASR-1.7B** | ~5.9% | 52 语言 | Apache-2.0 ✅ | vLLM/GPU | ⭐⭐⭐⭐ 准确率更高 |
| `qwen3_align` | **Qwen3-ForcedAligner-0.6B** | — | 多语言 | Apache-2.0 ✅ | PyTorch | ⭐⭐⭐⭐ 替代 wav2vec2 |
| `whisperx_base`（备用） | WhisperX large-v3 | — | 多语言 | MIT/模型条款需核对 | CTranslate2/GPU | ⭐⭐⭐ 备用对齐/识别 |

**为什么推荐 Qwen3-ASR**：
- 阿里出品，对**中文**优化远超 Whisper
- **Apache-2.0 商用无忧**（WhisperX 的 CTranslate2 转码链路也 MIT，但 Qwen 中文更准）
- 有 **[mlx-qwen3-asr](https://github.com/moona3k/mlx-qwen3-asr/) 原生 Apple Silicon 实现**——Mac 上不用 CUDA 也能跑得快（WhisperX 在 Mac 上很慢）
- 同团队开源了 **Qwen3-ForcedAligner**（词级对齐），替代 wav2vec2 且多语言

**何时仍用 WhisperX**：纯英文场景 + 已有 CUDA GPU + 需要 CTranslate2 的极致推理速度。

### 🎵 背景音乐分离

| pack_id | 模型 | 作用 | 使用策略 |
|---|---|---|---|
| `demucs_v4` | Demucs v4 `htdemucs` | 生成 vocals stem，降低音乐覆盖 | 仅在原始 Qwen 结果质量不足时尝试 |

Demucs 不是 ASR，也不是所有音频的必经步骤。分离后的 stem 可能包含歌曲人声，
因此系统会比较原始音频和分离音频的转录质量，保留更好的结果。

下载源：
- [Qwen3-ASR-0.6B-hf](https://huggingface.co/Qwen/Qwen3-ASR-0.6B-hf)
- [Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)
- [mlx-qwen3-asr（Mac 原生）](https://github.com/moona3k/mlx-qwen3-asr/)
- [2026 ASR 横评（MarkTechPost）](https://www.marktechpost.com/2026/07/23/best-open-speech-recognition-asr-models-in-2026-wer-languages-latency-and-license-compared/)

> **国内下载推荐 ModelScope（魔搭）**：实测 ~1.7–2.8 MB/s，比 HF 官方快约 8 倍。对应 ID：
> `Qwen/Qwen3-ASR-0.6B-hf`、`Qwen/Qwen3-ASR-1.7B`、`Qwen/Qwen3-ForcedAligner-0.6B`。
> 下载方式见本文件末尾「附录：ModelScope 下载命令」。

### 🗣️ 说话人分离

| pack_id | 模型 | DER | 许可证 | 部署 | 推荐度 |
|---|---|---|---|---|---|
| `nemo_sortformer` | **NVIDIA Sortformer** | ~10-15% | **Apache-2.0** ✅ | NeMo/GPU | ⭐⭐⭐⭐⭐ **商用首选** |
| `pyannote_diarize_3`（旧） | pyannote 3.1 | ~10-19% | **CC-BY-NC-4.0** ⚠️ | PyTorch/GPU | ⭐⭐⭐ 非商用 |

**为什么推荐 Sortformer**：
- **Apache-2.0 解决了 pyannote 的 CC-BY-NC-4.0 商用障碍**（你的产品是商业编辑器，这点关键）
- DER（Diarization Error Rate）与 pyannote 相当甚至更好
- NVIDIA NeMo 框架 GPU 优化充分
- 无需 HuggingFace token + 接受条款的繁琐流程

下载源：
- [NVIDIA NeMo / Sortformer](https://github.com/NVIDIA/NeMo)
- [Sortformer Diarizer 4spk v1（ModelScope，国内推荐）](https://modelscope.cn/models/nv-community/diar_sortformer_4spk-v1)
- [Sortformer vs pyannote 对比](https://vast.ai/article/whisper-pyannote-sortformer-diarization-vast)

### 🔤 OCR 文字识别

| pack_id | 模型 | 准确率 | ONNX | 许可证 | 推荐度 |
|---|---|---|---|---|---|
| `rapidocr_ocr`（当前） | **RapidOCR** | ~97% | ✅ 原生 | **Apache-2.0** ✅ | ⭐⭐⭐⭐⭐ **保持现状** |
| （未注册） | Surya OCR 2 | ~97.5% | ⚠️ 可导出 | **Open Rail-M（商用受限）** ⚠️ | ⭐⭐⭐ 准但许可麻烦 |
| （未注册） | olmOCR-2-7B | ~94% | ❌ | 开源 | ⭐⭐ 太重（7B） |

**为什么 RapidOCR 保持最优**：
- **Apache-2.0 商用安全**（Surya 的 Open Rail-M 有收入门槛限制）
- 原生 ONNX，与项目的降噪/抠像走**同一个 `onnxruntime` 后端**，零额外运行时
- CPU 可跑、~15MB 体积小
- 准确率与 Surya 差距很小（97% vs 97.5%），不值得为 0.5% 牺牲许可安全

### 模型格式对照（重要）

不同模型用**不同运行时**，不是统一 ONNX：

| 模型 | 格式 | 运行时 | 与项目降噪/抠像同源？ |
|---|---|---|---|
| RapidOCR | ONNX (`.onnx`) | **onnxruntime** | ✅ 是 |
| Qwen3-ASR | PyTorch/Safetensors | PyTorch/MLX | ❌ 否（但 MLX 在 Mac 上原生效） |
| Sortformer | PyTorch/NeMo | NeMo + PyTorch | ❌ 否 |
| WhisperX | CTranslate2 | CTranslate2 | ❌ 否 |
| pyannote | PyTorch (`.ckpt`) | PyTorch | ❌ 否 |

目录结构统一，但子目录内文件格式不同。下载时按各模型的官方说明放置对应格式文件。

### 推荐组合（2026）

- **Mac 用户 / 中文素材**：`rapidocr_ocr` + `qwen3_asr`（MLX）+ `nemo_sortformer`
- **Linux + NVIDIA GPU**：`rapidocr_ocr` + `qwen3_asr_large` + `nemo_sortformer`
- **混合音乐/对白**：`qwen3_asr` + `qwen3_align` + `demucs_v4`（按需）
- **纯英文 + 已有 WhisperX 环境**：`rapidocr_ocr` + `whisperx_base`
- **最低门槛试水**：只装 `rapidocr_ocr`（CPU、15MB、Apache-2.0、和降噪同栈）

---

## 附录：ModelScope 下载命令（国内推荐）

国内访问 HuggingFace 慢，推荐用 ModelScope（魔搭）。需先在项目 venv 装 `modelscope`：

```bash
pip install modelscope
```

**重要**：本机 Clash 为 TUN 模式 + fake-ip，若 pip / modelscope 报 SSL 证书错误，
在命令前加 `no_proxy="*"` 让 Python 走 TUN 直连（不要用 HTTP 代理环境变量）。

下载各 pack 到对应子目录（从项目根运行）：

```bash
no_proxy="*" python -c "
from modelscope.hub.snapshot_download import snapshot_download
import os
base = 'models/ai_editing'
for pack, mid in [
    ('qwen3_asr',       'Qwen/Qwen3-ASR-0.6B-hf'),
    ('qwen3_asr_large', 'Qwen/Qwen3-ASR-1.7B'),
    ('qwen3_align',     'Qwen/Qwen3-ForcedAligner-0.6B'),
    ('nemo_sortformer', 'nv-community/diar_sortformer_4spk-v1'),
    ('qwen3_tts_customvoice', 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'),
    ('qwen3_tts_tokenizer_12hz', 'Qwen/Qwen3-TTS-Tokenizer-12Hz'),
]:
    os.makedirs(f'{base}/{pack}', exist_ok=True)
    snapshot_download(model_id=mid, local_dir=f'{base}/{pack}')
    print(f'✅ {pack}')
"
```

| pack_id | ModelScope ID | 大小 |
|---|---|---|
| `qwen3_asr` | `Qwen/Qwen3-ASR-0.6B-hf` | ~1.5 GB |
| `qwen3_asr_large` | `Qwen/Qwen3-ASR-1.7B` | ~4.4 GB |
| `qwen3_align` | `Qwen/Qwen3-ForcedAligner-0.6B` | ~1.7 GB |
| `nemo_sortformer` | `nv-community/diar_sortformer_4spk-v1` | ~0.9 GB |
| `qwen3_tts_customvoice` | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | ~3.4 GB |
| `qwen3_tts_tokenizer_12hz` | `Qwen/Qwen3-TTS-Tokenizer-12Hz` | ~0.4 GB |

> ⚠️ 注意 `Qwen3-ForcedAligner`（无 `-0.6B` 后缀）在 ModelScope 上为 404，
> 正确 ID 是 `Qwen/Qwen3-ForcedAligner-0.6B`。HF 上两者都存在，但 ModelScope 只收了带后缀版。

下载完成后用 `safetensors` 验证权重完整性：

```bash
python -c "
import glob, os, json
from safetensors import safe_open
for st in glob.glob('models/ai_editing/*/*.safetensors'):
    with safe_open(st, framework='pt') as f:
        ks = list(f.keys())
    print(f'✅ {st}: {len(ks)} tensors')
"
```

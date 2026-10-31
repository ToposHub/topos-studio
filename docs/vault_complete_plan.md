# Topos Vault 完整计划表

> AI 影视素材库管理系统——独立外挂应用，给视频/音乐/图片做 AI 打标，供 Topos Color 主项目检索调用。
>
> **最后更新**：2026-08-07
> **当前状态**：阶段 A（地基）+ B（打标流水线 + 语义检索）+ C（视频完整链路 + 主项目集成）+ D（高级检索/日志/增量/性能）已完成
> **测试**：280 个通过 + 1 跳过（faiss 未装），10.3 秒
> **代码**：9909 行（50 个源文件）
>
> 本文档是 vault 的**唯一真相源**，包含：架构设计、扩展指南、完整优化计划、快速参考。

---

# 第一部分：架构设计

## 1.1 一句话定位

**指纹是主键（不是路径），模型层可插拔（不写死具体模型），三类素材统一架构。**

---

## 1.2 核心问题与答案

### Q1：数据放哪？

**打标数据**（vault.db / FAISS / 关键帧缓存）放**跨平台用户目录**（`platformdirs`）：
- macOS: `~/Library/Application Support/topos-vault/`
- Windows: `%LOCALAPPDATA%/topos-vault/`
- Linux: `~/.local/share/topos-vault/`
- 环境变量 `TOPOS_VAULT_DIR` 可覆盖

**模型权重**放项目内 `models/ai_editing/`，与主项目共享（复用 `ModelPackManager`）。

为什么分开：模型是可重建的大缓存（丢了重下），打标数据是几小时 AI 跑出来的成果（丢了重来）。两者可靠性要求不同，存储位置也不同。

### Q2：素材移动后怎么重连？

**三级匹配机制**（详见 `relink.py`）：

| Level | 速度 | 匹配方式 | 覆盖场景 |
|-------|------|---------|---------|
| 0 | 0ms | `path_current` 直接存在 | 素材没动（90% 正常情况）|
| 1 | <1ms | 原目录 + 同名 + 同 size | 整个文件夹一起移动 |
| 2 | ~500ms | **指纹精确匹配** | 改名/换盘/改后缀（核心级）|
| 3 | ~1s | ffprobe duration+WxH 兜底 | 重编码（字节和指纹都变了）|

**指纹按素材类型分算法**（关键设计）：

| 类型 | 算法 | 为什么 |
|------|------|--------|
| video | xxhash 3点采样（首+中+尾1MB） | 快（500ms/8GB），视频帧非随机不撞 |
| audio | Chromaprint 声纹 | 重编码/变码率仍能认出同一首 |
| image | pHash 感知哈希 | 缩放/压缩/裁剪仍能认出同一张 |

依赖缺失时降级到 `size:` 兜底（精度低但不崩溃，装好依赖后自动升级）。

### Q3：三类素材怎么统一？

**同库不同表，共享身份层与画面流水线**：

```
media_source (fingerprint PK)        ← 三类统一身份层
  ├── clip (video)                   ← 镜头片段
  │     ├── transcript_line
  │     └── face_track
  ├── audio_meta (audio)             ← 整曲元数据
  ├── image_meta (image)             ← 整图元数据
  └── vector_index                   ← FAISS 统一映射
```

**关键共享**：视频的画面打标和图片共用同一套 `visual_caption` + `visual_embed` provider——因为"理解画面"这件事与素材是视频还是图片无关。

---

## 1.3 架构总览

```
┌──────────────────────────────────────────────────────────┐
│ 进程 A：Topos Color 主项目（几乎不改）                     │
│  ├─ file_explorer 插件 ai_library_browser（检索入口）      │
│  └─ src/shared/vault/client.py（只读查询，预留）           │
└────────────────────────────┬─────────────────────────────┘
                             │ 只读 HTTP
                             ▼
          ┌──────────────────────────────────┐
          │  数据层（跨平台用户目录）          │
          │  ├─ vault.db (SQLite, WAL)        │
          │  ├─ faiss/*.faiss                 │
          │  └─ cache/<fp>/ (关键帧)          │
          └──────▲───────────────────┬───────┘
                 │                   │ 写入
┌────────────────┴────┐    ┌────────┴──────────────────────┐
│ 进程 C：查询服务(轻)  │    │ 进程 B：打标流水线(重)          │
│ topos-vault serve   │    │ topos-vault index             │
│ Flask 只读+懒加载    │    │ 可插拔 provider 流水线         │
└─────────────────────┘    └────────┬──────────────────────┘
                                    │
                   ┌────────────────┴────────────────┐
                   │  模型层（项目内，与主项目共享）   │
                   │  models/ai_editing/<pack_id>/    │
                   └─────────────────────────────────┘
```

**三进程靠共享文件系统通信**：打标进程写 `vault.db` + FAISS，查询进程和主项目只读。没有跨进程调用耦合，打标崩了不影响主项目。

---

## 1.4 模块清单

```
apps/topos_vault/
├── paths.py              跨平台路径（platformdirs + env 覆盖）
├── cli.py                CLI 入口（init/status/index/relink/serve/providers）
├── relink.py             三级重连匹配
├── probe.py              媒体特征探测（PyAV/ffprobe，Level 3 用）
├── model_specs.py        vault 模型包 spec（与主项目 ModelPackManager 集成）
├── db/
│   ├── schema.py         SQLite 7 张表 + 版本化迁移
│   ├── repository.py     读写仓储层（三类素材 CRUD）
│   └── faiss_store.py    向量存储（faiss 优先，numpy 兜底）
├── fingerprint/
│   ├── base.py           Protocol + 前缀常量 + 降级路径
│   ├── video.py          xxhash 3点采样
│   ├── audio.py          Chromaprint（pyacoustid/fpcalc）
│   ├── image.py          pHash（imagehash）
│   └── dispatch.py       按 media_type 分派
├── providers/
│   ├── base.py           6 能力枚举 + Protocol + 注册表 + 切换机制
│   ├── contracts.py      各能力的输入/输出 dataclass
│   ├── model_loader.py   懒加载基类 + device 解析（CUDA→MPS→CPU）
│   ├── stubs.py          6 个 stub 兜底实现
│   ├── real_transcript_embed.py   Qwen3-Embedding / BGE-M3
│   ├── real_visual_embed.py       SigLIP 2 / Chinese-CLIP
│   ├── real_visual_caption.py     Qwen3-VL-8B
│   ├── real_audio_understand.py   Qwen2.5-Omni-7B
│   ├── real_face_recognize.py     InsightFace
│   └── real_registry.py           统一注册入口
├── pipeline/
│   ├── steps.py          PipelineStep 基类 + 执行结果
│   ├── ingest_step.py    第一步：指纹入库 + 探测元数据
│   ├── scene_cut_step.py 视频镜头切割（PyAV 帧差）
│   ├── asr_step.py       ASR 转写（主项目 ASR 集成 + vault 降级）
│   ├── visual_step.py    画面向量+caption（图片整图 + 视频镜头级）
│   ├── transcript_step.py 台词向量
│   ├── audio_features_step.py 音频数值特征（librosa BPM/调性/能量/人声）
│   ├── keyframe_extract.py    视频关键帧抽取（PyAV）
│   ├── face_step.py      人脸识别（clip 关键帧 → face_track + face.faiss）
│   ├── character_cluster.py   角色聚类（DBSCAN，sklearn 优先 numpy 降级）
│   ├── orchestrator.py   编排器（批量/断点续跑）
│   └── phased_orchestrator.py 分时复用调度器（24GB 显存友好，大模型分时加载）
└── server/
    ├── query_service.py  搜索逻辑（SQL 粗筛 → 向量精排 + 高级检索）
    └── app.py            Flask 应用（/api/search /api/stats /api/advanced 等）

顶层模块：
├── logging_config.py     结构化日志（JSON/文本 + 文件轮转）
├── task_history.py       打标任务历史（record/list_runs/aggregate_stats）
└── watcher.py            增量索引（watchdog 文件监听）

external/file_explorer/plugins/ai_library_browser/
├── __init__.py           Blueprint + setup（注入搜索框到 sidebar_bottom）
└── static_plugin/.../main.js   前端：输入→fetch→塞 STATE.allFiles→重渲染

src/shared/vault/         主项目侧集成层（vault ↔ 主项目唯一耦合点）
├── __init__.py           包导出
├── client.py             VaultClient：只读 HTTP 客户端（search/stats/health）
└── bridge.py             TAEP 桥接：vault 数据 → SemanticSegment

tests/vault/              16 个测试文件，229 个测试
scripts/vault_smoke_test.py  真实模型联调验证脚本（用户下模型后跑）
```

---

## 1.5 关键设计决策（及理由）

### 1. 为什么 vault 独立，不塞进主项目？
用户明确要求：大批量电影打标不能进主 app（会卡死 media pool），闲置时单独跑。三进程分离让打标崩了不影响剪辑，主项目读到的永远是已完成快照。

### 2. 为什么指纹按类型分算法，不统一用一种？
- 视频换码率 → 字节全变，但 xxhash 采样哈希对视频帧结构有效
- 音频换码率 → 字节全变，Chromaprint 声纹基于频谱不变
- 图片缩放 → 字节全变，pHash 基于 DCT 低频不变
用错算法 = 素材一动就认不出 = 几小时打标白费。

### 3. 为什么 provider 用 Protocol 不用继承？
对齐主项目 `AnalysisProvider` 模式（duck typing）。新增模型只需实现 Protocol，注册到 registry，改配置即可切换，流水线代码不动。这是"多链路可切换"的核心。

### 4. 为什么模型放 `models/ai_editing/` 不放 vault 自己的目录？
与主项目共享——主项目已下载的 Qwen3-ASR，vault 直接复用不重复下载。复用 `ModelPackManager` 的 sha256 校验、许可证明示、不静默降级。

### 5. 为什么 stub 永远 `is_available()=True`？
保证总有一个 provider 可用（CI/测试/无 GPU 机器）。stub 输出确定性（同输入同输出），对齐主项目 `_stub_transcript` 模式。真实 provider 注册后覆盖默认。

---

## 1.6 6 个 AI 能力 + 多链路

| 能力 | 默认 provider | 备选 | stub |
|------|--------------|------|------|
| transcript_embed | Qwen3-Embedding-0.6B | BGE-M3 | ✅ |
| visual_embed | SigLIP 2 base | Chinese-CLIP | ✅ |
| visual_caption | Qwen3-VL-8B AWQ 4bit | Qwen3-VL-8B FP16 | ✅ |
| audio_understand | Qwen2.5-Omni-7B AWQ | FP16 | ✅ |
| face_recognize | InsightFace buffalo_l | buffalo_s | ✅ |
| asr | 复用主项目 Qwen3-ASR | WhisperX | ✅ |

**切换方式**（三档）：
1. 环境变量 `TOPOS_VAULT_<CAP>=<provider_id>`
2. 配置文件 `[providers] <cap> = <provider_id>`
3. 代码 `get_provider(Capability.XXX)`

**"换更小模型" = 加一个新 ModelPackSpec（同 capability）+ 改配置项**，流水线代码不动。

---

## 1.7 当前 pipeline 链路

```
video:  ingest → scene_cut → asr → transcript_embed
        → visual_embed_video → video_caption → face → audio_features
audio:  ingest → audio_features
image:  ingest → visual_embed → image_caption
```

角色聚类（cluster_characters）不在 pipeline 内自动跑——整库级后处理（跨素材
聚合同一角色），index 完成后单独调 `topos-vault cluster`。

---

## 1.8 测试覆盖

`tests/vault/` 共 **135 个测试**，2.65 秒跑完：

| 模块 | 测试数 | 覆盖点 |
|------|-------|--------|
| paths | 10 | env 覆盖/自动创建/分桶/模型目录 |
| db | 17 | 7表CRUD/级联删除/幂等/WAL/向量映射 |
| fingerprint | 9 | 三类算法/降级/确定性/分派 |
| relink | 10 | 三级匹配/二次丢失检测/幂等 |
| providers | 13 | 6能力/stub确定性/切换/注册 |
| real_providers | 10 | 真实provider注册/降级链/懒加载 |
| query_service | 10 | 搜索/类型过滤/相关度/file_explorer格式 |
| vector_search | 6 | 语义检索（搜微笑找到没写微笑的素材）|
| vector_store | 22 | 向量存储/余弦相似度/持久化/双后端 |
| model_specs | 9 | 5能力spec/安装检查/商用许可 |
| pipeline | 10 | 编排/断点续跑/增量/进度回调 |
| pipeline_steps | 11 | 各 step 行为/幂等/降级 |

---

## 1.9 与主项目的集成边界

| 共享 | 不共享 |
|------|--------|
| `models/ai_editing/` 模型权重 | vault.db（独立，不碰 topos.db）|
| `ModelPackManager` 校验/许可证机制 | media pool / 时间线 / 渲染管线 |
| file_explorer dock 面板（搜索 UI）| TAEP 协议（预留桥接，本阶段不做）|

**数据软关联**：`media_source.topos_asset_id` 可关联主项目 asset_id（若有），不强求一致。

---

# 第二部分：开发者扩展指南

所有扩展点都遵循同一设计哲学：**Protocol + 注册 + 配置切换**。新增能力不改流水线核心代码，只加实现 + 注册。

## 2.1 扩展点一：加一个新的 AI 能力

场景：比如想加"镜头质量评估"能力（评估画面是否模糊/过曝）。

**1. 在 `providers/base.py` 的 `Capability` 枚举加一项**
```python
class Capability(str, Enum):
    # ... 现有 6 个 ...
    QUALITY_ASSESS = "quality_assess"   # ← 新增
```

**2. 在 `providers/contracts.py` 定义 Protocol + 结果 dataclass**
```python
@dataclass
class QualityResult:
    sharpness: float        # 0-1
    exposure: float         # 0-1（0.5 = 正常）
    is_blurry: bool
    model: str = ""

@runtime_checkable
class QualityAssessProvider(Protocol):
    provider_id: str
    capability: Capability  # = Capability.QUALITY_ASSESS
    def assess(self, image_path: str) -> QualityResult: ...
    def is_available(self) -> bool: ...
```

**3. 实现 stub 兜底**（在 `providers/stubs.py`）

**4. 加环境变量映射**（`providers/base.py` 的 `_ENV_VARS`）
```python
_ENV_VARS[Capability.QUALITY_ASSESS] = "TOPOS_VAULT_QUALITY_ASSESS"
```

**5. 写真实实现 + 注册** + **6. 在 `model_specs.py` 加模型包描述**

完成。`get_provider(Capability.QUALITY_ASSESS)` 会按配置返回 stub 或真实实现。

## 2.2 扩展点二：给现有能力加一个新模型变体

场景：Qwen3-VL-8B 太大，想加个 Qwen3-VL-2B 的小版本。

**只需两步，流水线代码不动**：
1. 在 `model_specs.py` 加 spec（同 capability，`is_default=False`）
2. 实现 provider + 注册

切换：用户设 `TOPOS_VAULT_VISUAL_CAPTION=qwen3_vl_2b` 即用小模型。

## 2.3 扩展点三：加一个新的指纹算法

场景：给 PDF 文档加指纹。

1. 在 `fingerprint/` 加新模块（如 `document.py`）
2. 在 `fingerprint/dispatch.py` 加分派
3. 在 `pipeline/orchestrator.py` 的 `_EXT_MAP` 加扩展名

## 2.4 扩展点四：加一个新的打标步骤

场景：音频事件检测（狗叫/枪声/雨声）。

1. 实现 `PipelineStep` 子类（在 `pipeline/` 下）
2. 注册到编排器：`orch.add_step("video", AudioEventStep(repo))`
3. `is_done()` 支持断点续跑

## 2.5 开发约定

- **强类型**：所有函数有类型注解，返回值用 dataclass
- **frozen dataclass**：领域对象用 `@dataclass(frozen=True)`
- **中文注释**：docstring 和关键逻辑用中文，变量名用英文
- **不静默降级**：模型未装/依赖缺失 → 明确报错或走 stub（带标记）
- **测试**：每个新模块必须有 `tests/vault/test_<module>.py`，用 `vault_dir` fixture 隔离

---

# 第三部分：完整优化计划

## 3.1 当前已完成的能力（基线）

| 能力 | 状态 |
|------|------|
| 跨平台路径（platformdirs） | ✅ |
| SQLite 数据库（7表+WAL+迁移） | ✅ |
| 三类指纹（video/audio/image + 降级） | ✅ |
| 三级 relink（Level 0/1/2/3） | ✅ |
| 6 能力 provider（Protocol + stub + 真实 + 降级链） | ✅ |
| 真实 provider 代码层联调修复（SigLIP2 维度/Omni API/本地路径） | ✅ |
| pipeline 编排（完整链 + 断点续跑） | ✅ |
| 视频镜头级画面打标（关键帧 + clip 向量/caption） | ✅ |
| 人脸识别 + 角色聚类（DBSCAN） | ✅ |
| 音频数值特征（librosa BPM/调性/能量/人声） | ✅ |
| 主项目 ASR 集成（QwenASRProvider.analyze 接线） | ✅ |
| 分时复用调度器（24GB 显存友好，--phased） | ✅ |
| 向量存储（faiss/numpy 双后端 + 余弦相似度） | ✅ |
| 语义检索（SQL 粗筛 + 向量精排） | ✅ |
| 查询服务（Flask） | ✅ |
| file_explorer 插件（搜索 UI） | ✅ |
| CLI（init/status/index/relink/cluster/serve/providers） | ✅ |
| 模型包管理（model_specs + 主项目 BUILTIN_SPECS 合并） | ✅ |
| TAEP 桥接（vault → SemanticSegment） | ✅ |
| 主项目只读客户端（VaultClient HTTP） | ✅ |
| 高级检索（以图搜图 + BPM 匹配 + 组合查询） | ✅ |
| 结构化日志（JSON + 文件轮转） | ✅ |
| 任务历史（index_history + 聚合统计） | ✅ |
| 增量索引（watchdog 文件监听，自动打标/重连/标丢） | ✅ |
| 向量检索 IVF 索引（大规模加速） | ✅ |

---

## 3.2 P0 — 核心缺口（影响真实可用性）

### P0-1 真实模型联调验证 ✅ 已完成（代码层）
**问题**：所有真实 provider 写好了但**从未用真实模型跑过**（当前环境 torch/transformers 装不上）。
**风险**：真实推理路径可能有 bug（模型加载参数、tensor 维度、prompt 格式）。

- [x] 装 torch + transformers + sentence-transformers（已完成）
- [x] 修复联调必踩的代码层 bug（已完成）：
  - SigLIP2 维度修正 1152→768（base-patch16-224 的真实 hidden_size）
  - Qwen2.5-Omni processor 参数 `audios=`→`audio=`，音频传 numpy + sampling_rate
  - Qwen3-VL/Omni 消息 content 用路径字符串（跨版本兼容）
  - InsightFace 用本地模型路径 + GPU/CPU provider 自适应
  - 所有真实 provider 加 `_resolve_local_or_hf` + `is_available` 查模型文件
- [x] 写联调验证脚本 `scripts/vault_smoke_test.py`（用户下模型后跑）
- [x] 18 个代码契约回归测试 `tests/vault/test_real_provider_contracts.py`
- [ ] **用户下载模型后跑 `python3 scripts/vault_smoke_test.py` 做端到端验证**（用户侧）

**工作量**：M | **完成情况**：代码层全部修复 + 验证脚本/测试就绪，待用户下模型

### P0-2 音频数值特征（librosa）✅ 已完成
**问题**：`audio_understand` 只做语义理解，BPM/调性等数值特征没实现。大模型做不了精确 BPM。

- [x] 新增 `pipeline/audio_features_step.py`：librosa 算 BPM/调性/能量/人声判断
- [x] 写入 `audio_meta.bpm` / `key` / `energy` / `vocal_type`
- [x] 注册到 audio/video pipeline
- [x] 测试：合成 120BPM 节拍验证检测 60-200 范围（librosa 实测 117 BPM）

**工作量**：S | **完成情况**：9 个测试全过

### P0-3 主项目 ASR 集成接线 ✅ 已完成
**问题**：`AsrStep._try_main_project_asr()` 是空壳（只做 import 探测）。

- [x] 研究主项目 `transcript_provider.py` 的完整初始化流程（QwenASRProvider.analyze）
- [x] 实现集成：vault import src/，用 QwenASRProvider.analyze，帧坐标→秒转换
- [x] 环境变量 `TOPOS_VAULT_ASR_BACKEND=main_project|vault|auto` 切换
- [x] 测试：mock 主项目模块验证帧→秒换算、import 失败降级、后端切换

**工作量**：M | **完成情况**：7 个测试全过

---

## 3.3 P1 — 重要增强

### P1-1 视频镜头级画面打标 ✅ 已完成
**问题**：当前 `VisualEmbedStep` 只支持 image，视频的镜头画面没打标。

- [x] 新建 `pipeline/keyframe_extract.py`：PyAV 关键帧抽取（避开首尾边界 + 幂等）
- [x] 扩展 `VisualEmbedVideoStep`：clip 关键帧 → visual.faiss（id=clip_id，ref_table=clip）
- [x] 扩展 `VideoCaptionStep`：clip 关键帧 → caption/shot_type/keyframes 写回 clip
- [x] 注册到 video pipeline
- [x] repository 加 `update_clip_keyframes` + `get_clip`
- [x] 测试：11 个（mock 关键帧抽取验证向量写入/caption 写回/幂等）

**工作量**：M | **完成情况**：全过

### P1-2 人脸步骤 + 角色聚类 ✅ 已完成
**问题**：`FaceRecognizeProvider` 写好了但没接入 pipeline。角色聚类完全没做。

- [x] 新增 `pipeline/face_step.py`：clip 关键帧 → face_track + face.faiss
- [x] 角色聚类 `pipeline/character_cluster.py`：DBSCAN（sklearn 优先，numpy 降级）
- [x] 聚类结果写入 `face_track.cluster_id`
- [x] `assign_character_name`：给 cluster 指派角色名（人工标注 API）
- [x] CLI 新增 `topos-vault cluster [--list]` 子命令
- [x] repository 加 `list_face_tracks` / `update_face_cluster` / `list_character_clusters`
- [x] 测试：11 个（含 DBSCAN 聚类分组 + 纯 numpy 降级实现）

**工作量**：L | **完成情况**：全过（GUI 人工标注界面留待 P2-1）

### P1-3 查询服务前端实战验证 ⏳ 待用户侧验证
**问题**：file_explorer 插件从未在真实主项目里跑过。

- [ ] 启动主项目 + `topos-vault serve`
- [ ] 验证搜索框出现、输入、结果渲染、缩略图、类型过滤
- [ ] 修复 CSS 适配、跨域、路径问题
- [ ] 验证双击打开预览联动

**工作量**：S | **说明**：需用户在真实环境验证（涉及主项目启动 + 浏览器渲染）

### P1-4 ModelPackManager 正式注册 ✅ 已完成
**问题**：`to_main_project_specs()` 写好了但主项目没调用。

- [x] 在主项目 `ai_editing_mixin._build_model_pack_manager` 合并 vault specs
- [x] 去重（pack_id 重叠时主项目优先）+ vault 失败降级
- [x] 验证：15 个 spec 合并正确（5 个 vault + 主项目原有）
- [x] 测试：6 个

**工作量**：S | **完成情况**：全过

---

## 3.4 P1 — 架构完善

### P1-5 分时复用调度器 ✅ 已完成
**问题**：24GB 显存跑不了 Qwen3-VL + Qwen2.5-Omni 同时驻留。

- [x] 实现 `pipeline/phased_orchestrator.py`：阶段化加载大模型
- [x] 每个 provider 加 `unload()` 释放显存（LazyModelLoader.unload 已有）
- [x] CLI 加 `topos-vault index --phased`
- [x] 测试：10 个（阶段顺序/结果一致/unload 机制/进度回调）

**工作量**：M | **完成情况**：全过。N 个文件时大模型只加载 1 次（非 N 次）

### P1-6 TAEP 桥接层 ✅ 已完成
**问题**：vault 检索结果未来要喂给 AI 剪辑的 Executor。

- [x] 新建 `src/shared/vault/bridge.py`
- [x] `clip_to_semantic_segment(clip) -> SemanticSegment`（秒→帧转换）
- [x] 新增 `SegmentKind.VISUAL` / `FACE` / `CHARACTER`（editorial_models.py）
- [x] `face_track_to_segments` / `transcript_line_to_segment` / `search_hit_to_segments`
- [x] 测试：13 个

**工作量**：M | **完成情况**：全过。单向 vault→SemanticSegment，主项目不依赖 apps.topos_vault

### P1-7 主项目只读客户端 ✅ 已完成
**问题**：`src/shared/vault/client.py` 提到了但没实现。

- [x] 新建 `src/shared/vault/client.py`：`VaultClient` 封装 HTTP
- [x] `search()` / `get_stats()` / `is_available()`
- [x] vault 未启动时降级（返回空/False/None，1.5s 超时不卡主项目）
- [x] 测试：10 个（含 Flask test client 端到端）

**工作量**：S | **完成情况**：全过

---

## 3.5 P2 — 用户体验

### P2-1 vault 打标应用 GUI（PyQt）⏳ 未做（最大工程量，留待后续）
- [ ] `apps/topos_vault/ui/main_window.py`：主窗口
- [ ] 导入向导：选目录 → 预览 → 开始打标
- [ ] 进度面板：实时显示当前素材/步骤/进度条
- [ ] 结果校验界面：浏览打标结果，编辑 caption/角色名
- [ ] 库统计仪表盘
- [ ] relink 界面
- [ ] 模型管理

**工作量**：L | **说明**：PyQt GUI 是独立大工程，CLI 已覆盖全部功能，留待有 GUI 需求时做

### P2-2 增量索引（文件监听）✅ 已完成
- [x] watchdog 监听素材目录变化（`apps/topos_vault/watcher.py`）
- [x] 新文件 → 自动 ingest + 打标（增量，已打标跳过）
- [x] 文件移动/重命名 → 自动 relink
- [x] 文件删除 → 标记 missing
- [x] 去抖（500ms 窗口）+ 临时文件跳过
- [x] CLI 新增 `topos-vault watch [--once]`
- [x] 测试：13 个

**工作量**：M | **完成情况**：全过

### P2-3 高级检索 ✅ 已完成
- [x] 组合查询：`角色=张三 AND 情绪=愤怒 AND 时长>2s`（`advanced_search`）
- [x] 时间范围过滤 / 相似度阈值调节
- [x] 以图搜图（上传图 → 找相似画面，`search_similar_image`）
- [x] 音乐节拍匹配（给 BPM → 找节奏相近的，`search_by_bpm`）
- [x] 结果排序选项（按相关度 / BPM 差距）
- [x] API 端点 /api/similar_image / /api/by_bpm / /api/advanced
- [x] 测试：14 个

**工作量**：M | **完成情况**：全过

### P2-4 导出与集成 ⏳ 部分待做（需插件前端联调）
- [ ] 双击素材 → 主项目 media pool 导入
- [ ] 批量选择 → 导入多个
- [ ] 导出检索结果为 CSV/JSON
- [ ] "发送到时间线"

**工作量**：M | **说明**：需在 file_explorer 插件前端联调，属 P1-3 范畴

---

## 3.6 P2 — 工程质量

### P2-5 性能优化 ✅ IVF 索引已完成
- [x] 向量检索用 IVF 索引替代 Flat（支撑 10万+ 向量）
      `index_type=flat/ivf/auto`，≥5000 自动切 IVF
- [x] 性能基准脚本 `scripts/vault_perf_benchmark.py`
- [x] 批量 index 进度持久化（`index_history` 表 + TaskHistory，见 P2-7）
- [ ] SQL 查询加全文索引（FTS5）— LIKE 对当前规模够用
- [ ] 缩略图生成并行化
- [ ] 大文件指纹计算流式化

**工作量**：IVF + 基准 = S | **完成情况**：核心项（IVF）完成

### P2-6 国际化 ⏳ 未做（按需）
- [ ] CLI 输出支持英文
- [ ] GUI i18n
- [ ] caption 多语言支持

**工作量**：S | **说明**：当前全中文，国际化等有海外需求再做

### P2-7 日志与可观测性 ✅ 已完成
- [x] 结构化日志（JSON 格式，`logging_config.py`）
- [x] 打标任务历史记录（`index_history` 表 + `TaskHistory`，schema v2 迁移）
- [x] 错误聚合 / 性能指标导出（`aggregate_stats`：成功率/平均耗时）
- [x] CLI 新增 `topos-vault history [--limit N] [--stats]`
- [x] 测试：18 个

**工作量**：S | **完成情况**：全过

### P2-7 日志与可观测性
- [ ] 结构化日志（JSON）
- [ ] 打标任务历史记录
- [ ] 错误聚合 / 性能指标导出

**工作量**：S | **文件**：新增 `apps/topos_vault/logging_config.py`

---

## 3.7 建议实施顺序

```
第一批（验证可用）：✅ 已完成
  P0-1 真实模型联调（代码层修复 + 验证脚本，待用户下模型端到端验证）
  P0-2 librosa BPM ✅
  P0-3 ASR 集成接线 ✅

第二批（视频完整链路）：✅ 已完成
  P1-1 视频镜头级画面打标 ✅
  P1-2 人脸 + 角色聚类 ✅

第三批（架构完善）：✅ 已完成
  P1-5 分时复用调度器 ✅
  P1-6 TAEP 桥接 ✅
  P1-7 主项目客户端 ✅
  P1-4 ModelPackManager 注册 ✅

第四批（用户体验/工程质量）：✅ 核心已完成
  P2-3 高级检索（以图搜图 + BPM + 组合查询）✅
  P2-7 结构化日志 + 任务历史 ✅
  P2-2 增量索引（watchdog 监听）✅
  P2-5 性能优化（IVF 索引）✅
  P1-3 插件实战验证 ⏳ 待用户在真实环境验证
  P2-1 GUI 应用（PyQt）⏳ 未做（CLI 覆盖全功能）
  P2-4 导出集成 ⏳ 待插件前端联调
  P2-6 国际化 ⏳ 按需

剩余待做：P1-3 / P2-1 / P2-4 / P2-6（均需特定环境/前端/需求驱动，非核心）
```

---

## 3.8 已知的技术债

| 债务 | 位置 | 影响 | 优先级 |
|------|------|------|--------|
| scene_cut 用简化帧差，没用主项目完整 detector | `pipeline/scene_cut_step.py` | 切割精度略低 | P2 |
| VectorStore.remove 软删除，不回收空间 | `db/faiss_store.py` | 索引膨胀 | P2 |
| Chromaprint verify 用前缀比对，没做 Hamming 距离 | `fingerprint/audio.py` | 音乐重连可能漏判 | P2 |
| 向量库 model 字段没版本管理 | `db/faiss_store.py` | 换模型需全量重建 | P1 |
| ~~真实 provider 输出格式未验证~~ | ~~`providers/real_*.py`~~ | ~~联调可能有 bug~~ | ~~P0~~ ✅ 代码层已修复（待用户下模型端到端验证） |
| ~~audio_semantic dim 是估算的 1024~~ | ~~`db/faiss_store.py`~~ | ~~可能维度不匹配~~ | ~~P1~~ ✅ SigLIP2 维度已修正为 768 |

---

# 第四部分：快速参考

## 4.1 常用命令

```bash
# 初始化数据库
python3 -m apps.topos_vault.cli init

# 打标（扫描目录）
python3 -m apps.topos_vault.cli index /path/to/media

# 打标（分阶段模式，24GB 显存友好）
python3 -m apps.topos_vault.cli index /path/to/media --phased

# 查看库状态
python3 -m apps.topos_vault.cli status

# 重连移动的素材
python3 -m apps.topos_vault.cli relink /new/location

# 人脸角色聚类（index 完成后跑）
python3 -m apps.topos_vault.cli cluster
python3 -m apps.topos_vault.cli cluster --list  # 列出聚类结果

# 启动查询服务（供插件调用，端口 8288）
python3 -m apps.topos_vault.cli serve

# 查看 provider 链路
python3 -m apps.topos_vault.cli providers

# 真实模型联调验证（用户下模型后跑）
python3 scripts/vault_smoke_test.py
python3 scripts/vault_smoke_test.py transcript  # 只验证某能力

# 跑测试
python3 -m pytest tests/vault/ -q
```

## 4.2 环境变量

```
TOPOS_VAULT_DIR=/path/to/vault/data         # 数据目录（默认 platformdirs）
TOPOS_VAULT_MODELS_DIR=/path/to/models      # 模型目录（默认 models/ai_editing/）
TOPOS_VAULT_URL=http://127.0.0.1:8288       # 插件连查询服务的地址
TOPOS_VAULT_DEVICE=cuda:0|mps|cpu           # 推理设备（默认自动检测）
TOPOS_VAULT_VISUAL_CAPTION=qwen3_vl_8b_awq  # 指定某能力的 provider
TOPOS_VAULT_ASR_BACKEND=main_project|vault|auto  # ASR 后端（auto 默认）
TOPOS_VAULT_TRANSCRIPT_EMBED=...            # 指定 transcript_embed provider
TOPOS_VAULT_VISUAL_EMBED=...                # 指定 visual_embed provider
TOPOS_VAULT_AUDIO_UNDERSTAND=...            # 指定 audio_understand provider
TOPOS_VAULT_FACE_RECOGNIZE=...              # 指定 face_recognize provider
TOPOS_PROJECT_ROOT=/path/to/topos           # 项目根（定位共享模型）
```

## 4.3 关键路径

```
数据：    ~/.topos-vault/（或 platformdirs 跨平台路径）
模型：    <项目>/models/ai_editing/<pack_id>/
插件：    external/file_explorer/plugins/ai_library_browser/
测试：    tests/vault/
文档：    docs/vault_complete_plan.md（本文件）
```

## 4.4 数据库表结构速查

```
media_source     fingerprint(PK) / media_type / path_original / path_current / path_status
clip             clip_id(PK) / fingerprint(FK) / clip_index / start_sec / end_sec
transcript_line  line_id(PK) / clip_id(FK) / text / speaker / character
face_track       face_id(PK) / clip_id(FK) / character / cluster_id
audio_meta       fingerprint(PK,FK) / bpm / key / mood / instruments / description
image_meta       fingerprint(PK,FK) / tags / caption / dominant_color
vector_index     vector_id(PK) / index_name / ref_table / ref_id / model / dim
```

## 4.5 6 个能力 + 模型速查

| 能力 | 默认模型 | 维度 | 显存 |
|------|---------|------|------|
| transcript_embed | Qwen3-Embedding-0.6B | 1024 | ~2GB |
| visual_embed | SigLIP 2 base-patch16-224 | 768 | ~1GB |
| visual_caption | Qwen3-VL-8B-AWQ (4bit) | - | ~6GB |
| audio_understand | Qwen2.5-Omni-7B-AWQ (4bit) | - | ~8GB |
| face_recognize | InsightFace buffalo_l | 512 | ~1GB |
| asr | 复用主项目 Qwen3-ASR | - | 已有 |

---

## 4.6 版本历史

- 2026-04-22：初始版本，定义 Agent 使用规范和最佳实践（vault 地基阶段 A/B）
- 2026-08-07：阶段 C（视频完整链路 + 主项目集成）+ 阶段 D（高级检索/日志/增量/性能）完成
  - P0-1 真实 provider 代码层联调修复（SigLIP2 维度 1152→768、Omni processor API、
    本地模型路径解析）+ 验证脚本
  - P0-2 音频数值特征（librosa BPM/调性/能量/人声）
  - P0-3 主项目 ASR 集成（QwenASRProvider.analyze 接线）
  - P1-1 视频镜头级画面打标（关键帧抽取 + clip 向量/caption）
  - P1-2 人脸识别 + 角色聚类（DBSCAN）
  - P1-4 ModelPackManager 合并 vault specs
  - P1-5 分时复用调度器（24GB 显存友好，--phased）
  - P1-6 TAEP 桥接（vault → SemanticSegment）
  - P1-7 主项目只读客户端（VaultClient）
  - P2-3 高级检索（以图搜图 + BPM 匹配 + 组合查询）
  - P2-5 向量检索 IVF 索引（大规模加速）
  - P2-7 结构化日志 + 任务历史
  - P2-2 增量索引（watchdog 监听）
  - 测试：135 → 280+1，代码：6478 → 9909 行，源文件：41 → 50

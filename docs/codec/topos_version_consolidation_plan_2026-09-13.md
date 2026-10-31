# V 代际收纳与写端收敛计划（topos_version_consolidation_plan）

日期：2026-09-13 · 状态：**已结项（批 0~5 全部完成，ADR-C046 入库）· 独立收纳窗口**
结项对照（§10 成功标准）：①写面 3 入口 1 默认 ✓；②读面五代 + 六代干净拒绝 ✓；③覆盖等价迁移 + corpus_retired + TOPOS_DEV_REPLAY 回放 ✓；④能力面三方一致 ✓；⑤proxy 口径统一（A/B −19~20% 落档）✓；⑥保留代 golden SHA 全程零变化 ✓；⑦ADR-C046 入库，V9 立项引用 ✓。收尾门禁：ctest 79/79 + pytest 731 绿。
批 4 记录：D1=V7-A 归档落地——探测链四入口 + mov V7-A 目录分支生产拒绝
（V7-B 分支重排保留，TRAW 底座零影响）；bridge 写端拒绝；能力位四方下线
（manifest 先行 false → version.c 不广播 → 头文件/binding 注释退役 → 门测
试自动钉）；V7-A 专属 6 测试移入回放构建（自带 TOPOS_DEV=1）；生产 ctest
79/79 + pytest 731 绿，保留代 golden SHA 零变化。详见审计文档 §5d。
批 3 记录：frame_header 单点读端闸门（major 3..6 + V2-Rice + V7-R →
UNSUPPORTED_VERSION 含 retired/ADR 指引）；validate_v7a_common_fields 的
(6,4) 规则载体经 validate_impl(allow_retired) 豁免；TOPOS_DEV_REPLAY 构建期
option + TOPOS_DEV=1 运行期双重门三态实测通过（归档 V3 golden 考古可回放）；
test_retired_read 新增（+1 ctest，patch fixture + corpus_retired 全拒绝 +
保留代对照）；ctest 85/85 + pytest 731 绿，保留代 golden SHA 零变化。
详见审计文档 §5c。
批 2 记录：写域 {0,1,8,9} 落地（em2..7 → INVALID_ARGUMENT 含 ADR 指引）；
Python 合法域 {v1,v2,rans2} 构造期拒绝；双 CLI 收缩；proxy 显式 em8 且
A/B 体积 −19~20%（改善）；V7-A 桥载体 em2→em0 保持批 4 前可用；退役写
路径拒绝四方钉死（native validate/rans2 选择域/Python 参数化/导出构造期）；
ctest 84（test_rans_slice 归档，覆盖由 rans2 承接）+ pytest 730 绿；
保留代 golden SHA 零变化。详见审计文档 §5b。
执行记录：批 0 审计落档 `topos_consolidation_audit_2026-09-13.md`（D1=V7-A 归档/D2/D3/D4 按建议默认拍板；cfg 选择器 em 与位流 em 两套编号的澄清见审计 §0，批 2/3 实施以审计 §0 映射为准）；批 1 迁移：conformance_codec_intra→conformance_codec_v7r2_intra（V3 网格 em8 等价重建，gen=ce0e11c2）、fuzz_intra_replay 重指向、test_intra.c→test_frame_scenario.c（场景腿迁 V7-R2、V3 引擎腿归档：range 往返/VLC 模式语法/质量守卫/越界样本检查）、unit_ctx_ceiling sel7→8、stage9 parity 腿 {0,1,2}→{0,1,8}、mov 版本同步腿 4..6→{0,1,8,9}、c1_scratch 删 sel7 腿；golden_codec_v3_intra.bin 归档 tests/fuzz/corpus_retired/（SHA 完整）；ctest 85 项总数不降、保留代 golden SHA 零变化、pytest 726 绿。
性质：**不动任何保留代际的位流语义**（保留代 golden SHA 零变化是硬门禁）；只收敛
编码入口、退役死轴、对齐能力面、立版本预算纪律。
排期：批 0/1 可立即开始（与任何窗口无冲突）；批 2/3 触碰 `frame_header.c`/`codec.c`
版本路径，**须在 TRAW 批 1 合入之后、V9 批 1 之前串行执行**（合并顺序纪律见 §8）。
适用读者：**全新会话的执行 agent**——本文档自包含，按 §0 启动。

---

## 0. 新会话引导（先读这节）

**项目**：Topos Color 图像/视频编辑器，自研 topos_codec（C 库 + Python 绑定）。
本计划 = **收纳 V1..V8**：8/29~9/12 两周实验梯子留下 8 个位流代际，其中 6 个是
已被 rANS 线淘汰的死轴。本窗口做四件事：
①写端收敛（产品写面 = V7-R2 默认 + V2-VLC/RDO 保留 + V1 回退）；②读端退役
（V2-Rice/V3/V4/V5/V6/V7-R → 干净拒绝）；③V7-A 处置 + 能力面清理；
④ADR 立版本预算纪律（实验轴不再各开新 major）。

启动 checklist：

```bash
cd "/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01"
# 构建/测试（三个命令就是全部门禁）：
cmake --build native/topos_codec/build/release --target topos_codec
ctest --test-dir native/topos_codec/build/release --output-on-failure -j8   # 84 项
".venv/bin/python" -m pytest tests/native tests/shared -q                   # ~718 项
```

**必读文件**（按序，行号为 2026-09-13 实测）：
1. 本文档 §1（版本全景表——每一代是什么、哪天生的、谁在用）
2. `native/topos_codec/src/codec/codec.c:274-319`——`cfg_to_frame_header` 的
   `em = cfg->reserved[0]&0xFF` 版本选择器（1→V2-VLC、2→V2-Rice、3→V3、4→V4、
   5→V5、6→V6、7→V7-R、8→V7-R2、9→V8、其余→V1）；`:380-430`——cfg 域校验
   （reserved[0]∈{0..9}；仅 em==9 允许 reserved[3]/[4]）
3. `native/topos_codec/src/bitstream/frame_header.c:48-147`——版本规则单点
   （ADR-C027 D-8）；`:482-487`——解码接受清单 major 1..8（**退役要收缩这里**）
4. `src/shared/export/topos_encoder.py:350-373`——entropy_mode 解析与默认值
   （**缺省 'rans2'，ADR-C038 起为产品默认**；合法值 v1|v2|intra|intra-range|
   acpair|acpair_table|rans|rans2）；`:693-696`——写端旗标三元嵌套链；
   `:392`——`rdo_mode='on'` 须 `entropy_mode='v2'`（V2-VLC 写路径的存在理由）
5. `src/shared/media/proxy_generator.py:595-601`——代理编码 config **未设
   reserved[0]** → 缺省 V1（与主导出线 V7-R2 口径不一致，批 2 修）
6. `native/topos_codec/src/version.c:42-75`——能力位（含 `TC_CODEC_CAP_V7A_BAND_DECODE`）
   与 V7-B 可伸缩状态（REFERENCE，RD4-08 门禁未过但它是 TRAW 批 3 底座，**不动**）

**关键既有资产（本窗口依赖，不重写）**：
- golden/conformance 体系：`tests/conformance/golden_codec_v1*.bin`（含 `_vlc`
  与 `_intra` 宿主）、`golden_bitstream_v1.bin`、`golden_mov_v1.bin`、
  `golden_v8_v1.bin`（gen|check 双模式协议）；
- fuzz corpus：`fuzz_replay/fuzz_bitstream_replay/fuzz_codec_replay/
  fuzz_intra_replay/...`（按代际分宿主）；
- manifest 一致性门：`tests/media/test_capability_manifest.py`（先改
  `native/topos_codec/docs/capability_manifest.json` 再实现——仓库规则）。

---

## 1. 背景与版本全景

### 1.1 两周八代的由来

8/29（V1 基线）→ 8/31（V2 VLC/Rice，M9）→ 9/6~9/7（V3/V4/V5/V6 四个预测/熵
实验轴）→ 9/8~9/10（V7-A/V7-R/V7-R2/V7-B）→ 9/12（V8）。每个实验轴各开一个
major 以保持 golden 隔离——**纪律本身正确**（旧位流零风险），代价是留下大量
无产品价值的死轴：版本规则分支、job 分派分支、fuzz 面、测试宿主、以及后续
计划文档（TRAW/V9）反复解释它们的认知成本。

### 1.2 版本全景表（2026-09-13 三路调研 + git 考古）

| 代 | 内容 | 出生 commit | 写端可达 | 读端依赖 | 判定 |
|---|---|---|---|---|---|
| **V1** | Rice run/level 基线 | 8/29 阶段 4（83c95bb4 起） | app `'v1'` 显式；**proxy_generator 缺省误入** | T1.5 前全部存量文件；alpha 预算探测口径（topos_encoder.py:1007-1011） | **保留读写** |
| **V2-VLC** (em=1) | VLC 熵（canonical） | 8/31 M9（be78df98/5726d9ec/93e72e84） | app `'v2'`；**RDO 唯一宿主**（:392） | T1.5~ADR-C038 期间产物 | **保留读写**（RDO 依赖） |
| V2-Rice (em=2) | 选择器存在、无旗标映射 | 8/31 | 仅 native em 值 | 极少（实验） | **退役** |
| V3 (em=3) | 帧内预测 slice 原型 | 9/6 c00ae8d3 | app `'intra'`、CLI `intra` | 仅实验窗口文件；conformance_codec_intra 宿主 | **退役**（宿主批 1 迁移） |
| V4 (em=4) | C1 AC-pair 实验 | 9/7 36717098 | app `'acpair'` | 仅实验窗口 | **退役** |
| V5 (em=5) | C2 逐 slice pair 表 | 9/7 b0907366 | app `'acpair_table'` | 仅实验窗口 | **退役** |
| V6 (em=6) | 自适应 range-coded intra | 9/7 9725d5f0 | app `'intra-range'`、CLI `intra-range` | 仅实验窗口 | **退役** |
| V7-A | band 分段解码（(5,5,2) 专用入口） | 9/8 6a9ea8b7 | 内部桥 | 能力位 `V7A_BAND_DECODE` 广播中；rd2/rd3 资产 | **批 0 审计后定**（D1） |
| V7-R (em=7) | 单链 rANS（ADR-C034，−8.7/−9.1% vs V2） | 9/10 c0a59ba2 | app `'rans'`、CLI `rans` | 少（V7-R2 前窗口） | **退役**（V7-R2 严格包含其能力） |
| **V7-R2** (em=8) | rANS + order-1 上下文（ADR-C036/C038） | 9/10 dcc91ab5 | **产品默认**（'rans2'） | ADR-C038 后全部产物；A2 GPU 融合目标（codec.c:7801-7812） | **保留读写**（核心写代） |
| **V7-B** | 可伸缩金字塔 minor-2~4 | 9/9 rd4 包 | opt-in（REFERENCE） | TRAW 批 3 底座 | **保留**（本窗口不碰） |
| **V8** (em=9) | 段化多链 rANS + 瓦片共享表 | 9/12 | CLI `v8`（app 未接） | 8K GPU 唯一路径；生产切换拍板悬置 | **保留读写** |
| V9 | 帧间微 GOP（422 LP） | **仅计划** | — | — | 不在本窗口 |

### 1.3 收敛原则

1. **位流代际只能收敛，不能合并**——一代要么可读要么不可读，没有中间态。
   "合并"的正确形态 = 写面收敛到最少代 + 读面保留最小必要集 + 死轴干净拒绝。
2. **写端与读端分离处置**：写面收缩先行（旧二进制仍可读退役代产物）；读端
   退役后行，中间态合法。
3. **em 编号永久封存不复用**：退役的 em=2..6 与 V7-R 的 em=7 不再分配给任何
   新语义（防"同选择器不同代"考古灾难）；新代走新编号。
4. **退役 ≠ 破坏**：退役代文件返回 `UNSUPPORTED_VERSION`（干净、可诊断），
   绝不是 malformed/crash/静默误解码。

### 1.4 写端口径现状（三处，两处不一致）

| 写点 | 现状 | 口径 |
|---|---|---|
| `ToposVideoEncoder`（主导出/渲染缓存） | entropy_mode 缺省 `'rans2'`（ADR-C038） | V7-R2 ✓ |
| `proxy_generator`（代理） | config **未设 reserved[0]**（proxy_generator.py:595-601） | **V1 ✗**（缺省漏入） |
| alpha 预算探测 | 固定 reserved[0]=0（topos_encoder.py:1007-1011，T1.5 深度决策） | V1（**允许**：探测口径非产物，alpha 路径体积与熵版本无关，不改） |

---

## 2. 目标终态（完成定义）

| 面 | 终态 |
|---|---|
| 产品写面 | **V7-R2 默认 + V2-VLC（RDO 专用）+ V1（显式回退）**，三个入口，一个默认 |
| 产品读面 | V1、V2-VLC、V7-R2、V7-B（opt-in）、V8 五代；V7-A 视 D1 |
| 退役面 | V2-Rice、V3、V4、V5、V6、V7-R → 写端拒绝（INVALID_ARGUMENT"已退役"）+ 读端 `UNSUPPORTED_VERSION`（信息含退役 ADR 号）；em 编号封存 |
| 能力面 | capability manifest / flags / inspect / probe 与实际读写面一致（manifest 门测试绿） |
| 纪律面 | ADR-C0xx《位流代际生命周期》：新 major 只留给入产窗口；实验轴用 dev 旗标/分支 + 测试构造隔离；每进入新 major 必须同步登记前序死轴退役评审点 |

---

## 3. 拍板项（执行前须用户确认；均有建议默认）

| # | 拍板项 | 建议 | 依赖 |
|---|---|---|---|
| D1 | V7-A 处置：保留 band decode 还是归档 | 审计后定；若 timeline_preview/reduced 路径无真实依赖 → 归档 + 能力位下线 | 批 0 审计 |
| D2 | 退役代读端语义 | 生产 `UNSUPPORTED_VERSION`（信息含 ADR 号）+ **测试构建 `TOPOS_DEV=1` 旗标保留回放**（防考古成本，零生产面） | 无 |
| D3 | proxy_generator 写入口径 | 统一到缺省 V7-R2（与主导出线一致）；sized 搜索参数不动 | 无（批 2 执行） |
| D4 | V8 生产切换拍板是否与本文合并成一个 ADR | 建议合并（同一"写面收敛"主题，一次拍清；V8 切换本身仍按 `topos_v8_production_switch_2026-09-12.md` 的三重开条件独立判定） | 用户 |

**注意**：写端默认已是 V7-R2（ADR-C038 已拍板），本计划**不含**"默认切换"
拍板项——它已在历史中完成。

---

## 4. 批次分解（每批独立提交 + 落档 + 全量门禁）

**总估计：批 0 半天；批 1 一天；批 2 一天；批 3 一到两天；批 4 半天到一天；
批 5 半天。合计 ≈ 3~5 天（agent 节奏）。**

### 批 0：审计（零改动，产出决策材料）

四张清单，全部落档 `docs/codec/topos_consolidation_audit_<date>.md`：

1. **写点普查**：全仓 grep `reserved[0]`/`entropy_mode`/`coding_selection`——
   确认产品可达写点仅有 topos_encoder（三元链）/proxy_generator（V1 缺省，§1.4）/
   CLI（encoder_cli.c:79-88）/native em 直传；确认无 UI/preferences 面暴露
   entropy_mode（预期：无）。
2. **读依赖普查**：`dec_slice_job`（codec.c:7633-7832）逐分支标注宿主代际；
   auto2k/reduced 解码路径（codec.c:8690 起）与 V7-A band decode
   （`V7A_BAND_DECODE` 能力位、binding:65/547-548、timeline_preview_renderer
   "auto2k" 阶段）的引用链定界——**auto2k 是通用缩放路径（V8 才显式
   NOT_IMPLEMENTED），预期与 V7-A 解耦，用证据钉死**。
3. **测试宿主地图**：84 ctest + fuzz corpus 逐项标注宿主代际
   （conformance_codec_vlc→V2-VLC、conformance_codec_intra→V3、
   fuzz_intra_replay→V3 等）；产出批 1 迁移清单与批 3 归档清单。
4. **存量文件面**：用户自述 9/6~9/12 CLI 实验文件普查方式（本仓库
   bench/test 产物清单 + 用户本机导出目录）；产出"退役是否影响任何现存文件"
   的结论。

**验收**：四张清单落档；D1/D2/D3 拍板材料齐；零代码改动、门禁绿。

### 批 1：测试宿主迁移（行为零变化）

- 把以**退役代际**为宿主的 golden/conformance/fuzz 迁到保留代际：
  conformance_codec_intra（V3 宿主）→ 在 V7-R2/V1 上重建等价覆盖（同几何矩阵
  × 位深 × qp 网格）；fuzz_intra_replay 同理；V2-Rice/V4/V5/V6 若有宿主一并迁。
- 迁移等价性门：迁移前后**对同一输入的覆盖块数/几何/位深网格等价**（清单化
  对照），不许借机删覆盖。
- fuzz corpus：退役代种子移入 `tests/fuzz/corpus_retired/`（归档保留，
  D2 的 `TOPOS_DEV` 回放用），不删除。
- **验收**：门禁全绿且用例总数不降；保留代 golden SHA 零变化（本批未触任何
  写读路径，SHA 变化即为事故）。

### 批 2：写端收敛

- `ToposVideoEncoder`：entropy_mode 合法域收缩为 `v1|v2|rans2`（D3：缺省仍
  `'rans2'`）；`intra/intra-range/acpair/acpair_table/rans` 传入 → 显式
  `ValueError`（信息指向本文档，**禁止静默回落**）；`:693-696` 三元链收缩为
  三态。`rdo_mode='on'` ⇄ `'v2'` 的耦合校验（:392）保持。
- `proxy_generator`：config 显式设 `reserved[0]=8`（D3 统一口径）；补一条
  代理往返测试断言产物 bitstream_major==7/em==8。
- CLI `--entropy` 收缩为 `v1|vlc|rans2|v8`（+保留读说明文本）；usage() 同步。
- native 写域校验（codec.c:380-430）：reserved[0] 合法域 {0,1,7,8,9}
  （读域不变——旧二进制兼容原则，本批不动任何读路径）；em∈{2..6} 编码入口
  返回 INVALID_ARGUMENT（信息含"retired, see ADR-C0xx"）。
- **验收**：门禁全绿；V1/V2-VLC/V7-R2/V8 四代 round-trip golden SHA 零变化；
  退役 em 写路径拒绝测试钉死；proxy 产物代际断言绿。

### 批 3：读端退役（依赖批 2 合入 + D2 拍板）

- `frame_header.c:482-487` 接受清单收缩为 {1,2,7,8}（V7-B 走专用入口不受
  影响）；`dec_slice_job` 退役分支移除；`tc_frame_decode` 探测链与
  `tc_packet_parse_structure`（packet.c:13-24）对退役 major 返回
  `UNSUPPORTED_VERSION`，**错误信息含"generation N retired (ADR-C0xx)"**。
- `TOPOS_DEV=1`（仅测试/debug 构建编译面，D2）：退役代回放路径保留，供
  corpus_retired 考古；生产构建零代码面。
- `topos_inspect`/probe：对退役代文件保留**只读头解析**（不解码）→ 输出
  "retired generation N"诊断信息（诊断工具必须能解释文件为什么打不开）。
- **验收**：退役代文件读 = UNSUPPORTED_VERSION（不 crash/不误解码/不越界），
  fuzz 钉死；保留五代 round-trip golden SHA 零变化；inspect 诊断路径测试。

### 批 4：V7-A 处置 + 能力面清理（依赖批 0/D1）

- 若 D1=归档：V7-A 专用入口（(5,5,2) 合同）与 band decode 路径按批 3 同款
  退役规程；`TC_CODEC_CAP_V7A_BAND_DECODE` 能力位下线（version.c:50-55、
  binding、manifest 三方同步，门测试自动钉）。
- 若 D1=保留：仅做能力位/文档对齐（band decode 状态写入 manifest）。
- 能力面全面对齐：capability manifest 的 bitstream_profiles/container/flags
  与终态读写面一致；`topos_capability_summary`（topos_profiles.py:280-356）
  同步。
- **验收**：manifest 三方等式门绿；capability 查询与实际行为一致测试。

### 批 5：文档与纪律收口

- **ADR-C0xx《位流代际生命周期与退役规程》**：§2 目标终态表 + 版本预算纪律
  （新 major 只留入产窗口；实验轴 dev 旗标/分支 + 测试构造隔离 golden；
  进新 major 必须同步登记前序死轴退役评审点；em/major 编号封存规则）。
- 若 D4=合并：同 ADR 内附 V8 生产切换拍板记录（三重开条件引用
  topos_v8_production_switch 文档）。
- 活跃计划文档交叉引用更新（TRAW/V9 计划的关联段各加一行指向本 ADR）；
  历史实验文档（a 系列/rd 系列/C 系列报告）**不改**（档案性质）。
- SDK/CLI 文档与 i18n 错误文案（退役信息 10 locale）。
- **验收**：全门禁绿；文档无内部矛盾（脚本 grep 交叉引用）；ADR 编号登记。

---

## 5. 审计与测试清单（批 0/批 1 的执行粒度）

写点普查 grep 面：`src/shared/`、`src/features/`、`native/topos_codec/src/cli/`、
`tools/`（bench 脚本里的 coding_selection）。
读端分支清单（codec.c:7633-7832 逐条）：alpha plane3 流（7655-7706，保留）/
V7-R2 稀疏（7798-7811，保留）/V3/V6 intra（7821-7824，退役）/direct-scan
（7825-7827，判宿主）/generic sink（7828-7832，保留）。
测试宿主迁移对照表格式：`测试名 | 现宿主代际 | 迁往 | 等价覆盖证据`。
每批回归：三命令门禁 + 保留代 golden SHA 清单比对（V1 家族/V2-VLC/V7-R2/V8/
mov/image 全集）。

## 6. 回归纪律（每批提交前）

1. 三命令门禁全绿；用例总数不降（迁移等价门）。
2. **保留代（V1/V2-VLC/V7-R2/V7-B/V8）golden SHA 零变化是硬门禁**——本窗口
   不产生任何新位流形态。
3. 退役语义测试（写拒绝/读 UNSUPPORTED_VERSION/inspect 诊断）每批重跑。
4. 每批独立提交（`refactor(v-retire): 批 N——…` 中文摘要含数据与判定）+
   落档 + 本文档批次状态行更新。
5. 与 TRAW 批 1 / V9 批 1 的同文件冲突（frame_header.c/codec.c）按 §8 顺序
   纪律处理，禁止并行改。

## 7. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 退役代际存在未普查到的存量文件（用户本机实验导出） | 打不开旧文件 | 批 0 清单 4 普查；D2 的 TOPOS_DEV 回放兜底；退役信息含 ADR 指引 |
| 测试宿主迁移时悄悄丢失覆盖 | 回归面扩大 | 批 1 等价门（清单化对照、总数不降） |
| inspect/probe 对退役文件 crash 或误解码 | 诊断事故 | 批 3 只读头解析路径 + 专项测试 |
| 与 TRAW 批 1/V9 批 1 同文件冲突 | 合并事故 | §8 顺序纪律；批次粒度小、每批独立提交 |
| V7-A 能力位有隐藏消费者 | 下线破坏 | 批 0 引用链定界先行；能力位下线走 manifest 门测试 |
| proxy 口径切换改变代理体积基线 | 缓存/对比基准漂移 | 批 2 前后体积 A/B 落档（预期 V1→V7-R2 代理更小，属改善）；旧代理可再生 |
| 静默回落被当成退役（写端拒绝实现成回落 V1） | 代际泄漏 | 批 2 显式 ValueError 测试钉死 |
| 误动 V7-B/TRAW 底座 | TRAW 批 3 受损 | V7-B 全链列本窗口禁改清单（§9） |

## 8. 与其他窗口的顺序纪律

```
TRAW 批 1（frame_header/version 白名单扩展，开发中）
   ↓ 合入后
本计划 批 0~2（审计/迁移/写端，可随时）
   ↓
本计划 批 3~4（读端退役/V7-A，动 frame_header.c:482-487 与 dec 分派）
   ↓ 合入后
V9 批 1（新 major=9 分支 + 接受清单 +9）
```

理由：三者都动 frame_header.c/codec.c 版本路径；TRAW 批 1 是纯枚举加法、
本计划批 3 是收缩、V9 批 1 是加法——按"加法→收缩→加法"排序最小化冲突；
任意两者并行改同文件为明确禁止项。

## 9. 明确不做（本窗口范围外）

- **V7-B 全链不碰**（TRAW 批 3 底座；REFERENCE 状态语义不变）；
- V8 生产切换决策（独立拍板，D4 仅合并 ADR 归档）；
- 保留五代（V1/V2-VLC/V7-R2/V7-B/V8）的任何位流语义/熵核/量化改动；
- RDO 功能演进（V2-VLC 写路径仅因 RDO 保留；RDO 上 rans2 属未来独立项）；
- 退役 em/major 编号复用（永久封存）；
- TRAW/V9 排期调整；
- 历史 a/rd/C 系列实验报告的改写（档案不动）。

## 10. 成功标准（立项时冻结，结项对照）

1. 产品写面 = 3 入口 1 默认（V7-R2）；entropy_mode/CLI 旗标域收缩落地，退役
   传参显式报错（含文档指引）；
2. 读面 = 保留五代 + 退役六代干净拒绝（UNSUPPORTED_VERSION 含 ADR 指引），
   fuzz/损坏矩阵钉死，无 crash/误解码路径；
3. 测试覆盖等价迁移（总数不降），corpus_retired 归档 + TOPOS_DEV 回放可用；
4. capability manifest/flags/inspect/probe 与实际读写面三方一致（门测试绿）；
5. proxy_generator 口径统一（产物 bitstream_major 断言绿，体积 A/B 落档）；
6. 保留代 golden SHA 全程零变化；
7. ADR-C0xx 代际生命周期纪律入库，后续窗口（V9）引用。

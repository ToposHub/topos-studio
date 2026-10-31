# ADR-I009：格式链路从零审计（二轮）修复

日期：2026-09-05
状态：已实施（门禁全绿）
关联：ADR-I007（一轮审计修复）、ADR-I008（扩展名迁移）、`topos_image_file_spec_v0.md`

## 背景

应用户要求做第二轮"陌生人视角"从零审计：抛开一轮结论与计划书，spec → C 容器层
（container/reader/writer）→ ABI 头 → CLI → Python 绑定 → 媒体源 → 序列源 →
互转工具逐层核对，每个可疑点以独立复现脚本验证后修复。本轮共确认 8 项真实缺陷
（2×P1、4×P2、1×P3 + D 级若干），全部修复并钉回归测试。

## 修复清单

### P1 toos_tools 刻度契约违反（scripts/toos_tools.py）

codec 契约（topos_codec.h 输入/输出约定）：彩色平面 = 声明位深满刻度
（0..2^bd−1），**alpha 平面恒为 16-bit 满刻度容器（0..65535）**。

- **encode alpha 刻度错误**：旧 `to_bd` 对 alpha 也做彩色位深缩放——
  8-bit 不透明 PNG（alpha=255）编出的文件 alpha 容器值 1020/65535 ≈
  **1.6% 不透明度**（文件语义错误，任何按契约消费的端都显示近乎全透明）；
- **encode 16-bit 回绕**：`p << 8` 在 uint16 上模 65536 回绕，16-bit PNG
  彩色/alpha 只保留低 8 位（R=40000 → 16384）；
- **decode alpha 回绕**：对 0..65535 容器统一 `<<(16-bd)`，alpha > 2^bd
  即回绕。8-bit 输入下 encode/decode 两错误恰好抵消，是长期未暴露的原因；
- 次生：YUV 拒绝检查晚于全分辨率平面切片，YUV422 输入触发裸 ValueError
  而非设计好的拒绝消息；`src_bits` 在 astype 之后计算恒报 16-bit。

修复：encode 统一升到 16-bit 精确值后彩色四舍五入+饱和到 2^bd−1、alpha 恒等
直通；decode 彩色左移对齐、alpha 恒等直通；YUV 拒绝提前到切片前；src_bits
在 dtype 判定时记录。回归：`tests/media/test_toos_tools.py`（5 用例，含
A16 容器语义与 16-bit 精确往返断言）。

### P2 tc_image_write 库层契约缺口（image_writer.c）

- **compatibility_flags 未校验**（V4 复现）：传 0xFF 写出成功，自家 probe
  即 `BAD_PREAMBLE`——违反"writer 不得产出非法 envelope"。修复：minor=0
  掩码 0x3 之外拒绝（INVALID_ARGUMENT）；
- **extras 重复 FourCC 未拒**（V6 复现）：双 XMP 可写出，read_metadata 只
  回第一个，违反 spec §2.9 单一权威。修复：extras 之间重复类型拒绝；
- 顺手：`write_params.reserved[]` 非 0 拒绝（对齐 codec P1-10 风格）。

回归：`tests/unit/test_image_writer.c` test_write_rejects 扩展 3 组
（含合法值照常通过的对照路径）。

### P2 序列源并发缺陷（topos_image_sequence.py）

- **预取 pending 丢弃竞态**（V2 复现）：旧 worker 按出生代号退出，
  `prefetch_around` 每次必 +1 代 → "worker 自取 pending"分支不可达
  （死代码），worker 在飞时新窗口被静默丢弃，须重试才生效。修复：
  `_pending` 携带 `(gen, frame, direction)`，worker 采用 pending 自带
  代号；被取代时弃窗回循环顶自取最新 pending；所有退出路径（含异常，
  try/finally）在锁内置 `_worker_done`。修复后：`prefetch_around` 在锁内
  观察到 not done ⇒ worker 必然再进锁读一次 pending ⇒ 请求必达；
- **PlaneCache 重复 put 记账漂移**（V3 复现）：同 key 覆盖不扣旧值，
  `_bytes` 单向虚高致过早淘汰。修复：覆盖前扣除旧条目字节。

回归：`test_topos_image_sequence.py` 新增
test_prefetch_pending_not_dropped / test_plane_cache_duplicate_put_accounting。

### P3 plane_view ABI 域错误（5 处调用方 + C 校验）

`topos_plane_view` 是 codec 域结构，`abi_version` 须填
`TOPOS_CODEC_ABI_VERSION`(=2)；曾有 5 处误填 `TOPOS_IMAGE_ABI_VERSION`(=1)
未被察觉（Python 绑定、OIIO 插件、fuzz_image、test_image_reader×3——
复制传播）。修复：5 处改 codec 域值；`tc_image_decode` 补校验
（struct_size 非零时 abi 必须匹配；全零视图头保持历史宽容）。回归：
test_decode_plane_view_abi（误填→INVALID_ARGUMENT；正确值→正常解码）。

### D 级

- `tci_idsc_crosscheck` Preview 组合补 bit_depth∈{10,12}（spec §9.1；
  codec 层已拒绝 pf0+8bit，纯纵深防御）；
- `tci_validate_ocio` 增加严格 UTF-8 序列校验（RFC 3629：拒过长编码/
  代理区/越界/孤立续字节；旧实现只拦控制字符）；
- `tc_image_read_metadata` 内容校验失败时 `*out_size` 复位 0（头文件
  契约同步更新）；回归见 test_image_reader 扩展断言。

## 记录性偏离（不改码）

- spec §6 "chroma_siting：4:4:4 → 0" 不在文件层强制：codec 仅对 GBR
  （pf=2）强制 siting=0，YUV444 允许 {1,2}；硬性文件层校验会拒绝
  codec-legal 内容。按"codec 域字段以 codec 校验为准"处理。

## 复核追加（同日第二轮）：OIIO 插件（plugins/oiio_topos/topos_imageio.cpp）

主修复提交后复核发现 OIIO 插件（R-I15 未验收、本机无 SDK 从未编译）携带
**8 处缺陷**，其中 4 处与 toos_tools 同族、4 处属"从未编译暴露"：

同族刻度/语义类：
- 读侧裸拷贝：codec 彩色平面（0..2^bd−1）直塞 OIIO uint16（0..65535 语义）
  → 图像 ~1.5% 亮度（头注释声称左移对齐但实现漏了）；
- 写侧裸透传：OIIO 16-bit 满刻度直接喂 `bit_depth=10` encoder（codec 期望
  0..1023）→ 高位全废；
- YUV422（Preview）读侧按满分辨率整平面打包，半宽色度平面错位花屏；
- `write_scanline` 忽略 `y`（乱序/重复 scanline 被静默拼接）。

从未编译暴露类（HEAD 提交版总花括号深度=2，文件从未通过任何编译）：
- 两处 `format_name` 内联定义缺右括号（语法错误）；
- `m_planes[0] + row` 把 vector 元素引用当指针（整数运算，类型错误）；
- `decode_all` 局部 `uint32_t w = 0, h = 0;` 声明后从未赋值，四个平面指针
  全部指向缓冲开头（互相覆写）。

修复：读侧彩色 `<<(16-bd)`、alpha 直通；写侧彩色四舍五入+饱和、alpha 直通；
pf=0 显式拒读（与 toos_tools 同策略；pf=1/pf=2 可读）；scanline 顺序强制；
rename 前补 spec §12 写后 validate；`errorf` 字符串实参 `.c_str()`（对真
OIIO fmt 语义与 C varargs 均合法）。

防线：新增 `plugins/oiio_topos/syntax_check.sh` + stub OIIO 头
（`-fsyntax-only -Wall -Wextra -Wpedantic`，镜像插件用到的 OIIO 2.x 公共
API 面）——在 R-I15 真 SDK 验收前持续防"未编译代码腐化"。本机该门禁通过。

## 小项清理（同日第三轮，应业主要求清尾）

- toos CLI decode sidecar 增加 `alpha_bit_depth`（A16 语义诊断，测试断言钉死）；
- CLI `file_length` 改为对已打开句柄 fstat（Windows `_fstat64`）：消除二次
  fopen 与"打开失败=空文件"的 0 值歧义；顺带删除不再使用的 `tp_tell`；
- CLI encode 临时文件名 `snprintf` 截断显式拒绝（超长路径不再静默落错名）；
- `scan_sequence` step 锚点语义冻结并文档化：anchor = 显式 start，
  start=None 时锚定绝对帧号 0（过滤结果不依赖目录内容）；新增测试；
  顺带修测试 fixture `_make_sequence` 对负帧号用 naive `f"{v:0{pad}d}"`
  少一位数字的问题（统一走 `frame_file_name`）；
- OIIO 读侧元数据拆分：`topos:AlphaMode`（int，codec 枚举，恒有值）与
  `topos:AlphaAssociation`（string，straight/premultiplied，仅有 alpha 时）——
  不再同键双类型。

## 门禁

debug/ASan/UBSan ctest 57/57（三套件）；Python 图片相关 65 + topos
相关回归 1142 全过；check_exports 216 ≥ 36；image fuzz 回放 driver
在 ctest 内通过（libFuzzer runtime 本机不可用，与既有口径一致）。

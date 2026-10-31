# ADR-I007：计划符合性审计修复（2026-09-05）

状态：已实施。背景：对阶段 0–12 交付做计划书符合性审计，发现若干
"文档即契约"偏差与语义缺口，本 ADR 记录修复决策与遗留豁免。

## D1 Alpha 元数据链路对齐视频链（阶段 5 验收线修复）

- `ToposImageSource` extra 键 `alpha_premultiplied`/`concealed_slices`
  更名 `topos_alpha_premultiplied`/`topos_concealed_slices`——全部 6 个
  消费方（async_renderer/video_frame_node/edit_gpu_preview_renderer/
  topos_encoder/proxy_generator）按视频链键名读取，旧键导致 premultiplied
  语义整链失效（unpremultiply 分支永不执行，边缘光晕伪影）；
- 带 Alpha 的 pixel_format 注册名从 3 平面基础名（gbrp10le 等）改为视频链
  同源 `topos_*a{N}`（topos_gbrap10a16 等）——`format_info.has_alpha`/
  `planes` 与实际 4 平面一致，次要消费路径不再静默压平 Alpha；
- alpha 平面按 spec §8.6 交付"声明位深满刻度"（mode2 右移 16-N，
  与视频链同源）；plane_infos 逐平面位深（色度=bit_depth、A=alpha_bit_depth）；
- 新增带 Alpha golden（golden_image_v1_alpha.bin）的端到端 pytest。

## D2 C 层契约修复（spec 一致性）

- `topos_image.h` 的 `topos_image_info.plane_count` 注释纠正为"含 alpha
  平面（3/4）"——reader 实填内层 header 的 plane_count，旧注释会诱导
  外部调用方按不含 alpha 分配缓冲（越界写）；
- preamble：unknown major 改返 `TC_ERR_UNSUPPORTED_VERSION`（原
  BAD_PREAMBLE）；minor 不再硬拒（spec §14"读端必须容忍"，v1 读端接受
  同 major 任意 minor）；
- compatibility_flags：minor=0 时未定义位（>bit1）必须为 0（§9.3）；
  minor>0 容忍后续版本定义的新位（§14）；
- IDSC reserved[88..127] 必须为 0（§6，与 preamble/directory reserved
  的强制风格一致执行）；
- chunk↔directory 重叠检查改为双向区间测试（旧单向检查漏掉"chunk 落在
  directory 区间内部"的别名情况，违反 §2.4）；
- `tci_idsc_crosscheck` 新增 image_profile ↔ codec 组合校验（§9.1 一一
  映射：Preview=YUV422/codec3、HQ=GBR10/codec5、XQ=GBR12/codec5|6），
  兑现 topos_image.h 对 `tc_image_write` 交叉校验的承诺——错标文件
  （YUV422 内容标 HQ）现返回 CHUNK_CONFLICT。

## D3 CLI 修复（tpic）

- `--alpha-mode 2` 位深缺省 16 → 12（codec 仅允许 8/10/12，旧行为必然
  config validate 失败）；mode2 编码把 `stats.alpha_max_abs_error` 实测
  上界写入 IDSC.alpha_max_abs_err（spec §8"必须记录"，原恒为 0）；
- benchmark：malloc 判空移到使用前（OOM 空指针写）；三条错误路径补
  采样数组释放；
- Windows 可移植守卫：`_fseeki64/_ftelli64`、`_commit`、`MoveFileEx`
  （rename 原子替换语义）、`remove`、QPC 单调时钟、`_getpid`——
  MSVC 可编译、`--overwrite` 在 Windows 语义正确（实际构建验证待
  Windows 环境，见 SDK §8）；
- `topos_plane_view.abi_version` 填 `TOPOS_CODEC_ABI_VERSION`
  （codec 域结构，原误填 image 域常量；当前两值相同为潜伏陷阱）。

## D4 序列源策略语义补齐（阶段 6）

- MissingFramePolicy 三态真实分化：skip=读缺失帧号跳到后继有效帧
  （默认）；error=打开时检测 [首帧,末帧] 空洞并报错（SequencePatternError）；
  hold=保留前驱帧且连续空洞超过 max_consecutive_missing 时报错；
- 重复帧号显式拒绝（此前 dict 静默保留最后者）；
- scan_sequence 的 padding 参数生效：padding>0 严格位宽匹配
  （frame_1.tpic 不混入 frame_0001.tpic 序列），padding=0 任意位宽；
- PlaneCache key 增加 codec 构建身份维度（库 stat 指纹；重构建后旧缓存
  不与新构建混用）；缓存平面置只读（与视频链解码帧契约一致）；
- 预取 pending 窗口：在飞 worker 存活时新窗口挂起由其自取（旧行为
  直接丢弃新窗口请求）；worker 退出前置 done 标志（与提交方互斥），
  关闭"挂起请求被静默丢弃"竞态；缓存写入前验代（close/seek 后旧结果
  不落缓存）。

## D5 门禁口径修正

- `pytest 61` 纠正：image 专属 49（source 14 + sequence 19 +
  capability_manifest 16）；原口径混入 9 个与 tpic 无关的视频 factory
  用例。SDK.md §7 与 stage_status.json 已同步；
- run_tests.sh 纳入 image pytest（3 个文件）与 topos_fuzz_image
  libFuzzer 短跑目标（此前 image 测试无自动化门禁入口）；
- `ToposImageSource.open()` 改用 probe（原 validate 对含 PIXL 全部 chunk
  流式 CRC ≈ 整文件多读一遍；decode 内部本就再 probe+整读 PIXL）。

## D6 布局偏离正式记录（对计划书 §6.1 的豁免）

计划书 §6.1 推荐的布局与实际不一致，确认为有意偏离而非缺失：

- `src/image/image_metadata.c/.h`、`image_sequence_primitives.c/.h` 不存在：
  metadata 读写并入 image_container.c（chunk 表/上限/受限校验）与
  image_reader.c（tc_image_read_metadata）；序列原语由 Python 侧
  `src/shared/media/topos_image_sequence.py` 承载（v1 序列消费方全部在
  Python 层，C 原语无调用方）；
- 文件名 `topos_image_sequence.py`（计划书写 `topos_image_sequence_source.py`）：
  该类非 MediaSource 协议全量实现，命名去掉 _source 后缀以免误导；
- `image_reader.h`/`image_writer.h` 不存在：公开声明集中于 topos_image.h，
  内部共享声明在 image_container.h；
- 计划书 §6.2 的建议能力 `tc_image_write_metadata`（折叠进 tc_image_write
  的 extra_chunks）与 `tc_image_cancel`（取消由序列层 generation 承载）
  以等价形态实现；`tc_image_query_required_buffer` 实名为
  `tc_image_query_decode_buffer`。

## 遗留（本 ADR 不关闭，见 SDK §8）

导出/缩略图/导入探测三处应用层接线、序列源接入播放链、Windows 实机构建、
NAS/冷缓存矩阵、真实素材质量基线、OIIO 宿主验收。

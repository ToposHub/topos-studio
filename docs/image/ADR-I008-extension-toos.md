# ADR-I008：图片扩展名迁移 `.tpic` → `.toos`（取代 ADR-I001 D1 的扩展名部分）

状态：已实施（2026-09-05）。开发期决策，无存量用户文件，不留兼容层；
`.tpic` 扩展名自本 ADR 起废弃。

## D1-A 动机

- `.toos` 发音顺口（中文用户近"图s"）、四字母、品牌关联（topos 去掉 p）；
- 外部查证：全网无任何文件格式占用 `.toos`（FILExt/FileInfo 等扩展名
  数据库均无登记）——比 `.tpic` 干净（后者是 Truevision TGA 的历史备用
  扩展名，AccuSoft/LEADTOOLS 等图像 SDK 至今登记）；
- 根除 R-I05：内层 packet Magic `TPIC` 与图片扩展名同名的歧义从命名空间
  上消失（裸 packet 的 `.tpkt` 后缀维持不变）。

## D1-B `.toos` 与 `.topos` 一字之差的接受论证

`.toos` 与工程包 `.topos` 仅差一个字母（删除 p）。接受该邻近性：

1. 扩展名的实际消费者是软件而非人：保存由应用自动追加、打开走文件
   对话框过滤器、双击走 OS 关联分发——三者在机器层面完全可区分；
2. 路由以内容 Magic（TPIM）为最终权威，扩展名仅作快速候选（spec §2.1
   既有决策，format_registry.is_toos_image + factory auto 探测）；
3. 互斥路由有测试锁定（.toos 注册为 image、.topos 不是 image、TPIM
   magic 探测、工程包不进图片路由）；
4. 打错类型的残余场景（口头传文件、发错文件）由清晰报错兜底；
5. 业界先例：同厂一字之差扩展共存无碍（Adobe `.psd`/`.psb`、
   DaVinci `.drp`/`.drt`）。

已知残留：英文语境 "toos" 会被读成 "tools"（营销层考量，中文主战场
无碍；技术层面无影响）。

## D1-C 迁移范围（全量、无兼容层）

代码（`.tpic`/`tpic` → `.toos`/`toos`）：

- C 层：CMake 目标 `tpic` → `toos`（add_executable/ctest COMMAND）、
  image_cli.c 用法文本与错误前缀、临时文件后缀 `.tpic.tmp` → `.toos.tmp`、
  inspect.c/topos_image.h/测试注释；
- Python：format_registry（`.toos` 注册、`is_toos_image`）、factory
  （后端 id `tpic_image` → `toos_image`）、binding/source/sequence 注释
  与报错文案；`scripts/tpic_tools.py` → `toos_tools.py`（git mv）；
- 测试：test_topos_image_source/sequence 的扩展名、后端 id、fixture
  文件名（`frame_0001.toos`）、test_* 名称同步；
- OIIO 插件：**format_name 由 `"topos"` 改为 `"toos"`**——OIIO 以
  format_name 作默认扩展名，旧值会让插件在宿主中认领 `.topos`（工程包
  扩展），属本迁移顺手修复的潜在冲突；
- 文档（活文档）：spec v0 §2.1/§13、SDK.md、capability_manifest
  （file_extension）、test_vector_manifest、stage_status；MIME 建议
  `image/x-topos-picture` → `image/x-toos`。

不变项：

- 文件格式内容层零变化：TPIM envelope、IDSC/PIXL chunk、内层 TPIC
  packet、全部 golden 文件字节不变（扩展名只是路由层命名）；
- `.tpkt`（裸 packet 测试后缀）、内层 Magic `TPIC`、`TC_IMG_*` ABI
  前缀、`topos_image.h` 结构名均不变；
- 视频链 `topos_source.py` 的 `_stsd_first_entry_is_tpic`/
  `_faststart_is_tpic`（MOV 内层 packet 探测）不变；
- 历史 ADR（I001–I007）与 perf_report_stage7.md 作为历史记录不改写，
  其中的 `.tpic` 字样以本 ADR 为准。

## D1-D 验收

- native debug/ASan/UBSan 全量、image pytest（含 `.toos` fixture 路由
  互斥断言）、check_exports、fuzz 回放全绿；
- `grep -rn "\.tpic"` 在 src/ scripts/ plugins/ tests/ 及活文档中零命中
  （历史 ADR/报告/计划书除外）。

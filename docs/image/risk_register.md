# Topos Image 风险登记表 v0

状态：阶段 0 建立，随阶段滚动更新（新增风险追加，关闭风险标注日期）。

| # | 风险 | 影响 | 概率 | 处理策略 | 状态 |
| --- | --- | --- | --- | --- | --- |
| R-I01 | `.toos`（图片）与 `.topos`（工程）/`.topos`（视频 MOV 命名空间）近似混淆 | 文件路由、双击关联、插件匹配错误 | 中 | Magic（TPIM）为最终依据；format_registry 只注册 `.toos` 为 image；测试覆盖路由区分；扩展名仅作快速候选（ADR-I008：`.toos`/`.topos` 一字之差的可接受性论证） | **已关闭（2026-09-05，阶段 5 测试锁定；ADR-I008 复核）** |
| R-I02 | 外层 TPIM 与内层 TPIC 版本语义混淆 | 兼容矩阵失效 | 中 | 版本字段分离；IDSC payload_major/minor 绑定；兼容矩阵写入 spec §14 | 缓解中 |
| R-I03 | 复制 codec 算法造成视频/图片分叉 | 双倍维护、结果不一致 | 中 | image 层只调 libtopos_codec 核心；`check_exports.py` 防第二实现 | 缓解中 |
| R-I04 | 把 reserved 能力写成已完成（示例集 2026-09-21 更新：~~Lossless~~ [已改派 RAW]、~~HALF~~ [已支持]；现行 reserved 集 = FLOAT32/tile/多图/mipmap） | 虚假宣传 | 中 | 支持矩阵由 conformance gate 驱动；capability_manifest 单一真相源 | 持续 |
| R-I10 | 浮点（HALF）与 RAW（TRAW）产品化后的跨面漂移（白皮书/SDK/manifest/CLI/应用五处档位表；LP 产品化时已出现白皮书漏更） | 集成方误导、档位名不一致 | 中 | 单一档位注册表 `TOPOS_IMAGE_TIERS`（2026-09-21 E3）+ E4 命名一致性回归测试；两份白皮书命名规则章节（N-1~N-8） | 缓解中 |
| R-I11 | H1 写侧默认 transfer 改派 sRGB(13) 触碰旧文件语义 | 旧文件被错误重解释 | 低 | minor≥7 扩展代门控（major=1 域）；旧文件不回填不重解释；golden 新旧双轨钉扎（golden_codec.c 显式 pin legacy 值） | 缓解中（2026-09-21） |
| R-I05 | 内层 packet Magic `TPIC` 与图片扩展名 `.tpic` 历史歧义 | 旧帮助文本/脚本把裸 packet 当图片 | 高（已存在） | `.tpkt` 后缀迁移；`topos_inspect --raw-packet`；帮助文本修正（阶段 1）；**扩展名迁移 `.toos` 后歧义根除** | **已关闭（2026-09-05，ADR-I008 扩展名迁移根除）** |
| R-I06 | 高熵 8K RGBA 逼近 256 MiB packet 上限 | 编码失败/内存峰值 | 中 | 策略 1：限尺寸；最坏输入基准（阶段 7）后才宣传 | 开放 |
| R-I07 | Alpha 预算损伤遮罩质量 | 毛发/烟雾劣化 | 中 | 默认 A16 lossless；bounded 模式记录误差上界；超限显式诊断 | 缓解中 |
| R-I08 | ICC/OCIO/CICP 冲突被静默消化 | 颜色错误 | 中 | spec §7 优先级冻结；冲突硬错误 TC_IMG_ERR_METADATA_CONFLICT | 缓解中 |
| R-I09 | metadata bomb（巨大 XMP/EXIF/深层嵌套） | OOM/DoS | 中 | §10 硬上限 + 解析预算；fuzz 覆盖 | 阶段 3 关闭 |
| R-I10 | 损坏文件触发大分配/死循环 | 崩溃/挂起 | 中 | checked arithmetic、硬上限、解析顺序冻结、fuzz+OOM 门禁 | **已关闭（阶段 1：ASan/UBSan/fuzz 4096 实测）** |
| R-I11 | 编码失败留下损坏/0 字节文件 | 用户数据损失 | 低 | 原子写（spec §12）；失败清理；probe-before-rename | 阶段 4 关闭 |
| R-I12 | 序列线程/内存失控（帧级×slice 级过订阅） | CPU/内存爆 | 中 | 复用 DecodeQueue 有界预算、generation cancel、backpressure | 阶段 6 关闭 |
| R-I13 | mmap 被误当零拷贝 | 尾延迟恶化（NAS/Windows） | 中 | 普通 I/O 为基线；mmap 实测收益后才启用 | 阶段 7 决策 |
| R-I14 | OIIO/宿主插件被宣传成自动兼容 | 用户期望落差 | 中 | 逐宿主构建与验收；文档明确加载条件 | 阶段 8 文档化 |
| R-I15 | 环境缺 OIIO/Nuke/PS/AE SDK | 插件阶段无法本机验收 | 高 | 阶段 8 交付可构建源+文档；9-11 独立立项 | 持续 |
| R-I16 | 性能数字脱离硬件/素材 | 不可信报告 | 中 | benchmark_protocol 冻结；p50/p95/p99 + 环境记录 | 持续 |
| R-I17 | 上游 codec 演进（V2/Micro-GOP）使 image golden 失配 | 图片层回归 | 低 | golden 回归进 run_tests.sh；file/codec 版本解耦 | 持续 |

## 关闭记录

- 2026-09-05：R-I01（阶段 5 集成测试）、R-I10（阶段 1/3 安全线）关闭；
- 2026-09-05（审计修订，ADR-I007）：premultiplied/alpha 存在性元数据断链
  （阶段 5 验收线）修复关闭——extra 键名对齐视频链、topos_*a{N} 注册名、
  alpha 平面声明位深满刻度交付；image_profile 错标（§9.1）现被 crosscheck
  拒绝；序列缺帧/重复/padding 策略语义冻结；"pytest 61" 口径纠正为
  image 专属 49；image pytest 与 topos_fuzz_image 纳入 run_tests.sh；
- 遗留开放项与豁免见 `stage_status.json` 的 deferred 列表（分发环境依赖：
  R-I13/R-I15 对应的 NAS/OIIO 验收、Windows 平台矩阵；应用层导出/缩略图/
  播放链接线）。

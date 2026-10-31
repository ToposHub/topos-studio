# RD7-04：MOV / TPIM 版本与能力同步

状态：**已实现并通过 native 回归**（2026-09-08）。

本项收口容器层的版本权威关系：

| 容器 | 权威字段 | 校验对象 | 不一致行为 |
| --- | --- | --- | --- |
| MOV | `tpcC.version` | 每个实际读取的 TPIC sample header `version_major` | `TC_ERR_MALFORMED` |
| TPIM | `IDSC.payload_major/minor` | 内嵌 PIXL 的 TPIC frame header | `TC_IMG_ERR_CHUNK_CONFLICT` |
| MOV sample entry | `TPIC` + `tpcC` 配置 | sample 的尺寸、格式、位深、Alpha、色彩和 SAR | `TC_ERR_MALFORMED` |

## 实现内容

- MOV `tpcC` 现在接受已实现的 TPIC V1–V7；V7 sample 按目录版本分别进入
  V7-A 或 V7-B 结构校验，不再误送入旧版 slice parser。
- V7-B sample 在 MOV 写入和读取校验时使用 base-only query：校验外层目录、base
  segment CRC、base header 和嵌入 packet，但不读取/解码 enhancement residual。
- `tc_movie_packet()` 在成功读入 sample 后校验完整 frame header；除了 major，还核对 `tpcC` 的几何、格式、位深、量化矩阵、Alpha、色彩标签和 SAR。
- `tc_movie_packet_batch()` 在单次 I/O 后逐 sample 执行同一校验，避免批量路径绕过一致性门禁。
- MOV `open` 仍保持 moov/index-only，不为打开一个大文件额外扫描所有媒体 sample；因此校验发生在 sample 被请求时，且不会从首帧推断轨级版本。
- TPIM IDSC 明确拒绝 `payload_major=0` 或未知版本，V1–V6 可被显式记录；已有 IDSC ↔ PIXL 逐字段交叉校验继续负责实际 payload 的精确匹配。

## 验收

```text
native/topos_codec/build/release/test_mov             PASS
native/topos_codec/build/release/test_image_container PASS
```

新增回归覆盖：

1. V4、V5、V6 packet 可写入 MOV，读取 `tpcC` major 并通过单帧/批量读取；
2. track=`V1`、sample=`V4` 的伪造混合流在两个 packet API 都稳定拒绝；
3. TPIM `payload_major=7` 被 IDSC parser 拒绝，V6 作为合法内层版本保留。

这项只保证 MOV 容器能承载并正确索引 V7-A/V7-B sample；V7-B 的默认 writer、最终
质量/速度门禁、capability rollout 和公开配置入口仍保持独立门控。

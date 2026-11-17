# E 阶段第六批（E7）：RAW 能力按真实后端报告 + 打开/探测有界

日期：2026-11-18 · 分支：fix-ui · 范围：`libav_player` 的 RAW 家族拒绝、
有界打开、有界首帧探测；toast 迟到计时器加固（E6 取消路径暴露的真
实缺陷）。

## 缺口（FUP-07 实测行）

8K ProRes RAW HQ 相机 MOV：metadata 打开 ~0.55s，首帧 RGB **35s+ 未返
回**，验收矩阵 40s deadline 超时——现状既不报能力也无超时，用户得到
的是无限等待/黑屏。"常规 ProRes 可播"被静默推广成了"ProRes RAW 可播"。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `libav_player.py` | ① `raw_capability(tag, name)`：按 codec tag/名识别 ProRes RAW（`aprh/aprn/apro`）、BRAW、R3D → **无解码后端，明确拒绝**；常规 ProRes（`apco/apcs/apcn/apch/ap4x`）不连坐。`_LibavSession` 打开时（解码前）检查，命中即抛 `UndecodableRawError`（原因经 `play_failed` toast 上抛，播放器隐藏——不回退系统播放器）。② `_av_open_with_timeout`（默认 5s）：daemon 线程有界打开，挂起源按超时失败收尾（有界泄漏纪律）。③ `_compute_mezzanine` 路由询问同样有界（超时→非 mezzanine→Qt 引擎，其 1.5s 开播看门狗兜底报错）。④ E4 的旋转首帧探测加 3s 预算：超时按未旋转收尾并告警（旋转是尽力而为元数据，不是播放前置） |
| `base_player.py` / `player_qt.py` | `_reset_title`/`reset_title` 容忍已销毁标签（RuntimeError）：toast 后 2.5s 内关窗时迟到计时器此前会打进 Qt 事件循环（E6 的"取消导出"toast + 关窗路径实测触发） |

## 判定依据

codec tag 与 FFmpeg 自身的解码器兼容判定同源——实验确认 ffmpeg 对
`-tag:v aprh` + 常规 prores 载荷**拒绝写头**（"Tag aprh incompatible
with output codec id"），即真实 ProRes RAW 文件必然携带本判定集合内
的 tag；伪造不出"tag 正常但载荷是 RAW"的误报源。

## 验证（`tests/ui/test_raw_capability_e7.py`，19 项全绿）

* **能力矩阵**（13 组参数化）：aprh/aprn/BRAW/R3D /prores_raw 拒绝且
  原因含"无解码后端/SDK"；常规 ProRes 四变体 + dnxhd + 未知 tag 放行；
* **会话层**：带 RAW tag 的会话在**打开阶段**拒绝（解码零调用——断言
  注入的 no-decode 哨兵未被触碰）；常规 ProRes（ffmpeg 合成 prores_ks）
  照常开帧；
* **播放层**：RAW 拒绝经 `play_failed` 如实上抛（消息含原因、播放器
  隐藏、不静默不回退）；
* **有界**：挂起的 `av.open`（sleep 30s 注入）在 5s 预算内 TimeoutError；
  路由询问超时 <1s 收尾；首帧永不返回的旋转探测 0.3s 预算返回未旋转。

回归：E3-E7 六套 + GPU YUV/topos audio/native host/topos audio container
—— 111 通过（含合跑复现并修复的跨用例计时器缺陷）；files UI 批 57 通
过（一次 FR-15 单发失败为仓库既有顺序抖动，隔离与合跑复跑均绿）。

## 诚实边界

- **SDK 未接入**：ProRes RAW/BRAW/R3D 依旧不可播——本批的交付是
  "按真实后端**报告**能力 + 快速明确拒绝"，不是解码支持；SDK 接入后
  从 `raw_capability` 白名单放行（单一改动点）。
- 真机 8K ProRes RAW 原文件未复测（本机无该素材；ffmpeg 合成 +
  tag 判定依据 + 注入挂起已覆盖行为面）。真机素材矩阵归 FUP-07 的
  实机验收补证。
- 首帧解码预算只覆盖旋转探测（打开路径）；播放中的逐帧解码无逐帧
  超时（解码线程卡死由 stop_decoder 的有界泄漏纪律兜底）。

## 隐私

ffmpeg lavfi/prores_ks 合成素材 + 注入式挂起；无真实相机素材、无隐私路径。

# AI 剪辑真实素材测试报告（2026-08-09）

## 测试范围

- Python：项目 `.venv/bin/python`（Python 3.12.13）
- 真实素材：`tests/test_videos/2K_h264.mp4`
- 本地分析：Qwen3-ASR 1.7B；词级对齐已启用；说话人分析为本次受控测试关闭
- 规划模型：当前活动 profile `profile-974c818e`，模型 `deepseek-v4-flash`
- 测试原则：只生成 preview 方案，不提交时间线，不保存 API key、完整 prompt、完整模型回包或绝对路径

## 结果

| 项目 | 实测结果 |
|---|---|
| 本地模型 pack | `qwen3_asr_large`、`qwen3_align` 已安装且校验通过 |
| ASR | 真实推理成功，1 段中文转录，42 个词级时间码 |
| ASR 置信度 | 0.95 |
| 证据时间范围 | 起始帧 204，持续 504 帧（24 fps） |
| DeepSeek 请求 | 真实请求成功，结构化 JSON 解析成功 |
| 规划方案 | 2 个：经典口播叙事、高能快切节奏 |
| Beat 覆盖 | 两个方案均为 3/3，覆盖率 1.0 |
| EditPlan | 两个 preview 计划均编译成功，每个包含 3 个 `add_clip` 操作 |
| 证据引用 | 两个方案均引用真实转录 segment；未发现未知或越界引用 |
| 总耗时 | 118.29 秒（含本地模型加载、ASR、对齐和 DeepSeek 请求） |
| 模型 usage | 7,564 tokens（prompt 503 / completion 7,061）；provider 未返回费用字段 |

## 脱敏校验信息

- 转录文本字符数：49
- 转录文本 SHA-256：`044bcdc2ca76e3caadacfb9a6ea12d043c265350a363b4ffe0456925c6effe42`
- DeepSeek request ID：已在本地运行日志中产生；本报告不保存完整请求回包
- API key：未读取到报告或日志输出

## 结论

“真实视频 → 本地真实 ASR/词级对齐 → AnalysisService 证据仓库 → DeepSeek V4 Flash → 结构化双方案 → EditPlan preview 编译”链路已在当前环境跑通。该结果证明 P1 的 DeepSeek 规划通道和 P4/P10 的真实证据闭环可用，但不等同于全部计划完成：真实图片/视频/音乐/TTS provider、宿主编码完成回调、并发性能、故障注入和 Alpha/Beta 运营数据仍需后续实测。

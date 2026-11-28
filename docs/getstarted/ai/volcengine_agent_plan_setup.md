# 火山方舟 Agent Plan 本地接入记录

更新时间：2026-08-12

本机的 Agent Plan 凭据引用为 `topos:volcengine-agent-plan`。真实 API Key 不写入仓库，也不写入新的用户配置 JSON，而是保存到 macOS Keychain；应用组合根使用钥匙串优先、旧文件兼容回退的凭据层。

## 已配置的用户级 profile

配置文件：`~/.topos/ai_profiles.json`（权限 0600）。

| 用途 | provider kind | 模型/Resource-Id | Base URL |
| --- | --- | --- | --- |
| 文本 | `volcengine_responses` | `doubao-seed-2.0-lite` | `https://ark.cn-beijing.volces.com/api/plan/v3` |
| 图片 | `volcengine_seedream` | `doubao-seedream-5.0-lite` | `https://ark.cn-beijing.volces.com/api/plan/v3` |
| 语音合成 | `volcengine_plan_tts` | `seed-tts-2.0` | `https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional` |

当前 active text profile 保持原有 DeepSeek 配置，避免静默改变已有工作流；需要测试方舟文本时，在 AI 偏好设置中切换到 `volcengine-agent-plan-text`。

## 实测状态

- Responses API：成功返回 `ARK_TEXT_OK`。
- Seedream 图片：成功生成并下载 2048×2048 PNG；Agent Plan 端点拒绝 1024×1024，适配器默认使用安全的 2K 方图下限。
- 豆包 TTS 2.0：成功生成有效 MP3；HTTP 返回的多行 JSON 音频分片已在适配器中合并并解码。
- Seedance 视频：Medium 套餐当前不支持视频配额；`doubao-seedance-1.5-pro` 已标记即将下线且不支持新增接入，因此不注册为可用 provider。

测试产物默认写到 `/tmp/topos-volcengine-smoke-20260812/`，应用生成素材缓存仍由既有生成物化器管理。

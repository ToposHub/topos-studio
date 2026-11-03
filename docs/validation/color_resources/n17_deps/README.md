# N17 留档：可选依赖诊断（2026-11-04）

## 实现

- `src/shared/diagnostics/optional_dependencies.py`：**单一探测源**——
  - `qt_multimedia`（Qt 运行时后端，importable 探测）；
  - `external_ffmpeg`（外部工具路径，ThumbEngine/CLI 用途；**缺失不影响
    Qt 播放/解码**——语义分离）；
  - `ai_model_packs`（ModelPack 注册表逐项 installed+verified）。
- `ai_model_available()`：未安装恒 False（**不得作为 AI 通过证据**，
  fail-closed）——R01 目录 `ai_model_missing` 与 R06 预检消费同一语义。
- `log_startup_summary()`：启动聚合**一条** INFO（替代散落 WARNING；
  summarize 不含探测细节，日志不泄露无关内容）。
- 全部探测可注入（无外部工具/模型环境可测）。

## 测试（tests/shared/test_optional_dependencies.py，4 项）

Qt 运行时 vs 外部工具语义分离；AI 门 fail-closed（未装/未知恒 False）；
摘要单行且不泄露 detail；真实探测在无外部工具环境不抛。
回归：catalog + 资源执行 27 passed；原生验证 passed（启动聚合日志生效）。

## 说明

浏览器目录在缺 FFmpeg/模型环境仍可浏览（条目按 R01/R06 码提示准确）；
实际调用（AI 处理/外部转码）以各自入口显式拒绝或使用已声明替代。

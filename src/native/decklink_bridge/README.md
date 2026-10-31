# DeckLink Bridge（macOS 监看输出桥）

`topos_decklink.cpp` 为 Topos 的 DeckLink 监看输出桥，为**本项目自有实现**
（相关构建/测试脚本见 `tools/build_topos_decklink.py` 等）。

## 构建依赖

Blackmagic **Desktop Video SDK**（专有许可，**不随本仓库分发**）：

1. 从 Blackmagic 官网下载 Desktop Video SDK 与驱动并安装；
2. 将 SDK 头文件路径提供给 `tools/build_topos_decklink.py` 进行构建。

未安装 DeckLink 硬件/SDK 时，不影响本产品其余功能的构建与运行。

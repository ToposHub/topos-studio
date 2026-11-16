# A03 / A10 平台矩阵（自动化）

命令：`PYTHONPATH=. .venv/bin/python docs/validation/files_native_p0/platform_check.py`（每个 DPI 独立进程）。

| QT_SCALE_FACTOR | 侧栏宽 | 信息栏宽 | 主要控件可达 | 浮动窗口 | 浮动面板 |
|---|---|---|---|---|---|
| 1 | 164 | 200 | 全部 | True · 700×500 | True |
| 1.5 | 164 | 200 | 全部 | True · 700×500 | True |
| 2 | 164 | 200 | 全部 | True · 700×473 | True |

- A03 路径身份由 `tests/ui/test_native_files_platform.py` 覆盖：NFC/NFD 同身份、大小写不敏感、`C:\`/UNC 归一、特殊字符 `# % 引号 空格` 的 QUrl 往返。
- 高 DPI 仅验证控件可达与不裁切；字体渲染粗细属平台行为，发布前按需人工复核。
- 浮动窗口为 QDockWidget 离屏尺寸 700×500，面板仍可见。
- 760px 下搜索框按既有设计收起，其余主控件保持可达（由同名测试验证）。
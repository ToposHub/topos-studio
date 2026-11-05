# N16 本机单进程回归（2026-11-05）

命令（同一工作树、两次连续执行）：

```bash
QT_QPA_PLATFORM=offscreen PYTHONFAULTHANDLER=1 \
  .venv/bin/python -m pytest tests/color tests/app tests/ui tests/shared -q
```

| 运行 | 结果 | 时间 | 日志 |
|---|---|---:|---|
| A | 10,465 passed, 20 skipped, 7 warnings | 471.05s | `topos-full-suite-gate-a-20261105.log` |
| B | 10,465 passed, 20 skipped, 7 warnings | 462.87s | `topos-full-suite-gate-b-20261105.log` |

两轮之前的一轮在约 54% 的 `test_color_node_host_menu.py` fixture 中发生
原生段错误，Python 栈位于 `ColorNodeView._cancel_wire()` 对尚无连线的
初始视图调用 `viewport().setCursor(...)`；原始日志为
`topos-full-suite-final-20261105.log`。修改后仅在实际连线被取消时恢复
光标，两轮通过。先前也曾在 `TimelineClipStrip._build_ui` 的
`installEventFilter` 附近偶发段错误，因此栈落点不能单独证明根因已消除。

本矩阵只包含 `tests/color tests/app tests/ui tests/shared`，未包含
`tests/edit`，也未覆盖 Linux/Windows 或其他硬件。跳过和警告没有计为通过。

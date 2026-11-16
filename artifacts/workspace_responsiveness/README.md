# 窗口响应对照（2026-11-16）

用同一台机器、同一脚本与真实 Qt 控件，每组 40 次窗口缩放测量主线程布局和绘制。
`QT_QPA_PLATFORM=offscreen`，不执行素材解码、GPU 呈现或系统窗口合成。

| 时间线片段数 | 修改前中位数 | 修改后中位数 | 修改前 P95 | 修改后 P95 |
| --- | ---: | ---: | ---: | ---: |
| 200 | 36.456 ms | 23.271 ms | 44.133 ms | 27.760 ms |
| 2,000 | 108.867 ms | 54.157 ms | 176.963 ms | 73.033 ms |

基线及最终结果见 `resize_before.json`、`resize_after.json`。共享机器的负载可能波动；
这是本次局部优化的单机对照，不代表播放帧率或所有工程的交互延迟。
Mixer 的 8 轨结果作为现状记录，未对其缩放路径作性能改动。

复现：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/benchmark_workspace_responsiveness.py --output /tmp/workspace-resize.json
```

页面切换另由 `tests/app/test_page_controller_coverage.py` 的真实回调队列守卫验证：
Library → Deliver → Edit → Library 快速连续切换时，渲染请求由 6 次减为 2 次，
文件区刷新由 3 次减为 1 次，过期 Deliver 布局迁移由 1 次减为 0 次，布局快照
由 6 次减为 3 次。正常单次访问仍保留两阶段视口重绘。

`gpu_probe.json` 记录独立硬件验证：真实 Qt cocoa OpenGL 4.1 上下文、GPU 渲染
线程与 VT 源，帧 0/40/88/89/88 均成功发布，无错误或泄漏警告。该测试只使用仓库
视频和独立上下文，不代表已经复现用户原始工程中的 `glUseProgram 1282` 故障。

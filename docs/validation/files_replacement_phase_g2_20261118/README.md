# G 阶段第二批（G2）：image_factory 接管——批处理 command 与回库

日期：2026-11-18 · 分支：fix-ui · 范围：`image_factory_service.py`（新）+
`image_factory_window.py`（新）+ 控制器原生命令表扩展。G 节行验收：
**色彩/HDR、逐项结果、取消/关闭，生成结果同一索引入口回库**。

## 新入口 / 新服务 / 关闭路径

| 部件 | 路径 |
| --- | --- |
| 新入口 | `launch("files.image_factory")` → `_launch_native`（复用 G1 三段 + F4 原子边界）→ `_open_image_factory`（F5 singleton）→ `ImageFactoryWindow`（真实选中数组全部项） |
| 新服务 | `ImageFactoryService`：`build_plan`（InputSnapshot 复用 G1；冲突政策 rename/skip/overwrite + 执行期 rename 解析）；`execute` **Pillow 同进程**（builtin 可信，F10 门禁放行）——**ICC 配置文件逐项透传**、**16-bit PNG（I;16）位深保留**（不压 8-bit）、长边减半预设；逐项回调 `on_item`（进度/成败）；**项间协作取消**（threading.Event）+ 整批 deadline 兜底；`BatchResult` 携带 source→output 映射 |
| 回库 | 窗口 `outputs_completed([{"source","output"}])` → 控制器 `_image_outputs_to_library`：**同一刷新入口**（`refresh`）+ **同一重扫**（`transport.start_scan`）——不开第二套索引 |
| 关闭路径 | 窗口 `closeEvent` → `service.cancel()`（项间生效，部分结果如实上报）→ F5 注册表销账；控制器 shutdown 同款 |

## 验证

- `tests/ui/test_image_factory_g2.py`（9 项）：真实选中数组计划（4 项全
  量 + 快照冻结）；冲突政策矩阵（rename -1/skip 显式/未知拒绝）；
  **ICC 透传**（写出 profile 可被 ImageCmsProfile 解析）；**16-bit 位深
  保留**（I;16 → I;16，长边减半尺寸正确）；逐项结果部分失败（坏文件
  如实 fail、好文件交付、无 .part 残留）；**取消**（30 项第 5 项后取消
  → 5 完成/25 如实"已取消"）；关闭即取消；快照漂移拒绝；
  窗口 outputs_completed 携带映射（qtbot 事件队列消费）。
- `tests/ui/test_native_files_plugins.py` +1：控制器回库端到端（同一
  start_scan 入口 + 提示"回库"）；F2 线上测试改用 decode_benchmark
  （image_factory 现零 HTTP——接管证明）。
- 回归：plugins + files + G1 + native UI —— **798 通过**。

## 诚实边界

- HDR 交付以位深保真为口径（16-bit 读写）；Display P3/Rec2020 广色域
  依赖 ICC 透传，未做像素级色域转换（输入即所得）。
- 取消为项间协作（单张 Pillow 操作不可中断——毫秒级，可接受）；
  逐项进度是完成计数（无 ETA）。
- 回库链路的"索引可见"依赖 start_scan 的既有扫描语义（合成环境断言
  调用发生与提示，真实库内可见性归实机验收）。
- WebP/JPEG 输出对 16-bit 源会按 Pillow 规则量化（预设标题未承诺位深
  ——选 PNG 预设保位深，标题明示"保留位深"）。

## 隐私

Pillow 内存合成图（含 sRGB profile）；无隐私路径。

# G 阶段第一批（G1）：format_factory 接管——独立 Qt 工具 command/window

日期：2026-11-18 · 分支：fix-ui · 范围：`format_factory_service.py`（新，
安全转码/封装服务 + 原生命令服务）+ `format_factory_window.py`（新，独立
工具窗口）+ 控制器原生路径接线。G 节行验收：**输入快照、输出计划/冲突、
进度/取消/失败恢复；不走旧 /launch**。

## 新入口 / 新服务 / 关闭路径（逐行记录）

| 部件 | 路径 |
| --- | --- |
| 新入口 | Files 选择 → 控制器 `launch("files.format_factory")` → `_launch_native`（prepare 冻结→revalidate worker→dispatch GUI，F4 锁内提交）→ `_open_format_factory`（F5 `WindowRegistry` singleton：二次打开聚焦并刷新输入快照）→ `FormatFactoryWindow` |
| 新服务 | `FormatFactoryService`：`build_plan`（InputSnapshot 冻结 path/size/mtime；输出计划逐项冲突标注；政策 rename/skip/overwrite 显式）；`execute_item/retry_item`（复用 E6 `ClipExportJob`：deadline 预算/协作取消/有界输出/临时文件+原子改名；执行期快照再验 + 执行期 rename 解析）；`cancel/cancel_all`。`NativeToolCommandService`：F3/F4 同款三段命令（身份/取消/期限/库 revision + `DispatchAuthority` epoch 原子边界）——dispatch 是 Qt 线程安全窗口打开，**零 HTTP** |
| 关闭路径 | 窗口 `closeEvent` → `service.cancel_all()`（在途 ffmpeg 终止，不留孤儿）→ F5 注册表 `close_window` 销账；控制器 `shutdown` → `windows.close_all("files.format_factory")` |

## 验证（tests/ui/test_format_factory_g1.py，11 项；ffmpeg 真 CLI）

- **输入快照**：mtime 漂移/文件消失即失配；执行期快照失配拒绝（不读
  非手势时的文件）；
- **输出计划/冲突**：rename 自动 -1/-2 且可见；skip/overwrite 目标不
  变、冲突显式标注；多输入计划；未知预设/政策 ValueError；
- **执行**：真封装转换原子交付（.part 无残留、ffprobe 可读、完成出账）；
  **取消**（6s 素材慢预设在途时 cancel_all → 失败收尾、无输出无 .part）；
  **失败恢复**（skip 撞预占 → 失败项；重试换 rename 政策 → 交付 -1 文件
  且预占未被覆盖）；
- **原生命令**：prepare 冻结 payload/三段成功打开；revision 漂移
  LIBRARY_REVISED；epoch 变更 AUTHORITY_EPOCH_MOVED 且**零打开**；身份
  错配拒绝。

控制器集成：`launch("files.format_factory")` 零 transport 调用（F2 线上
测试改用 image_factory 锁定 legacy 路由仍工作）；27 项插件 UI 回归绿。

回归：tests/plugins + tests/files + native 插件 UI —— **776 通过**
（一次 baseline_freeze 顺序抖动为仓库既有类，隔离绿）。

## 诚实边界

- 预设三种（copy-mp4/h264-mp4/prores-mov）——音频流复制假定兼容容器；
  不兼容封装的 -c copy 由 ffmpeg 如实失败（可重试换转码预设）。
- 窗口为功能完整但朴素的第一版（无逐项进度条差异/剩余时间估算——
  ClipExportJob 完成即出账，进度=完成计数）。
- 单实例聚焦后旧在途任务继续（不中断用户上次批次）——刷新输入快照
  只改下一批计划。
- 真 8K/长片转码的实机性能验收未做（本机 lavfi 合成素材）。

## 隐私

ffmpeg lavfi 合成素材 + 随机字节；无隐私路径。

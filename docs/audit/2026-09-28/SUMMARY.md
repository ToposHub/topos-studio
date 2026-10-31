# 计划01 执行摘要（FINAL）

## 完成内容

- P0：全量清单与 398 批次表（INVENTORY.md）。
- P1：P0 优先级 16/16 批次审查完成（fan-out 子智能体，每批独立报告）。
- P1.5：5 条 Critical 全部独立复核（verification/criticals-color-core.md、verification/criticals-gpu.md）。
- P2：ISSUES.md 台账 v1。
- P3 第一波（8 批）：app/controllers（color/composite/deliver）、app/main_window、edit/core 音频图/播放/混合/命令系统；新 Critical 5 条全部复核维持。

## 发现总览：371 条（复核后 Critical 8 / Major 134 / Suggestion 229）

## 复核后 Critical（3 条，建议最优先修复）

1. **AUD-0200** AI Bake 全链路 100% 失效：`_do_bake_layer` 引用未定义变量必然 NameError，被渲染主循环吞掉，`layer_baked_ready` 永不发射。
2. **AUD-0501** 撤销栈污染：历史快照与活动图层共享可变对象（`to_dict()` 返回活动引用），面板滑杆就地 `update()` 会污染栈内快照，undo/redo 静默失效。
3. **AUD-0600** 撤销后视口渲染旧图：undo/redo 恢复后 GPU 纹理链路无重新上传触发，破坏性操作撤销后视口持续显示旧图。
4. **AUD-16200/16201** Edit 命令系统 undo 重建模型丢字段：插入链接素材丢 12 个 TimelineSequence 字段（字幕轨、混音状态被清空）；转场三命令 5 处丢 13 个 EditTrack 字段（EQ、automation、sends 等）。
5. **AUD-16202/16203** Edit 命令系统 undo 逻辑错误：批量删除波纹 undo 回移全部失效致片段错位；覆盖剪辑 undo 误删同轨未受影响片段。
6. **AUD-2301** Color 控制器遮罩遍历崩溃：对无 `type` 属性的 MaskGroup 访问 `s.type`，Qt 直连槽内异常在 PyQt6 默认 qFatal 终止进程（共 3 处同型缺陷）。

## 复核降级（2 条）

- AUD-0310 CacheWarmer 死锁：缺陷成立但全库无生产调用方（死代码），当前零风险 → Major。
- AUD-1214 `_do_pixel_probe` 假阳性探测：缺陷成立但调用链双重休眠（死代码 + 上下文未注册）→ Major。

## 高价值 Major 主题（跨文件共性问题）

- **GPU 资源泄漏/无界累积**：ColorOutput 纹理替换不走 deferred-release（AUD-0201）、YUV 缓存按尺寸无限累积（AUD-0202）、comp_cache 无淘汰（AUD-0831）、降噪表面缓存键内嵌尺寸（AUD-0907）、Metal 路径无 autoreleasepool（AUD-1402）。
- **HDR 语义破坏**：legacy 路径 max()>1.0 启发式（AUD-0205）、AI 降噪 clip [0,1] 压平高光（AUD-0412）、blend mode 14 HSV 往返退化（AUD-0910）、Metal 位深校验缺上限（AUD-1510）。
- **导出链路**：HDR 导出 clamp+8bit 量化（AUD-0303）、JPEG 重编码 q75（AUD-0304）、ICC profile 静默退化 sRGB（AUD-0307）、LUT 导出 mixer/hue_shift 必然失败（AUD-0710）、导出后台实为 UI 同步（AUD-0711）、.3dl round-trip 断裂（AUD-0712）。
- **线程安全**：PluginManager 注册表无锁（AUD-0800）、present_frame lease 状态转移锁外执行（AUD-1601）、调度器 drain 快照竞态（AUD-1520）、stats 跨线程竞态（AUD-0903）。
- **状态/配置**：播放降级系统从未激活（batch-011 死代码）、预设保存异常吞掉后整体覆盖丢用户配置（AUD-1005）、embedded 模式丢 LUT 参数（AUD-0405）。

## 质量说明

- 所有发现均要求真实代码行号证据；复核环节剔除 2 处不成立的子论据（AUD-0501 的 canvas 路径、AUD-0600 的 mask_dirty 论断），0 条被推翻。

## 下一步

- 阶段 P3（199 批：edit/composite/deliver/app/products）与 P4（182 批：shared 其余+features 其余）待后续会话按 INVENTORY.md 批次表继续；断点续跑入口：PROGRESS.md。

---

## 最终数据（2026-09-29 全量完成）

- 398/398 批次完成，覆盖 src/ 全部 1,483 文件 / 835,840 行。
- 最终台账 **5,067 条**：Critical 133 / Major 1,492 / Suggestion 3,442（含 3 条复核降级：AUD-0310、AUD-1214、AUD-36100）。
- P5 随机抽查 32 条：推翻率 0%（验收线 <10%）。
- 详见 FINAL_REPORT.md 与 FINDINGS.json。

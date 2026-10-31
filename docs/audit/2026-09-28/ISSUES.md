# 计划01 审查问题台账（FINAL）

- 范围：src/ 全部 1483 个 .py 文件（398 批次全量，P0/P1/P2 优先级全部执行）
- 条目总数：**5067**（复核后 Critical 133 / Major 1492 / Suggestion 3442）
- 类型分布：{'bug': 1184, 'risk': 620, 'perf-opportunity': 130, 'process': 2930, 'doc-gap': 160, 'test-gap': 43}
- 复核状态：早期 10 条 Critical 全复核（8 维持 2 降级）；P5 随机抽查 32 条（31 成立、1 部分成立降级 AUD-36100、0 推翻，推翻率 0% < 10% 验收线）。
- 机器可读版本：FINDINGS.json（总纲第七节 schema）；证据见 batches/，复核见 verification/。

## 统计矩阵（目录 × 严重度，Top 30）

| 目录 | Critical | Major | Suggestion | 合计 |
|------|----------|-------|------------|------|
| `src/features/edit` | 30 | 390 | 884 | 1304 |
| `src/features/composite` | 38 | 293 | 702 | 1033 |
| `src/features/teaching_animation` | 10 | 138 | 323 | 471 |
| `src/features/color` | 9 | 110 | 245 | 364 |
| `src/shared/media` | 5 | 89 | 192 | 286 |
| `src/app/controllers` | 8 | 66 | 121 | 195 |
| `src/shared/video` | 7 | 56 | 103 | 166 |
| `src/shared/ui` | 7 | 38 | 102 | 147 |
| `src/features/ai_harness` | 1 | 40 | 133 | 174 |
| `src/features/ai_workflow` |  | 46 | 122 | 168 |
| `src/shared/export` | 3 | 22 | 57 | 82 |
| `src/shared/ai_models` | 1 | 31 | 41 | 73 |
| `src/app/ui` | 2 | 20 | 43 | 65 |
| `src/features/deliver` | 1 | 24 | 51 | 76 |
| `src/shared/project` | 1 | 22 | 41 | 64 |
| `src/features/scripting` | 3 | 4 | 11 | 18 |
| `src/features/shared` | 2 | 4 | 18 | 24 |
| `src/shared/color` | 1 | 12 | 17 | 30 |
| `src/shared/gpu` |  | 16 | 54 | 70 |
| `src/shared/io` | 1 | 5 | 9 | 15 |
| `src/shared/config` | 1 | 4 | 4 | 9 |
| `src/shared/codec` |  | 8 | 22 | 30 |
| `src/shared/utils` | 1 |  | 2 | 3 |
| `src/main.py:291（使用处）；src/main.py:104-106（导入处缺失）` | 1 |  |  | 1 |
| `src/shared/tracking` |  | 6 | 20 | 26 |
| `src/shared/effects` |  | 6 | 4 | 10 |
| `src/shared/diagnostics` |  | 4 | 6 | 10 |
| `src/shared/assets` |  | 3 | 6 | 9 |
| `src/shared/layout` |  | 3 | 6 | 9 |
| `src/shared/db` |  | 3 | 5 | 8 |

## 台账（全量，按编号）

| 编号 | 严重度 | 类型 | 位置 | 置信度 | 标题 |
|------|--------|------|------|--------|------|
| AUD-0200 | Critical | bug | `src/features/color/core/async_renderer.py:6428` | medium | `_do_bake_layer` 引用未定义变量 `render_request_id`，AI Bake 每次必然抛 NameError |
| AUD-0201 | Major | bug | `src/features/color/core/async_renderer.py:5797-5806` | medium | `_ensure_color_output_fbo` 尺寸变化时立即释放仍在被 Composite/monitor 消费的 ColorOutput 纹理，未走 deferred-release |
| AUD-0202 | Major | risk | `src/features/color/core/async_renderer.py:1255-1280` | medium | `_yuv_rgb_output_cache` 按尺寸无限累积纹理，窗口多次缩放造成 GPU 内存持续增长 |
| AUD-0203 | Major | bug | `src/features/color/core/async_renderer.py:3299-3309（源分辨率路径，3432-3435 视口路径同样模式）` | medium | matte 重生成异常时缓存悬挂：旧 matte 已归还纹理池，但 `matte_cache` 仍保留可"命中"的旧条目 |
| AUD-0204 | Major | risk | `src/features/color/core/async_renderer.py:2917、1757-1777` | medium | `_last_scope_render_params` 浅拷贝长期持有 UI 侧可变 layer DTO，GPU scope lane 稍后在渲染线程直接复用 |
| AUD-0205 | Major | bug | `src/features/color/core/async_renderer.py:5192-5197` | medium | legacy 裸帧路径用 `max() > 1.0` 启发式猜尺度，float32 HDR 帧会被整帧错误除以 255 |
| AUD-0206 | Suggestion | perf-opportunity | `src/features/color/core/async_renderer.py:812-821（3347/3443/3518/3604/3640 等消费点）` | medium | `load_prog` 吞掉着色器编译失败，后续代码用 `self.utility_shaders['keyer']` 等无守卫下标，失败后每帧 KeyError |
| AUD-0207 | Suggestion | process | `src/features/color/core/async_renderer.py:4104-4109` | high | `_do_update_aux_tex` lut3d 上传失败时保留旧 LUT 纹理，GPU 状态与模型不一致 |
| AUD-0208 | Suggestion | risk | `src/features/color/core/async_renderer.py:6529-6547、6592-6626` | medium | `_do_upload_baked_texture` / `_do_upload_ai_plugin_result` 缺形状校验，写入失败时新建纹理泄漏 |
| AUD-0209 | Suggestion | process | `src/features/color/core/async_renderer.py:1108-1113、1130-1135、6008-6011` | medium | 池属 matte 纹理在 `_do_remove_layer`/`_do_clear_aux_tex`/`_cleanup` 中被直接 `.release()`，绕过 `fb_manager` 池记账 |
| AUD-0210 | Suggestion | process | `src/features/color/core/async_renderer.py:4179-4181、5881-5884` | high | 多处裸 `except:` 捕获（含 SystemExit/KeyboardInterrupt） |
| AUD-0211 | Suggestion | risk | `src/features/color/core/async_renderer.py:429-437` | medium | `run()` 早退/初始化异常路径不执行 `_cleanup()`，泄漏离屏 surface 与共享上下文 |
| AUD-0212 | Suggestion | risk | `src/features/color/core/async_renderer.py:5229-5231` | high | `_do_render_video_frame` 在复制前直接改写调用方持有的 `render_params` 字典（跨线程写 UI 侧对象） |
| AUD-0213 | Suggestion | process | `src/features/color/core/async_renderer.py:6206-6213` | high | `_cleanup` 尾部 fbo_pair/tex_pair/pyramid/reference 释放无 try 包裹，单个失败中断剩余清理与 doneCurrent |
| AUD-0214 | Suggestion | process | `src/features/color/core/async_renderer.py（示例：` | high | 类型注解缺失（仅记录） |
| AUD-0300 | Suggestion | process | `src/features/color/core/auto_color.py:171` | medium | 对比度反解在 hi 精确等于 0.18 时发生除零，产生 numpy RuntimeWarning |
| AUD-0301 | Suggestion | risk | `src/features/color/core/color_output.py:62` | medium | update() 四字段赋值非原子，跨线程直接读属性存在撕裂读可能 |
| AUD-0302 | Suggestion | process | `src/features/color/core/color_output.py:86` | low | compute_proxy_size 缺少零尺寸/极端长宽比防御，int 截断可产生 0 维边 |
| AUD-0303 | Major | bug | `src/features/color/core/color_manager.py:460` | medium | _convert_via_lcms 将 HDR float 图像 clamp 到 0..1 并经 8bit 中转；uint16 输入路径静默失败 |
| AUD-0304 | Major | bug | `src/features/color/core/color_manager.py:365` | high | embed_icc 以默认参数重新编码 JPEG（quality 75），二次有损压缩且丢失 EXIF 等元数据 |
| AUD-0305 | Major | bug | `src/features/color/core/color_manager.py:424` | medium | apply_defaults_to_new_image 的 float 分支把 float16/float64 输入误走 /255 整数缩放路径 |
| AUD-0306 | Major | risk | `src/features/color/core/color_manager.py:123` | medium | ColorManager 单例 __new__ 无锁，并发首次实例化可产生双实例并重复执行昂贵初始化 |
| AUD-0307 | Major | bug | `src/features/color/core/color_manager.py:489` | medium | _get_pil_profile 不查 bundled profiles 目录，与 get_profile_for_space 的三级查找不一致，目标空间静默退化为 sRGB |
| AUD-0308 | Suggestion | process | `src/features/color/core/color_manager.py:170` | medium | _scan_system_profiles 的 os.listdir 无异常保护，目录列举失败会中断构造 |
| AUD-0309 | Suggestion | process | `src/features/color/core/color_manager.py:218` | medium | 多处宽泛 `except Exception: pass` 静默吞错且无日志 |
| AUD-0310 | Major↓ | risk | `src/features/color/core/cache_manager.py:1425` | medium | CacheWarmer.start_warmup 在持有不可重入锁时调用 stop_warmup，必然死锁 |
| AUD-0311 | Major | bug | `src/features/color/core/cache_manager.py:1605` | medium | 内存压力监控在内存"正常"时也每 5 秒清空缩略图缓存 |
| AUD-0312 | Major | risk | `src/features/color/core/cache_manager.py:1594` | medium | `import psutil` 位于 try 块之外，psutil 缺失时监控线程直接崩溃，try 内 `except ImportError` 是死分支 |
| AUD-0313 | Major | bug | `src/features/color/core/cache_manager.py:357` | medium | CacheConfig.max_memory_mb 定义但全模块未实现任何字节容量控制，缓存内存实际上限仅由"条目数 × 单帧大小"决定 |
| AUD-0314 | Major | bug | `src/features/color/core/cache_manager.py:455` | medium | LRUCache 字节统计系统性错误：覆盖同 key 不减旧值、evict 减的是"当前插入条目"的字节数、remove/invalidate_range 完全不减 |
| AUD-0315 | Major | bug | `src/features/color/core/cache_manager.py:809` | medium | GPUTextureCache 淘汰/清空只丢 Python 引用，无 release 或 deferred-release 回调，违背项目 GPU 资源释放架构 |
| AUD-0316 | Suggestion | process | `src/features/color/core/cache_manager.py:1141` | high | CacheManager._lock 定义后从未使用，复合操作（clear_all/update_config）非原子 |
| AUD-0317 | Suggestion | process | `src/features/color/core/cache_manager.py:1236` | medium | update_config/reset_stats 直接穿透访问子缓存私有 `_cache` 属性 |
| AUD-0318 | Suggestion | process | `src/features/color/core/cache_manager.py:738` | medium | RenderPlanCache 的 get/put 从不传质量参数，RenderPlanCacheKey.quality 恒为默认 "preview"，不同质量层级的渲染计划互相覆盖 |
| AUD-0319 | Suggestion | risk | `src/features/color/core/cache_manager.py:1560` | high | MemoryPressureMonitor.start/stop 的 _is_running 为无锁 check-then-act，可能双线程启动 |
| AUD-03900 | Critical | bug | `src/app/ui/preferences_dialog.py:2033-2050（辅证 :1386-1395、:1628-1639）` | medium | 打开偏好设置后直接 Apply/Save 会把 file_explorer 的 disabled_plugins 清空（用户插件停用状态丢失） |
| AUD-03901 | Major | risk | `src/app/ui/preferences_dialog.py:341-364` | medium | `_restore_preview_snapshot` 程序化更新控件未加状态锁，Cancel 时每个外观项双重发射预览信号 |
| AUD-03902 | Major | bug | `src/app/ui/preferences_dialog.py:2107-2258（缺失项对照 :804、:1199、:1966、:1984）` | medium | Restore Defaults 漏掉 `pass_fusion_mode` 与 `render_cache_auto_threshold`，恢复默认后这两项仍保留用户值 |
| AUD-03903 | Major | bug | `src/app/ui/preferences_dialog.py:2098-2105（对照 :2042-2049、:2053-2057）` | medium | Cancel 回滚不完整：Apply 已直写的 FE config 与模块页副作用无法撤销 |
| AUD-03904 | Major | risk | `src/app/ui/preferences_dialog.py:2102-2103` | medium | Cancel 直接整体替换 `config_manager.config`，绕过 ConfigManager 的 `_file_lock`，与并发 `set()`/`save_config()` 存在竞态 |
| AUD-03905 | Major | bug | `src/app/ui/preferences_dialog.py:867-885（` | medium | Timeline 分辨率预设下拉从不与配置同步：打开即显示 "4K UHD"，实际宽度/高度可能是 1920x1080 |
| AUD-03906 | Major | bug | `src/app/ui/preferences_dialog.py:1899-1903、:2053-2057、:2250-2254` | medium | 模块偏好页 load/apply/restore_defaults 委托全部 `except Exception: pass`，静默吞掉模块页持久化失败 |
| AUD-03907 | Major | risk | `src/app/ui/preferences_dialog.py:1853、:1879（同族 :1771-1800、:1827、:1843-1849）` | medium | 外部 config 数值未做类型验证即 `int()`/`setValue()`，手工编辑或损坏的 config.json 会使对话框构造即崩溃 |
| AUD-03908 | Suggestion | risk | `src/app/ui/preferences_dialog.py:1693-1733（触发点 :1914-1917）` | medium | 缓存体积统计在 UI 线程同步扫盘且只统计顶层文件：切页即卡顿、数值偏低 |
| AUD-03909 | Suggestion | process | `src/app/ui/preferences_dialog.py:2066-2071（对照 :2073-2096）` | medium | `_on_apply` 清 VT capability 缓存而 `_on_save` 不清，两个提交路径行为不对称 |
| AUD-03910 | Suggestion | process | `src/app/ui/preferences_dialog.py:1477-1488` | high | `_reset_all_shortcuts` 在对话框内立即破坏性提交，游离于 Apply/Cancel 模型之外 |
| AUD-03911 | Suggestion | process | `src/app/ui/preferences_dialog.py:2260-2267（触发条件 :2077-2095）` | medium | 重启提示文案固定为"语言变更"，但 work_mode / developer_mode 变更也走同一提示 |
| AUD-03912 | Suggestion | risk | `src/app/ui/preferences_dialog.py:1548-1568` | high | 诊断导出在 UI 线程同步 `import torch` / `import onnxruntime`，重依赖导入可阻塞界面数秒 |
| AUD-03913 | Suggestion | process | `src/app/ui/preferences_dialog.py:1737-1748（消费示例 :1962-1964）` | medium | `_select_combo_by_data` 未命中时静默选中第 0 项，未知配置值在下次 Apply 时被无声改写 |
| AUD-03914 | Suggestion | process | `src/app/ui/preferences_dialog.py:2121、:2257` | high | `_on_restore_defaults` 的 `_preview_blocked` 置位/复位无 try/finally 保护 |
| AUD-03915 | Suggestion | process | `src/app/ui/preferences_dialog.py:114、:1609、:974、:2281` | medium | 少数方法/参数缺少类型注解 |
| AUD-0401 | Suggestion | risk | `src/features/color/core/color_session.py:513,532` | medium | ColorSessionManager._session_history 只增不减，长期会话内存缓慢泄漏 |
| AUD-0402 | Suggestion | process | `src/features/color/core/color_session.py:424-438` | medium | from_dict/load_from_path 对损坏工程数据抛裸 ValueError，未转领域错误 |
| AUD-0403 | Suggestion | doc-gap | `src/features/color/core/color_session.py:371-378` | medium | get_save_target_region 三个分支返回完全相同字符串，与文档表格承诺不符 |
| AUD-0404 | Suggestion | process | `src/features/color/core/color_session.py:276-307` | high | from_page_context 入参 context: Any 弱类型，帧字段 getattr 与 clip/asset 字段直接属性访问混用 |
| AUD-0405 | Major | bug | `src/features/color/core/color_stack_render_service.py:125-131（对照 :94-95）` | medium | embedded 模式丢弃 lut_path/lut_intensity，嵌入栈的 3D LUT 在 Composite 渲染中静默丢失 |
| AUD-0406 | Suggestion | process | `src/features/color/core/color_stack_render_service.py:113-115` | medium | preset 模式数据源 project.color_presets 全库无填充方，且只提取 grading_params |
| AUD-0407 | Suggestion | process | `src/features/color/core/color_stack_render_service.py:88-96,113-115,125-131` | high | 三种 stack_source 返回 dict 形状不一致（8 键 / 1 键 / 5 键），契约脆弱 |
| AUD-0408 | Suggestion | process | `src/features/color/core/composition_uniforms.py:488-495` | medium | upload_composition_uniforms 逐 uniform 宽泛 except Exception 且只记 debug 日志 |
| AUD-0409 | Suggestion | process | `src/features/color/core/composition_uniforms.py:362,398（对照 :376` | medium | u_opacity/u_lut_intensity 用直接 float() 转换，显式 None 时抛 TypeError，与全文件 `or` 惯用法不一致 |
| AUD-0410 | Suggestion | process | `src/features/color/core/delivery_matrix.py:171-180` | medium | _changed 对 None/不可转值按"已修改"处理，交付报告可能误报操作启用 |
| AUD-0411 | Major | risk | `src/features/color/core/denoise_engine.py:84-85,165-168` | medium | 类级共享 _session_cache/_current_model_path：denoise 与 deblur 双引擎交替使用时互清缓存，模型每次从磁盘重载，且跨 QThread 无锁 |
| AUD-0412 | Major | bug | `src/features/color/core/denoise_engine.py:180-181,205-213` | medium | AI 降噪路径对 HDR 输入强制 clip [0,1]，>1.0 高光在结果中永久压平（Basic 路径明确支持 HDR，行为不一致） |
| AUD-0413 | Suggestion | process | `src/features/color/core/denoise_engine.py:645-658（对照 _run_classic_split_denoise:106-146 无任何 isInterruptionRequested）` | medium | cancel() 兜底 worker.terminate() 有进程级未定义行为风险，且 Basic 路径完全无协作取消检查点 |
| AUD-0414 | Suggestion | process | `src/features/color/core/denoise_engine.py:599-601` | high | apply_denoise 原地改写调用方 params dict（model_path 相对路径→绝对路径） |
| AUD-0415 | Suggestion | risk | `src/features/color/core/denoise_engine.py:359-367` | high | _load_with_timeout 超时后悬挂的加载线程永不回收 |
| AUD-0416 | Suggestion | process | `src/features/color/core/denoise_engine.py:450-451（对照 color_controller.py:1541-1546）` | medium | 用户取消经 error 信号通道上报，UI 会弹"严重错误"对话框 |
| AUD-0417 | Suggestion | process | `src/features/color/core/denoise_engine.py:87-92,581,610` | high | 多处公开方法与 Worker 方法缺类型注解 |
| AUD-0418 | Suggestion | perf-opportunity | `src/features/color/core/export_denoise_bridge.py:136-140` | high | process_frame 每帧创建并释放 FBO，长序列 4K 导出存在分配churn |
| AUD-0419 | Suggestion | process | `src/features/color/core/export_denoise_bridge.py:88-93` | high | VideoDenoiseParams.from_dict 解析失败完全静默直通，导出成片无降噪且日志无痕 |
| AUD-0501 | Critical | bug | `src/features/color/core/history.py:136` | medium | 历史快照与活动图层共享可变对象，undo/redo 静默失效 |
| AUD-0502 | Major | bug | `src/features/color/core/history.py:77` | medium | flip_h/flip_v 只在 push_state 记录，undo()/redo() 的对侧栈状态不含翻转位，恢复路径也不回读 |
| AUD-0503 | Suggestion | process | `src/features/color/core/history.py:45` | medium | copy_image=False 的引用共享依赖"源图像永不就地变更"的隐式约定 |
| AUD-0504 | Suggestion | process | `src/features/color/core/history.py:76` | high | current_image 为 None 时 undo/redo 仍弹出条目但不写对侧栈，该编辑层历史不可达 |
| AUD-0505 | Suggestion | process | `src/features/color/core/history.py:16` | high | 类型注解缺失与隐式 Optional |
| AUD-0510 | Major | bug | `src/features/color/core/grade_clipboard.py:197` | medium | 粘贴时同一 patch dict 对象复用到多个目标图层，目标间共享可变嵌套结构 |
| AUD-0511 | Major | bug | `src/features/color/core/grade_clipboard.py:142` | medium | copy_grade 走 to_dict(None)，BITMAP 蒙版的 bitmap_data 被静默丢弃 |
| AUD-0512 | Suggestion | process | `src/features/color/core/grade_clipboard.py:145` | medium | lut_intensity 恒被保留导致无 LUT 源也把 "lut" 计入 copied_categories，粘贴预览误报 |
| AUD-0513 | Suggestion | process | `src/features/color/core/grade_clipboard.py:213` | high | describe() 绕过 i18n 辅助函数直用硬编码标签 |
| AUD-0520 | Major | risk | `src/features/color/core/grade_keyframes.py:84` | medium | from_dict 不校验 value 的类型/分量数，畸形工程数据延迟到渲染/保存期才崩溃 |
| AUD-0521 | Suggestion | process | `src/features/color/core/grade_keyframes.py:189` | high | Track.from_dict 捕获 TypeError/ValueError 后静默 continue，无日志吞掉畸形关键帧 |
| AUD-0522 | Suggestion | risk | `src/features/color/core/grade_keyframes.py:115` | medium | UI 编辑与导出线程共享 tracks 无同步，append→sort 间隙可读到中间态 |
| AUD-0523 | Suggestion | process | `src/features/color/core/grade_keyframes.py:64` | high | 关键公共入口类型注解过宽 |
| AUD-0600 | Critical | bug | `src/features/color/core/image_engine.py:4194-4225` | medium | undo/redo 恢复图像后既不重新上传 GPU 纹理也不重置蒙版脏标记，视口继续渲染撤销前的旧图 |
| AUD-0601 | Major | bug | `src/features/color/core/image_engine.py:4230-4237（对照 set_image:691-699、rotate_image:4247）` | medium | resize_image 从不更新 img_full_original，全分辨率导出与坐标基准滞留旧尺寸 |
| AUD-0602 | Major | bug | `src/features/color/core/image_engine.py:4293-4357（对照 _apply_flip_state:4261-4275）` | medium | apply_crop 不更新 img_source_full，随后的 flip/reset_flip 用未裁剪源图重建，裁剪结果被静默丢弃 |
| AUD-0603 | Major | bug | `src/features/color/core/image_engine.py:4051-4061, 4091-4093` | medium | render_cached_image 临时替换 img_full_original 无 finally 恢复；嵌套事件循环期间被并发读取 |
| AUD-0604 | Major | bug | `src/features/color/core/image_engine.py:4055` | medium | render_cached_image 将 float 图像 `(img_high * 255).astype(np.uint8)` 无 clip，HDR 值按模 256 回绕 |
| AUD-0605 | Major | bug | `src/features/color/core/image_engine.py:4399-4404` | medium | bake_layer 全分辨率数据不分数值类型一律除以 255，uint16/float HDR 源烘焙结果数值错误 |
| AUD-0606 | Major | risk | `src/features/color/core/image_engine.py:1769-1779（对照 1799-1801、1848-1850）` | medium | request_sam_decode 跨线程信号发射可变列表（同一文件其他发射点都做了 .copy()） |
| AUD-0607 | Major | risk | `src/features/color/core/image_engine.py:3058, 3086-3091, 3163-3181, 3211, 3281-3378, 3404-3434, 3444-3463` | medium | _do_render_full_res 异常路径泄漏 GPU 纹理：3211 行之前创建的纹理未入 garbage 清单，mask/aux 纹理仅在正常路径释放 |
| AUD-0608 | Major | bug | `src/features/color/core/image_engine.py:4424-4454, 4642-4690, 4509-4523` | medium | bake 进行中执行 unbake 会被异步 EXR 保存完成信号"复活"，且复活后的 bake 无 digest 记录、永不自动失效 |
| AUD-0609 | Major | risk | `src/features/color/core/image_engine.py:69-90, 4705-4729` | medium | _layer_render_data / _create_layer_snapshot 把 ndarray（curves_lut、hsl_curve_lut、warper_params、ai_matte）按引用传给渲染线程，破坏"不可变 D |
| AUD-0610 | Major | bug | `src/features/color/core/image_engine.py:1027-1036（对照 set_image:697-712）` | medium | apply_changes 借道 set_image(is_new_file=True) 实现烘焙，触发"新文件"语义的一揽子副作用 |
| AUD-0611 | Major | bug | `src/features/color/core/image_engine.py:1953-1999（对照 set_image:682-683、_compute_prefix_digest:4492）` | medium | _on_raw_update_finished 重解码替换源图像内容但不递增 _source_epoch，bake 前缀失效校验漏掉该路径 |
| AUD-0612 | Suggestion | risk | `src/features/color/core/image_engine.py:1855-1882, 1943-1947` | medium | on_raw_params_changed 就地变异调用方 params dict 并将同一引用跨线程传给 RAW worker |
| AUD-0613 | Suggestion | process | `src/features/color/core/image_engine.py:445-456, 1896-1905, 2843-2852, 3113-3122` | high | cs_map/gamma_map 大字面量在 4 处重复定义且每次渲染调用都重建 |
| AUD-0614 | Suggestion | process | `src/features/color/core/image_engine.py:1075-1093（对照 240-246）` | high | remove_layer 不清理 ai_plugin_result_cache / _bake_prefix_digests / _pending_bake_digests 中的残留条目 |
| AUD-0615 | Suggestion | process | `src/features/color/core/image_engine.py:747-752, 3444-3447, 3450-3463, 3749-3751` | medium | 裸 except / 宽泛 except 吞领域错误 |
| AUD-0616 | Suggestion | process | `src/features/color/core/image_engine.py:3096, 3194-3206` | high | prog_raw / prog_pass 每次全分辨率渲染新建 program 且从不显式 release |
| AUD-0617 | Suggestion | process | `src/features/color/core/image_engine.py:3963-3973（render_full_image_for_scope:4012-4025 同模式）` | medium | scope 读回路径在拿不到 GL mutex 时仍读回并缓存黑帧 |
| AUD-0618 | Suggestion | process | `src/features/color/core/image_engine.py:2728, 4501-4533` | medium | _validate_baked_prefixes 在每次渲染（含 playback 帧）对每个 baked 层做全前缀 md5 摘要 |
| AUD-0619 | Suggestion | process | `src/features/color/core/image_engine.py:668, 805, 1027, 1063-1204, 1389, 1854, 2001, 2698（部分参数有默认值但无注解）等` | high | 大量公开方法缺少类型注解 |
| AUD-0700 | Major | bug | `src/features/color/core/layer.py:85（另见 :115 同型）` | medium | 位图缓存键使用 `id(bitmap_data)`，对象地址复用或原位修改导致陈旧蒙版渲染 |
| AUD-0701 | Major | bug | `src/features/color/core/layer.py:1141-1142（` | medium | 反序列化链路对外部工程数据无结构验证，畸形数据在加载时直接抛未捕获异常 |
| AUD-0702 | Major | bug | `src/features/color/core/layer.py:1148、:1194` | medium | PchipInterpolator 要求 xs 严格递增，重复 x 控制点在装有 scipy 时抛 ValueError，行为依赖运行环境 |
| AUD-0703 | Suggestion | process | `src/features/color/core/layer.py:744` | high | apply_grade_patch 写入的 `ai_matte_file` 属性全仓库无读者，属死状态 |
| AUD-0704 | Suggestion | doc-gap | `src/features/color/core/layer.py:516、:596、:652-653` | medium | reset_params 复位不彻底；copy_params_from 声称复制 film_grain_params 但该属性从不在 Layer 上初始化（死代码+误导文档） |
| AUD-0705 | Suggestion | process | `src/features/color/core/layer.py:938、:953、:967` | high | to_dict 多处直接引用活模型可变对象，序列化产物与模型别名 |
| AUD-0706 | Suggestion | process | `src/features/color/core/layer.py:177、:981` | medium | AI matte/bitmap 落盘 PNG 时 float→uint8 无 clip，超界值经 astype 回绕将静默损坏资产 |
| AUD-0707 | Suggestion | process | `src/features/color/core/layer.py:27（` | high | 公共方法普遍缺类型注解 |
| AUD-0708 | Suggestion | process | `src/features/color/core/layer.py:533-538、:674-755（apply_grade_patch）、:466-531（reset_params）` | medium | 图层模型变更入口无通知机制，undo/revision 同步完全依赖调用方自觉 |
| AUD-0710 | Major | bug | `src/features/color/core/lut_exporter.py:651、:655、:659、:663、:667、:671（对照已修复的静态版 :363-373）；调用路径 :533-535（hue_shift）、:790+:826（mixer）` | medium | `_hsv_to_rgb` 的 np.stack 混合数组与标量必抛 ValueError：mixer 激活或 hue_shift≠0 时 LUT 导出必然失败（已运行时复现） |
| AUD-0711 | Major | risk | `src/features/color/core/lut_exporter.py:1031（对照 :976-981 的 moveToThread 声明）` | medium | "后台 worker" 实为 UI 线程同步执行：线程空转、导出全程阻塞 UI |
| AUD-0712 | Major | bug | `src/features/color/core/lut_exporter.py:924-929（对照 src/shared/color/lut_loader.py:151-155）` | medium | 导出的 .3dl 首行写 TITLE，自家 `parse_3dl` 也无法读回（round-trip 断裂），且不符合 Autodesk .3dl 规范 |
| AUD-0713 | Major | bug | `src/features/color/core/lut_exporter.py:162-169（消费点 :437-441）` | medium | `_load_lut_file` 宽泛 except 静默吞掉 LUT 解析错误，已应用 LUT 损坏时导出悄悄丢失该效果 |
| AUD-0714 | Suggestion | risk | `src/features/color/core/lut_exporter.py:1022-1028` | high | export_lut 每次连接回调不断开，同实例重复导出将累积连接、回调重复触发 |
| AUD-0715 | Suggestion | process | `src/features/color/core/lut_exporter.py:1043、:1055、:1059` | high | `_extract_exportable_params` 对嵌套可变参数只做浅拷贝，与活图层共享内部 list/dict |
| AUD-0716 | Suggestion | process | `src/features/color/core/lut_exporter.py:898-904、:933-939` | medium | 文件写出为三重 Python 循环逐行 f.write（65 档 27 万行），且最终硬 clip 到 [0,1] 使 HDR 档位在导出中丢失 |
| AUD-0720 | Suggestion | process | `src/features/color/core/msl_composition.py:222-223（对照 :123-124 注释声明的固定顺序）` | medium | 效果发射顺序完全信任调用方传入的 active_effects 顺序，不按声明的 composition 顺序排序或校验 |
| AUD-0721 | Suggestion | process | `src/features/color/core/msl_composition.py:224-231（对照 :19-21 docstring 的等价性前提）` | medium | blend_modes 位未启用时 opacity/matte 完全不参与输出，opacity<1 的管线与 GL 基准分歧且无守卫 |
| AUD-0730 | Suggestion | process | `src/features/color/core/opengl_render_backend.py:123-149（对照 async_renderer.py:5937-5949 的登记实践）` | medium | facade 创建的真实 GL 资源不登记入 self.registry 台账，脱离 release/report 视图 |
| AUD-0731 | Suggestion | risk | `src/features/color/core/opengl_render_backend.py:108-117` | medium | release() 不做渲染线程守卫，registry.release_all 会在调用线程执行 release_fn，与 create_* 的严格守卫不对称 |
| AUD-0800 | Major | risk | `src/features/color/core/plugin_manager.py:50, 72-97` | medium | 共享插件注册表跨线程无锁读写：UI 线程 scan/pop 与渲染线程 get_plugin 并发 |
| AUD-0801 | Suggestion | process | `src/features/color/core/plugin_manager.py:307-321` | medium | _parse_params 数值转换无容错，单行坏参数导致整个插件被丢弃 |
| AUD-0802 | Suggestion | process | `src/features/color/core/plugin_manager.py:335-344` | medium | select 参数解析失败时产物缺少 default/options 键，下游静默回退索引 0 |
| AUD-0803 | Suggestion | process | `src/features/color/core/plugin_manager.py:85-93` | medium | .tglsl 与 .tai 同名（同 basename）时 .tai 静默覆盖 .tglsl 注册项 |
| AUD-0804 | Suggestion | process | `src/features/color/core/plugin_manager.py:146-152` | medium | 预览质量注册发生在参数解析之前，解析失败的插件在质量管理器留下残留注册 |
| AUD-0805 | Suggestion | process | `src/features/color/core/plugin_manager.py:72-97, 509-520` | high | scan_plugins 增量合并不清理已删除插件文件的陈旧条目，reload_plugins 全仓无调用方 |
| AUD-0806 | Suggestion | process | `src/features/color/core/plugin_manager.py:68-70` | high | 插件目录缺失时 scan 副作用建目录，且 makedirs 失败会未捕获抛出 |
| AUD-0810 | Suggestion | process | `src/features/color/core/preset_meta.py:110-118, 157-165` | medium | sidecar 写失败完全静默，模块无任何日志 |
| AUD-0811 | Suggestion | process | `src/features/color/core/preset_meta.py:138-145` | medium | _to_uint8_bgr 不处理 float16/uint16 输入，JPEG 编码将静默失败 |
| AUD-0812 | Suggestion | process | `src/features/color/core/preset_meta.py:148-154, 178-189` | medium | _resize_max 无异常保护，极端长宽比下 int() 归零使 cv2.error 逸出 write_still_images |
| AUD-0820 | Suggestion | risk | `src/features/color/core/project_dependencies.py:133-138, 76-87` | medium | DependencyReport.from_dict 不校验 entries 元素类型，损坏的外部 JSON 会以 AttributeError/ValueError 崩溃 |
| AUD-0821 | Suggestion | process | `src/features/color/core/project_dependencies.py:187-196` | high | _resolve_path 的存在性检查分支冗余，两个分支返回同一值 |
| AUD-0822 | Suggestion | process | `src/features/color/core/project_dependencies.py:260-267` | medium | 通过 getattr 访问 AIModelManager 私有 `_registry`，跨模块脆弱耦合 |
| AUD-0830 | Major | bug | `src/features/color/core/shader_compiler.py:253-258（对照 :84）` | medium | comp_cache 缓存键未包含插件内容哈希，插件文件运行期更新后渲染继续使用旧 program |
| AUD-0831 | Major | risk | `src/features/color/core/shader_compiler.py:133-134, 258-260, 403-404` | medium | comp_cache 无容量上限与淘汰策略，GL program 随结构组合切换无限累积 |
| AUD-0832 | Major | bug | `src/features/color/core/shader_compiler.py:306-313（对照 :320-323）` | medium | 插件源码正则改写是名字盲替换：@param 名与插件体内任意同名标识符冲突可致编译失败或语义破坏 |
| AUD-0833 | Suggestion | process | `src/features/color/core/shader_compiler.py:234-241, 300-313` | medium | binding.index 默认 0 且编译入口不校验唯一性，手工构造的 bindings 可产生重复 process_P0 |
| AUD-0834 | Suggestion | process | `src/features/color/core/shader_compiler.py:402-424` | medium | 编译失败不做负缓存，失败结构在渲染循环中反复重组装、重编译 |
| AUD-0835 | Suggestion | process | `src/features/color/core/shader_compiler.py:110-114（对照 :42-45）` | medium | plugin_bindings_from_instances 的非 dict 分支为死代码 |
| AUD-0836 | Suggestion | process | `src/features/color/core/shader_compiler.py:283` | high | 注释与事实不符：u_tex_size 实由 GRADING_SNIPPET 声明，而非 BLUR_SHARPEN_SNIPPET |
| AUD-0837 | Suggestion | process | `src/features/color/core/shader_compiler.py:132, 207-221, 426-430` | high | 类型注解缺失（Suggestion 级，仅记录） |
| AUD-0840 | Major | bug | `src/features/color/core/shader_nodes.py:168-172, 262-267（对照 MIXER :584-598、WARPER :628-646）` | medium | HSV 转换对负值 HDR 颜色的防护不一致：hue_shift 与 HSL 曲线直接对可能为负的 color 做 rgb2hsv |
| AUD-0841 | Suggestion | process | `src/features/color/core/shader_nodes.py:538, 543` | high | apply_blur_sharpen 的 USM 迭代循环边界为运行时值，违反项目"for 循环边界必须是常量"约定 |
| AUD-0842 | Suggestion | process | `src/features/color/core/shader_nodes.py:739-740` | high | FILM_GRAIN_SNIPPET 头部注释错误声称 u_tex_size 由 BLUR_SHARPEN_SNIPPET 声明 |
| AUD-0843 | Suggestion | process | `src/features/color/core/shader_nodes.py:266-267（同类用法 :72, :123, :152, :176, :460, :817）` | high | 亮度系数实为 BT.601 但注释声称 Rec.709，色度学口径不统一 |
| AUD-0900 | Major | risk | `src/features/color/core/video_denoise_gl.py:108-119` | medium | RenderSurfaceCache.get 创建失败后，已释放的旧表面残留缓存，可被后续复用（use-after-release） |
| AUD-0901 | Suggestion | process | `src/features/color/core/video_denoise_gl.py:104-105, src/features/color/core/video_denoise_gl.py:139-145` | medium | get() 忽略已缓存表面的 components/dtype 不匹配；total_bytes() 硬编码 comp=4/f2 |
| AUD-0902 | Suggestion | process | `src/features/color/core/video_denoise_gl.py:43-52` | medium | QuadRenderer 以 program.glo 作 VAO 缓存键，存在 GL 对象名复用的误命中风险 |
| AUD-0903 | Major | risk | `src/features/color/core/video_denoise_pipeline.py:56-64, src/features/color/core/video_denoise_pipeline.py:183, src/features/color/core/video_denoise_pipeline.py:196` | medium | stats 字典跨线程快照读取与 window_frames 动态键插入存在竞态（dict 迭代中 resize） |
| AUD-0904 | Suggestion | process | `src/features/color/core/video_denoise_pipeline.py:186-188` | high | process_window 失败路径不计错误、不熔断，与 process 的 _note_error 行为不一致 |
| AUD-0905 | Suggestion | risk | `src/features/color/core/video_denoise_pipeline.py:76-78` | medium | params property 返回内部可变对象，外部可绕过 sanitized/锁直接改参 |
| AUD-0906 | Suggestion | process | `src/features/color/core/video_denoise_shaders.py:266` | high | 上采样细节恢复着色器含 `residual * keep * 0.0` 死代码 |
| AUD-0907 | Major | risk | `src/features/color/core/video_spatial_denoise.py:86, src/features/color/core/video_spatial_denoise.py:111-112, src/features/color/core/video_spatial_denoise.py:157-160, src/features/color/core/video_spatial_denoise.py:214` | medium | 表面缓存键内嵌尺寸 + 缓存无淘汰策略，多分辨率场景 GPU 内存无上界累积 |
| AUD-0908 | Suggestion | process | `src/features/color/core/video_spatial_denoise.py:194, src/features/color/core/video_spatial_denoise.py:203` | medium | enhanced 上采样 pass 注释与代码不符（"固定 0.5" vs 实际传 p.detail_recovery） |
| AUD-0909 | Suggestion | process | `src/features/color/core/video_spatial_denoise.py:37, src/features/color/core/video_spatial_denoise.py:55, src/features/color/core/video_spatial_denoise.py:210` | medium | 类型注解不完整 |
| AUD-0910 | Major | bug | `src/features/color/core/shader_nodes_msl.py:709` | medium | blend mode 14（Color）是 HSV 恒等往返，退化为 Normal 混合，未保留 base 亮度 |
| AUD-0911 | Major | bug | `src/features/color/core/the_decompiler.py:263, src/features/color/core/the_decompiler.py:280` | medium | apply_soft_clip 分支门控与 GPU 语义背离：丢失两条 elif 路径 |
| AUD-0912 | Major | bug | `src/features/color/core/the_decompiler.py:182-192, src/features/color/core/the_decompiler.py:263-273` | medium | 前向模型缺失 GPU 管线的"暗部保护"与 toe RGB 平衡两个处理级 |
| AUD-0913 | Major | bug | `src/features/color/core/the_decompiler.py:227` | medium | hue_shift 前 `np.clip(res, 0, 1)`，GPU 两侧均不 clip，HDR/负值区域 CPU/GPU 分叉 |
| AUD-0914 | Suggestion | risk | `src/features/color/core/the_decompiler.py:350-362` | medium | decompile 无输入校验且重计算可能阻塞调用线程 |
| AUD-0915 | Suggestion | process | `src/features/color/core/the_decompiler.py:20-21, src/features/color/core/the_decompiler.py:341, src/features/color/core/the_decompiler.py:350` | high | 类型注解缺失（GradingMath/Decompiler 公共方法均无注解） |
| AUD-1000 | Major | bug | `src/features/color/core/video_temporal_denoise.py:82-90（配合 :52-57、:150-154、:187-191）` | medium | `force_reset()` 后同一 clip/generation/params 下状态机永久卡死在 delta==0 分支，时域降噪静默失效 |
| AUD-10001 | Major | risk | `src/features/composite/ui/keyframe_editor.py:821-837（触发点 641/651/612 的 valueChanged/currentIndexChanged 连接；重入落点 871-879、881-897）` | medium | 选中关键帧时程序化更新 spin/combo 无状态锁，重入处理器并发出冗余 keyframes_changed 提交 |
| AUD-10002 | Major | bug | `src/features/composite/ui/keyframe_editor.py:677-681（触发）；742-784（处理器）` | medium | set_parameter 的 setChecked 程序化触发 _on_animation_toggled，以"旧参数 track + 新参数名"的混合状态执行快照与提交 |
| AUD-10003 | Major | bug | `src/features/composite/ui/keyframe_editor.py:862-869（处理器）；637-638（spin 范围）；802-819（_on_keyframe_moved 无去重）；对比 304-328（时间线拖拽有 _find_free_frame）` | medium | 帧号 spinbox 移动关键帧：无同帧碰撞检查、无 total_frames 钳制、移动后选择状态不同步 |
| AUD-10004 | Suggestion | process | `src/features/composite/ui/keyframe_editor.py:528-535（曲线拖值）；262-274（时间线拖帧）；899-911（_emit_keyframes_changed）` | medium | 曲线/时间线拖拽期间每个 mouseMove 触发一次完整提交信号，无节流或"拖拽结束才提交"的批处理 |
| AUD-10005 | Suggestion | process | `src/features/composite/ui/keyframe_editor.py:16-27` | medium | 13 个未使用的导入 |
| AUD-10006 | Suggestion | process | `src/features/composite/ui/keyframe_editor.py:77、344、574（` | high | 多个方法缺少类型注解 |
| AUD-10007 | Major | bug | `src/features/composite/ui/keyframes_model.py:263-265、394-396、475-477、812-813、819-821、826-829、837-840、857-859、873-875、879-881、904-905` | medium | 11 处宽泛 `except Exception` 静默吞掉领域错误，违反"不得宽泛 except 掩盖领域错误"铁律 |
| AUD-10008 | Suggestion | process | `src/features/composite/ui/keyframes_model.py:495-498（visit 内）；488-507（遍历结构）；触发点 keyframes_panel.py:1483（面板每次刷新重建）` | medium | rebuild_layers 每层对 graph.connections 全量线性扫描，复杂度 O(层数×节点×连接) 且无缓存 |
| AUD-10009 | Suggestion | process | `src/features/composite/ui/keyframes_model.py:397-398（以` | medium | 层模式下 KeyframesTrackVisual.row_id 恒为 node: 前缀，与实际存储键 layer: 前缀不一致 |
| AUD-1001 | Major | bug | `src/features/color/core/video_temporal_denoise.py:405、:455、:460（对照契约 :301-311、自认 :469-471）` | medium | `_replay_window` 对邻帧降采样使用当前帧尺寸作为 `u_texel`，混合分辨率时运动估计标定错误 |
| AUD-10010 | Suggestion | process | `src/features/composite/ui/keyframes_model.py:25；179` | high | 未使用的导入 KeyframeTrack 与死常量 _PARAM_CHANNEL_COLORS |
| AUD-10011 | Suggestion | process | `src/features/composite/ui/keyframes_model.py:843（` | high | 个别函数缺少类型注解 |
| AUD-10012 | Suggestion | process | `src/features/composite/ui/import_report_panel.py:346` | high | update_report 内函数级 `import os`，应上移到模块顶部 |
| AUD-1002 | Suggestion | process | `src/features/color/core/video_temporal_denoise.py:231-235` | medium | 运动估计失败路径不推进历史纹理，历史与 luma_prev/注释语义失配（单帧拖影） |
| AUD-1003 | Suggestion | perf-opportunity | `src/features/color/core/video_temporal_denoise.py:268-277` | medium | 因果模式步骤 5 的历史推进是一次额外的全分辨率 f16 拷贝（4K 热路径每帧成本） |
| AUD-1004 | Suggestion | process | `src/features/color/core/video_temporal_denoise.py:193、:285、:313、:390-395、:415` | medium | 多个核心方法参数/返回值缺类型注解 |
| AUD-1005 | Major | bug | `src/features/color/core/workspace_presets.py:187-191（配合 :231、:213）` | medium | 配置读取异常被吞为空列表，随后任何保存都会整体覆盖写回，永久丢失全部用户预设 |
| AUD-1006 | Major | bug | `src/features/color/core/workspace_presets.py:212-215、:238-240` | medium | `_write_user_workspaces` 静默吞掉写入异常且无日志，`save_user_workspace` 仍返回新 preset 造成保存成功假象 |
| AUD-10100 | Major | bug | `src/features/composite/ui/keyframes_panel.py:548（另见 600、1479、1568、2107、2224）` | medium | 多处宽泛 `except Exception` 吞掉宿主回调与领域错误（铁律违规） |
| AUD-10101 | Major | bug | `src/features/composite/ui/keyframes_panel.py:2339` | medium | `set_playback_controller` 重复绑定/置空时不断开旧连接 |
| AUD-10102 | Major | perf-opportunity | `src/features/composite/ui/keyframes_panel.py:2392（另见 1540–1555、1684）` | medium | 播放期间每帧全量刷新 Value 列，provider 每次调用都重新反序列化整条关键帧轨道 |
| AUD-10103 | Suggestion | process | `src/features/composite/ui/keyframes_panel.py:1743（另见 1955；对照 2866–2871）` | medium | `_value_provider` 宿主回调两处调用无异常防护，与同文件守卫写法不一致 |
| AUD-10104 | Suggestion | process | `src/features/composite/ui/keyframes_panel.py:1922（另见 1926–1927）` | medium | `QTimer.singleShot` 携带 lambda，面板销毁后触发时访问已删除控件 |
| AUD-10105 | Suggestion | process | `src/features/composite/ui/keyframes_panel.py:636` | medium | `_on_graph_selection_changed` 直接改写 AnimationSelectionModel 私有 `_selection`，绕过公开 API |
| AUD-10106 | Suggestion | process | `src/features/composite/ui/keyframes_panel.py:1598` | medium | `_on_timeline_track_changed` 校验传入 keyframes 的 frame，却不校验节点既有（持久化）keyframes 的 frame |
| AUD-10107 | Suggestion | process | `src/features/composite/ui/keyframes_panel.py:1912（另见 1561–1562、1898）` | medium | 绑定反馈与 On/Off 等用户可见文案硬编码英文，绕过 I18N |
| AUD-10108 | Suggestion | process | `src/features/composite/ui/keyframes_panel.py:1785` | medium | 已动画参数的 no-op 值编辑仍走 `add_keyframe_at`，产生冗余 track_changed/undo 记录 |
| AUD-10109 | Suggestion | process | `src/features/composite/ui/keyframes_panel.py:607（另见 356、392、531、537、2749）` | medium | 多个公开/私有方法缺少参数或返回类型注解 |
| AUD-10110 | Suggestion | process | `src/features/composite/ui/keyframes_panel.py:653（另见 329、433、541–542、2001、2065、2980、3040）` | medium | 面板频繁读写兄弟视图/模型私有成员 |
| AUD-10200 | Major | bug | `src/features/composite/ui/keyframes_timeline_view.py:738-739, 1240-1256` | medium | 单击（无拖动）范围条即提交 range 编辑，把默认图层条固化为显式 time_range 并产生多余 undo 记录 |
| AUD-10201 | Major | bug | `src/features/composite/ui/keyframes_timeline_view.py:160-163` | high | dropEvent 用宽泛 `except Exception` 静默吞掉媒体拖放处理器的一切错误 |
| AUD-10202 | Major | risk | `src/features/composite/ui/keyframes_timeline_view.py:763-776, 1422-1427` | medium | 双击添加关键帧绕过行锁定（P9），且双击已有菱形会重复发射未变化的 track |
| AUD-10203 | Major | bug | `src/features/composite/ui/keyframes_timeline_view.py:1080-1082, 1106-1123, 1146-1148, 1268-1276` | medium | Alt+单击（未拖动）提交同帧重复关键帧，破坏"一轨一帧"不变量 |
| AUD-10204 | Major | bug | `src/features/composite/ui/keyframes_timeline_view.py:1546-1561, 1849-1866, 1354-1358` | medium | scale/interpolation 对隐藏行（折叠、过滤后不可见）先改模型后跳过提交，编辑静默丢失 |
| AUD-10205 | Suggestion | process | `src/features/composite/ui/keyframes_timeline_view.py:919-930` | medium | Left/Right 步进未钳到非负，frame 0 处按 Left 发出 current_frame_changed(-1) |
| AUD-10206 | Suggestion | process | `src/features/composite/ui/keyframes_timeline_view.py:664-666, 962-963, 740-749` | medium | 标尺普通单击会双次发射 current_frame_changed（press 一次 + release 强制一次） |
| AUD-10207 | Suggestion | process | `src/features/composite/ui/keyframes_timeline_view.py:1723-1725` | high | update_keyframe_at 不校验 interpolation 合法性 |
| AUD-10208 | Suggestion | process | `src/features/composite/ui/keyframes_timeline_view.py:1312-1340` | medium | work-area 拖拽每次 mouseMove 无节流发射 play_range_changed |
| AUD-10209 | Suggestion | process | `src/features/composite/ui/keyframes_timeline_view.py:122-124, 647, 709, 733, 1354-1358, 1360-1362, 1384-1386, 493-494` | medium | 多个公开/内部方法缺少类型注解 |
| AUD-10210 | Suggestion | process | `src/features/composite/ui/keyframes_timeline_view.py:707, 1054-1056, 1105, 1657, 1653/1689/1710, 317-319` | medium | 死代码、冗余导入与重复的 row_id 格式串 |
| AUD-10300 | Critical | risk | `src/features/composite/ui/keyframes_tree_view.py:1391-1409` | medium | mouseReleaseEvent 绑定单击路径引用未赋值局部变量，触发 UnboundLocalError 崩溃 |
| AUD-10301 | Major | bug | `src/features/composite/ui/keyframes_tree_view.py:755-758、1219-1224；对照 src/features/composite/ui/keyframes_model.py:135-137` | medium | Eye/Solo/Shy 列读写的行字段名与 KeyframesRow 模型不一致，开关图标状态永不反映真实值 |
| AUD-10302 | Major | bug | `src/features/composite/ui/keyframes_tree_view.py:1275-1276、1293-1294、1434-1435；对照 964-967` | medium | `_value_provider` 在交互路径未加异常防护（同一 provider 在绘制路径有防护） |
| AUD-10303 | Suggestion | process | `src/features/composite/ui/keyframes_tree_view.py:402-412` | high | 内联关键帧编辑器四个控件同连 commit，一次操作触发多次提交信号 |
| AUD-10304 | Suggestion | process | `src/features/composite/ui/keyframes_tree_view.py:1220-1223（典型）、501-504、645-648、964-967、999-1008、1035-1041、1068-1071、1652-1656` | medium | 多处宽泛 `except Exception` 静默吞错，且赋值失败后仍继续发信号 |
| AUD-10305 | Suggestion | risk | `src/features/composite/ui/keyframes_tree_view.py:530、550-552` | high | `_value_controls` 只增不减，DragValue 子控件随会话无界累积 |
| AUD-10306 | Suggestion | process | `src/features/composite/ui/keyframes_tree_view.py:1218-1226、1243-1247；对照绘制侧 754-757 的` | high | eye/lock 列点击命中判定覆盖 param 行，而图标只在 node/layer 行绘制 |
| AUD-10307 | Suggestion | process | `src/features/composite/ui/keyframes_tree_view.py:822-825、837-843` | high | `_draw_solo`/`_draw_shy` 存在不可达分支（死代码） |
| AUD-10308 | Suggestion | process | `src/features/composite/ui/keyframes_tree_view.py:215、223、233、243、248、253、1549（` | medium | 类型注解缺失：provider 安装器、事件参数与 row 参数均无类型 |
| AUD-10309 | Suggestion | risk | `src/features/composite/ui/material_panel.py:245-288` | medium | `_refresh_current` 的 `_guard` 复位不在 finally 中，异常路径将永久锁死面板编辑 |
| AUD-10310 | Suggestion | process | `src/features/composite/ui/material_panel.py:194` | high | `decode_material_overrides` 返回的诊断码被静默丢弃 |
| AUD-10311 | Suggestion | risk | `src/features/composite/ui/material_panel.py:355-361` | high | `_on_preset_changed` 程序化 `setCurrentIndex(0)` 未加状态锁，实际发生一次信号重入 |
| AUD-10312 | Suggestion | process | `src/features/composite/ui/material_panel.py:189-190` | high | `set_materials` 形参注解为 `object`，类型契约过弱 |
| AUD-10313 | Suggestion | process | `src/features/composite/ui/minimap_widget.py:38-39、84-85、90、99、201、298、305、325、328、335` | medium | 全文件类型注解缺失（返回注解、事件参数、parent 参数） |
| AUD-10400 | Critical | risk | `src/features/composite/ui/node_editor_actions.py:796` | medium | 使用未定义的 `logger`，粘贴 Topos Color 节点时抛 NameError 使粘贴中断/崩溃 |
| AUD-10401 | Major | bug | `src/features/composite/ui/node_editor_actions.py:490-509` | medium | `connect()` 双重提交：先改模型再发信号，页面处理器再次 connect，多输入端口产生重复连接与重复视觉线 |
| AUD-10402 | Major | bug | `src/features/composite/ui/node_editor_actions.py:529-536（另见 346-354）` | medium | `disconnect()`/`delete_selected_connections()` 自行移除视觉后又发信号，页面按 dst 再删一条，多输入端口误删无辜连线视觉 |
| AUD-10403 | Major | bug | `src/features/composite/ui/node_editor_actions.py:226-229（另见 324-331）` | medium | 删除节点前先 `_reconnect_around_node` 断开全部连接，导致 `_push_remove_undo` 捕获空连接列表，单步 undo 恢复的节点丢失全部连线 |
| AUD-10404 | Major | process | `src/features/composite/ui/node_editor_actions.py:177-193` | medium | `_rename_node_safe` 写入 `node._id`，但 `Node.id` 是普通属性，重命名实际未生效且 `_node_items` 未重键，图/序列化/视觉全面脱节 |
| AUD-10405 | Major | bug | `src/features/composite/ui/node_editor_actions.py:1338（副作用在 699）` | high | `save_selected_as_preset` 借道 `copy_selected_nodes` 覆写编辑器剪贴板，保存预设后 Ctrl+V 粘贴出预设源节点 |
| AUD-10406 | Major | bug | `src/features/composite/ui/node_editor_actions.py:783-791` | medium | 粘贴 topos_color 生成的 `new_stack_id` 从未真正创建对应 color stack，节点引用不存在的调色栈 |
| AUD-10407 | Major | bug | `src/features/composite/ui/node_editor_actions.py:156-174` | medium | `create_3d_scene_with_children` 每个子节点 emit `node_added`，页面逐个推 undo 并整图求值，一次拖拽产生 N 步 undo |
| AUD-10408 | Major | process | `src/features/composite/ui/node_editor_actions.py:215（对照 308、856、886）` | high | 删除保护判定不一致：单删按 `node_type`，多删/剪切/复制按可变的 `node_title`，重命名可绕过 Output/COLOR 保护 |
| AUD-10409 | Major | bug | `src/features/composite/ui/node_editor_actions.py:621-642` | medium | `insert_dot_on_connection` 先断开原连接，后续 connect 失败不回滚，链路断裂且 Dot 悬挂 |
| AUD-10410 | Major | bug | `src/features/composite/ui/node_editor_actions.py:1388-1389、1412-1416` | medium | `load_preset` 对外部预设 JSON 的 `position` 形状未验证，畸形数据触发 IndexError/TypeError |
| AUD-10411 | Suggestion | process | `src/features/composite/ui/node_editor_actions.py:670-681` | high | `copy_selected_nodes` 索引映射与 `nodes_data` 脱同步（被跳过节点仍占索引） |
| AUD-10412 | Suggestion | process | `src/features/composite/ui/node_editor_actions.py:744-747` | high | `paste_nodes` 位置偏移含魔法数 `-50` 且 x/y 不对称 |
| AUD-10413 | Suggestion | process | `src/features/composite/ui/node_editor_actions.py:154-173、271、735-835` | high | `_batch_mode` 直接置位/复位不平铺（嵌套被提前关闭），且 `paste_nodes`/`load_preset` 未用批量模式致 O(N²) 刷新与逐连接求值 |
| AUD-10414 | Suggestion | process | `src/features/composite/ui/node_editor_actions.py:1453` | high | `get_preset_manager` 返回注解 `"PresetManager"` 在模块级不可解析（未在 TYPE_CHECKING 导入） |
| AUD-10415 | Suggestion | process | `src/features/composite/ui/node_editor_actions.py:932-935` | high | `toggle_bypass`/`set_bypass` 返回值语义二义（False 既表示未找到也表示新状态为启用） |
| AUD-10500 | Major | process | `src/features/composite/ui/node_editor_widget.py:2132（另见 1496、4798）` | medium | Output/COLOR 节点保护与 Output 查找均基于显示标题，重命名后 Delete 键可删除 Output 节点 |
| AUD-10501 | Major | bug | `src/features/composite/ui/node_editor_widget.py:2094-2107` | medium | Delete 键批量删除 backdrop 后未发射 graph_changed，项目 dirty 标志可能缺失 |
| AUD-10502 | Major | risk | `src/features/composite/ui/node_editor_widget.py:986（实现 935-960）` | medium | dropEvent 在 UI 线程同步探测视频元数据（PyAV/ffmpeg），多文件拖放冻结界面 |
| AUD-10503 | Major | bug | `src/features/composite/ui/node_editor_widget.py:233（常量 157；处理器 916-932）` | medium | P7.2 assets_dropped 信号从未发射，dragEnterEvent 仅接受 URL 拖拽——媒体池资产拖放到节点画布功能未接线 |
| AUD-10504 | Major | bug | `src/features/composite/ui/node_editor_widget.py:1300-1303（super 转发在 1139）` | medium | Ctrl+点击多选与 Qt 场景默认选择处理双重应用，手动发射的选择信号可与画布状态失同步 |
| AUD-10505 | Major | bug | `src/features/composite/ui/node_editor_widget.py:2891-2913` | medium | `_add_new_node` 的 `_batch_mode` 置位无 try/finally，异常路径将永久卡在批量模式 |
| AUD-10506 | Major | bug | `src/features/composite/ui/node_editor_widget.py:4940-4953` | medium | `set_viewport_state` 未校验外部反序列化数据的类型，损坏的 viewport 状态可在建图时抛 TypeError |
| AUD-10507 | Suggestion | process | `src/features/composite/ui/node_editor_widget.py:2682-2687` | medium | `_on_backdrop_changed` 的"已删除"分支不发射 undo 信号也不发 graph_changed，与 sticky 对应处理器不对称 |
| AUD-10508 | Suggestion | process | `src/features/composite/ui/node_editor_widget.py:2353-2381` | high | `_flash_node_highlight` 每次导航新建 QTimer(self) 且从不销毁；闪烁通过切换真实选中态实现 |
| AUD-10509 | Suggestion | process | `src/features/composite/ui/node_editor_widget.py:3915-3916（同模式 3935-3936、3967-3968、3994-3995）` | high | 预设名从列表显示文本 `split(" (")[0]` 反解析，名称含 " (" 时操作错误对象 |
| AUD-10510 | Suggestion | process | `src/features/composite/ui/node_editor_widget.py:1390-1391（同模式 1409-1410、1891-1892、2230-2231、3103-3104、3465-3466、3531-3532）` | medium | 多处 `except Exception: pass` 静默吞掉配置/注册表/分组查询错误，无日志 |
| AUD-10511 | Suggestion | process | `src/features/composite/ui/node_editor_widget.py:16-33（属性见 4963、4990）` | medium | 未使用导入（copy/QMimeData/QByteArray/QFont/QColorDialog）；`_viewing_node_id` 未在 `__init__` 声明 |
| AUD-10512 | Suggestion | process | `src/features/composite/ui/node_editor_widget.py:836-855` | medium | `clear_all` 不复位 `_selected_node_id`，重建后可能返回悬空节点 ID |
| AUD-10513 | Suggestion | process | `src/features/composite/ui/node_editor_widget.py:137、727、916、927、962、1005、1805、2425、2540、2575、2834` | medium | 事件覆盖方法与部分工具函数缺少类型注解 |
| AUD-10514 | Suggestion | process | `src/features/composite/ui/node_editor_widget.py:3077-3086` | medium | `_execute_shake_disconnect` 的 `_shake_disconnecting = False` 位于 try/finally 之外，异常时标志卡 True |
| AUD-10601 | Critical | risk | `src/features/composite/ui/node_graph_commands.py:158-169` | medium | DeleteNodeCommand.undo 在锁外直接改写 graph.connections，数据竞争且绕过 revision/dirty 标记 |
| AUD-10602 | Major | risk | `src/features/composite/ui/node_graph_commands.py:127-141` | medium | 重连链路预测与引擎实现判定口径不一致（端口数 vs 连接数），undo 可泄漏自动重连链路 |
| AUD-10603 | Major | bug | `src/features/composite/ui/node_graph_commands.py:397-409` | medium | SetParameterCommand 以 None 兼作"未捕获"哨兵，旧值为 None 的参数 undo 无效 |
| AUD-10604 | Major | bug | `src/features/composite/ui/node_graph_commands.py:477-497` | medium | CopyNodesCommand 节点缺失时索引映射错位，粘贴时静默丢失内部连接 |
| AUD-10605 | Major | bug | `src/features/composite/ui/node_graph_commands.py:545-566` | medium | PasteNodesCommand.redo 对剪贴板数据零验证，且绕过 set_parameter 直接注入参数 |
| AUD-10606 | Suggestion | process | `src/features/composite/ui/node_graph_commands.py:265-276（ConnectCommand.undo）、302-313（DisconnectCommand.undo）、178-186（DeleteNodeCommand.undo 恢复连线）` | medium | 多处 undo 以宽泛 `except Exception: pass` 吞错（引擎 API 只返回 bool 不抛异常） |
| AUD-10607 | Suggestion | process | `src/features/composite/ui/node_graph_commands.py:358-377` | medium | ReconnectCommand.undo 忽略 disconnect/connect 返回值，旧连接恢复失败静默 |
| AUD-10608 | Suggestion | process | `src/features/composite/ui/node_graph_commands.py:837-843` | medium | ModifyBackdropCommand.undo 只恢复 old_data 中存在的键，新增键残留 |
| AUD-10609 | Suggestion | process | `src/features/composite/ui/node_graph_commands.py:550（PasteNodesCommand）、630（DuplicateNodesCommand）` | high | 新节点 ID 取 uuid4 前 8 位且不查重，与 add_node 静默替换语义叠加有理论数据丢失 |
| AUD-10610 | Suggestion | risk | `src/features/composite/ui/node_graph_commands.py:777-780（MoveBackdropCommand.redo；ResizeBackdropCommand 802-807、ModifyBackdropCommand 829-835 同型）` | medium | Backdrop 命令在锁外原地改写引擎内部 backdrops 字典 |
| AUD-10611 | Suggestion | process | `src/features/composite/ui/node_graph_commands.py:43-47、67-70、89-92、155-157、212-214、255-256、293-294、340-342、397-405、471-472、537-539、618-619、701-706 等` | high | 类型注解缺失：redo/undo/__post_init__ 等方法无返回类型注解 |
| AUD-10620 | Major | bug | `src/features/composite/ui/node_item.py:446-452（对照 paint 512-527、540-554）` | medium | boundingRect 未为常规节点的选中/抖动光晕扩展，绘制超出边界致局部重绘残影 |
| AUD-10621 | Major | perf-opportunity | `src/features/composite/ui/node_item.py:729-744` | medium | paint 每帧对缩略图执行 SmoothTransformation 缩放，无缓存 |
| AUD-10622 | Suggestion | process | `src/features/composite/ui/node_item.py:174-186（_title_item 仅在 _create_title 159-172 赋值，且 141-143 仅非 dot 调用）` | medium | set_title 访问 dot 节点从未初始化的 _title_item，潜在 AttributeError |
| AUD-10623 | Suggestion | process | `src/features/composite/ui/node_item.py:829-832（对照 set_thumbnail 775-784）` | high | clear_thumbnail 未调用 _update_layout，清除后保留缩略图占位高度 |
| AUD-10624 | Suggestion | process | `src/features/composite/ui/node_item.py:416-419、650-652` | medium | 进度值只 clamp 上界，负值/非法输入产生负宽度进度条 |
| AUD-10625 | Suggestion | process | `src/features/composite/ui/node_item.py:421-434、703-705（对照 _refresh_tooltip 395-404）` | high | _perf_error 文本从未展示给用户 |
| AUD-10626 | Suggestion | process | `src/features/composite/ui/node_item.py:32-44（keyPressEvent/focusOutEvent）、454（_paint_dot）、479（paint）、759（mouseDoubleClickEvent）、786（itemChange）、344（set_disabled）、329-331（update_connections）等` | medium | 类型注解缺失：Qt override 与绘制辅助方法无参数/返回注解 |
| AUD-10700 | Critical | bug | `src/features/composite/ui/node_properties_panel.py:3011（同类：2995、3279、3292、4427-4450）；根因在 1887 与 4418-4445` | medium | 多选/pinned 场景下编辑非首个 section 的参数会写入错误的节点 |
| AUD-10701 | Major | bug | `src/features/composite/ui/node_properties_panel.py:5370` | medium | `_show_parameter_context_menu` 读取面板上未定义的 `_group_promote_hook`，右键参数名菜单必然抛 AttributeError |
| AUD-10702 | Major | risk | `src/features/composite/ui/node_properties_panel.py:1813、1915` | medium | `set_nodes` 置位 `_block_signals` 后无 try/finally，构建异常会永久锁死参数变更通路 |
| AUD-10703 | Major | bug | `src/features/composite/ui/node_properties_panel.py:3649-3678（同型：3692-3719、3730-3757、3766-3792）` | medium | `*_for_section` 构建器的"上下文换入/换出"无异常保护，失败后面板上下文指向错误节点 |
| AUD-10704 | Major | bug | `src/features/composite/ui/node_properties_panel.py:3794-3891；注册点 2586、2593、2599、2936、3232、3554、6568` | medium | `_update_section_param_value` 缺少 file/hdri/channel/vec2/vec3/text/curve/color(无 update) 分支，外部更新对多类控件静默失效 |
| AUD-10705 | Major | bug | `src/features/composite/ui/node_properties_panel.py:1819-1821、1600、1605、6749-6751；清理路径 1827-1832 只删不在 visible_ids 的 section` | medium | 已删除节点的 pinned section 与 `_pinned_node_ids`/`_split_dims`/`_scene3d_toggle_buttons` 永不清理 |
| AUD-10706 | Major | bug | `src/features/composite/ui/node_properties_panel.py:5222-5225；消费点` | medium | `sync_animated_values` 把播放头采样值写进 `section.parameters`/`_current_parameters`，预设/重建会固化"当前帧值"而非基准参数 |
| AUD-10707 | Major | bug | `src/features/composite/ui/node_properties_panel.py:3521-3529、6061-6064（写显示值）vs 7579-7588（转换后再写）` | medium | 颜色写入的 sRGB/线性边界不一致：RGB 三合一行与 Keyer 直接写显示值，Constant 路径则做一次 sRGB→linear 转换 |
| AUD-10708 | Suggestion | process | `src/features/composite/ui/node_properties_panel.py:4265-4379、1592-1596、1950-1952 与 746、326-336` | medium | 死代码簇：无人调用的 `_update_param_widget_value`（且 curve 分支会在容器控件上调用不存在的 setCurve）、永不赋值的 legacy 多选状态、永不发射的 `node_reset_requested` |
| AUD-10709 | Suggestion | process | `src/features/composite/ui/node_properties_panel.py:2491-2496、1337-1340、1081-1117` | medium | 搜索框每个按键全量重建所有 section；custom builder 节点完全不响应搜索过滤 |
| AUD-10710 | Suggestion | process | `src/features/composite/ui/node_properties_panel.py:3011、3279、3292（` | medium | int 型参数全程被写入 float 值 |
| AUD-10711 | Suggestion | process | `src/features/composite/ui/node_properties_panel.py:626-632` | medium | CurveWidget 拖动约束在相邻点间距 <0.002 时失效，可产生乱序控制点 |
| AUD-10712 | Suggestion | process | `src/features/composite/ui/node_properties_panel.py:1381-1394` | medium | 节点颜色实时预览取消后不回滚，且每个 live tick 都发 `node_color_changed` |
| AUD-10713 | Suggestion | process | `src/features/composite/ui/node_properties_panel.py:1462-1467` | high | `_on_save_preset` 对 ImportError 静默返回，按钮无任何反馈 |
| AUD-10714 | Suggestion | process | `src/features/composite/ui/node_properties_panel.py:7049（对照 7002 的` | high | `_build_constant_ui` 直接下标 `params["mode"]`，旧数据缺键即 KeyError |
| AUD-10715 | Suggestion | process | `src/features/composite/ui/node_properties_panel.py:1588（以及 1050、1180、3338、5749、5928、6164、6902、6997 等构建器仅部分注解；` | high | 类型注解失实与缺失：`_param_widgets: Dict[str, QWidget]` 实存 dict；大量槽函数/构建器无注解 |
| AUD-10801 | Critical | bug | `src/features/composite/ui/node_search_popup.py:341（另见 366、387、475、544、552、617）` | medium | `Qt.ItemFlag.UserRole` 在 PyQt6 中不存在，弹窗构造即抛 AttributeError，Add Node 搜索弹窗完全不可用 |
| AUD-10802 | Major | bug | `src/features/composite/ui/node_search_popup.py:472-473（同类无效判断另见 550、616）` | high | 表头判断 `flags() & NoItemFlags` 恒为假（NoItemFlags==0），关键字过滤不会跳过类别表头 |
| AUD-10803 | Suggestion | process | `src/features/composite/ui/node_search_popup.py:404-411` | high | `_get_display_name` 用宽泛 `except Exception: pass` 吞掉 i18n 查错 |
| AUD-10804 | Suggestion | process | `src/features/composite/ui/node_search_popup.py:470-488、492-498` | medium | 过滤路径每次按键对每个条目做全类别线性扫描，复杂度 O(条目×类别×别名表) |
| AUD-10805 | Suggestion | process | `src/features/composite/ui/node_search_popup.py:150、247、311、317、348、369、453、572 等` | high | 多个方法缺失类型注解 |
| AUD-10806 | Suggestion | process | `src/features/composite/ui/node_thumbnail.py:216-217、277-290` | medium | ThumbnailWidget 单通道视图中 R 与 B 的显示颜色互换（缺陷位于当前未被引用的控件类中） |
| AUD-10807 | Suggestion | process | `src/features/composite/ui/node_thumbnail.py:450-451、398、419` | medium | 类型注解错误：`Tuple = None` 默认值与 `callable` 用作类型 |
| AUD-10808 | Suggestion | process | `src/features/composite/ui/perf_panel.py:89-98` | medium | `_trace_snapshots` 宽泛 `except Exception: pass` |
| AUD-10809 | Suggestion | perf-opportunity | `src/features/composite/ui/perf_panel.py:179-181` | high | 每帧调用 `_trace_snapshots()` 触发 provider 全量列表拷贝，仅为显示一个计数 |
| AUD-10810 | Suggestion | process | `src/features/composite/ui/perf_panel.py:32、39、184、193、222` | high | 缺失类型注解 |
| AUD-10811 | Suggestion | process | `src/features/composite/ui/port_item.py:151-159` | medium | `set_compact(False)` 不恢复尺寸/画笔/笔刷，仅实现单向紧凑化 |
| AUD-10812 | Suggestion | process | `src/features/composite/ui/preview_connection_item.py:53-57、61-68、116-126` | medium | 模块级单例动画驱动强引用条目，`_advance`/`update_path` 缺少对端 C++ 对象已销毁的防护 |
| AUD-10813 | Suggestion | process | `src/features/composite/ui/project_settings_section.py:124-127、181-183` | medium | `set_settings_callback` 与 section 级信号中继构成双通道，并用时回调将收到双份通知 |
| AUD-10814 | Major | bug | `src/features/composite/ui/project_settings_widget.py:163、399（对照正确的守卫写法 354-359；` | medium | FPS 组合框选择 "Custom" 时 `float("__custom__")` 抛 ValueError，settings_changed 中断发射 |
| AUD-10816 | Suggestion | perf-opportunity | `src/features/composite/ui/scene_outliner_panel.py:314-327` | high | `_apply_filter` 对每个条目重新 DFS 全部后代，整体 O(n²)，且挂在 textChanged 每键触发 |
| AUD-10900 | Suggestion | process | `src/features/composite/ui/spline_commands.py:14,28,38` | high | `from_key`/`apply_to` 的 `key` 参数无类型注解，且 `Optional` 导入未使用 |
| AUD-10901 | Major | bug | `src/features/composite/ui/spline_model.py:397-398,485-492` | medium | 曲线可见性状态在 rebuild 后全部丢失（`_visible_ids` 语义缺陷） |
| AUD-10902 | Major | bug | `src/features/composite/ui/spline_model.py:366-377,403-408` | medium | `rebuild`/`_node_color` 宽泛 `except Exception` 掩盖领域错误，失败时曲线已先被清空 |
| AUD-10903 | Suggestion | process | `src/features/composite/ui/spline_model.py:448-449,459` | high | 在 dataclass 实例上动态注入 `_had_saved_handles` 未声明属性 |
| AUD-10904 | Major | bug | `src/features/composite/ui/spline_panel.py:678-681,683-696,437-441（spline_view.py:273-275）` | medium | 树行选中/可见性切换调用 `view.refresh()` 重置视口，与「selection does not reset zoom」设计矛盾 |
| AUD-10905 | Major | bug | `src/features/composite/ui/spline_panel.py:229-239（spline_view.py:806-811 发射端）` | medium | 视图级 `current_frame_changed` 信号从未连接，标尺拖动 seek 失效 |
| AUD-10906 | Major | bug | `src/features/composite/ui/spline_panel.py:729-741` | medium | `_on_key_selection_changed` 直接改写共享选择模型私有 `_selection` 并调用私有 `_notify()`，绕过公共 API |
| AUD-10907 | Major | bug | `src/features/composite/ui/spline_panel.py:118-127（对照 74 行` | high | 曲线树 hover 索引按未过滤全集计算，与 paintEvent 的过滤子集不一致 |
| AUD-10908 | Suggestion | process | `src/features/composite/ui/spline_panel.py:666-676（对照 spline_view.py:1224-1229）` | medium | 工具栏切线按钮只作用于第一个选中 key，与右键菜单"作用于全部选中"不一致 |
| AUD-10909 | Suggestion | process | `src/features/composite/ui/spline_panel.py:427,557,587` | high | 多处公开/槽方法参数缺类型注解 |
| AUD-10910 | Critical | bug | `src/features/composite/ui/spline_view.py:1310-1311,1348-1349（函数定义 1021-1028,1065-1074）` | medium | 右键菜单 Delete/Select all 直接 connect 未绑定模块函数，`triggered(bool)` 把 False 当 self → AttributeError → PyQt6 槽内未处理异常终止进程 |
| AUD-10911 | Major | perf-opportunity | `src/features/composite/ui/spline_view.py:887-899` | medium | `_do_multi_key_drag` 的 `sel_set` 基于按下时刻帧快照，首个移动事件后键不再匹配 → 多选拖拽冻结在第一帧增量 |
| AUD-10912 | Major | bug | `src/features/composite/ui/spline_view.py:1180-1199` | medium | `_nudge_selected_keys` 正向 nudge 升序边遍历边改帧，相邻多选键右移被撕裂 |
| AUD-10913 | Major | bug | `src/features/composite/ui/spline_view.py:939-943（同型：907-909、1456-1458）` | medium | 键垂直拖动/改值时手柄 y 不随值平移（x 平移、y 不平移），贝塞尔曲线畸变 |
| AUD-10914 | Major | bug | `src/features/composite/ui/spline_view.py:759-765,979-986,994-1002` | medium | `_select_only_key` 不清除其他曲线的 `selected`，旧曲线残留选中态且 `curve_selection_changed` 发出旧曲线 → Keyframes 高亮错误参数行 |
| AUD-10915 | Suggestion | process | `src/features/composite/ui/spline_view.py:715-721,1294-1352` | medium | 右键同时用作 pan 起点与 context menu 触发源，平台相关冲突 |
| AUD-10916 | Suggestion | process | `src/features/composite/ui/spline_view.py:1406-1416` | medium | `_sv_mouseDoubleClickEvent` 中插入位置 `idx` 为死代码 |
| AUD-10917 | Suggestion | process | `src/features/composite/ui/spline_view.py:549-553` | high | `_draw_extrapolation` 死分支 `if ep.widthF() and False`，注释与代码不符 |
| AUD-10918 | Suggestion | process | `src/features/composite/ui/spline_view.py:1463-1493` | high | 模块尾部以 monkey-patch 方式绑定 24 个方法到 SplineView 类 |
| AUD-1100 | Major | bug | `src/shared/effects/preview_quality.py:330` | medium | 播放降级系统从未激活：全库无任何 `set_playback_state(True)` 调用 |
| AUD-11001 | Major | bug | `src/features/composite/ui/sticky_note_item.py:586-597` | medium | "Change Color" 拾色对话框在 `show()` 后随局部变量被 GC 销毁，功能完全不可用 |
| AUD-11002 | Major | bug | `src/features/composite/ui/sticky_note_item.py:279-285, 211-224, 592-594` | medium | 拾色器实时预览把每个中间颜色各推一步 undo（拖一次颜色 = N 个 undo 步），且取消不回滚 |
| AUD-11003 | Major | risk | `src/features/composite/ui/sticky_note_item.py:651-664` | medium | `from_dict` 对外部工程数据硬索引 `id`/`position`/`size`，缺键或值畸形直接抛 KeyError/TypeError 崩溃加载 |
| AUD-11004 | Suggestion | process | `src/features/composite/ui/sticky_note_item.py:48-49` | high | 构造参数 `position: QPointF = None` / `size: QRectF = None` 为隐式 Optional 注解 |
| AUD-1101 | Major | risk | `src/shared/effects/preview_quality.py:546` | medium | 无锁共享可变状态 + 非线程安全的懒汉式全局单例 |
| AUD-11010 | Major | risk | `src/features/composite/ui/timeline_commands.py:445-523（核心 489-511）` | medium | `commit_move` 未删除被拖拽关键帧的原帧 —— 拖动产生"复制"而非"移动"；preview 累积语义还会把中间帧一并落盘 |
| AUD-11011 | Major | bug | `src/features/composite/ui/timeline_commands.py:686-727` | medium | 表达式命令的 undo 记录不完整：`remove_expression` 不产生任何记录，`set_expression` 的 undo 无法恢复表达式文本 |
| AUD-11012 | Major | bug | `src/features/composite/ui/timeline_commands.py:610-632` | medium | `paste_keyframes`（及 `commit_move`）的值不经范围归一化，越界值静默落盘 |
| AUD-11013 | Suggestion | process | `src/features/composite/ui/timeline_commands.py:483-487` | high | `commit_move` 中 min_val/max_val 计算后从未使用（死代码，且印证范围钳制未实现） |
| AUD-11014 | Suggestion | process | `src/features/composite/ui/timeline_commands.py:794-821` | high | `_classify_error` 依赖错误消息字符串前缀/子串匹配映射稳定错误码，脆弱耦合 |
| AUD-11015 | Suggestion | process | `src/features/composite/ui/timeline_commands.py:911` | medium | `apply_video_range_edit` 中 `source_out` 取值缺 None 防护（与 `source_in` 的 `or 0` 不对称） |
| AUD-1102 | Major | bug | `src/shared/effects/preview_quality.py:384` | medium | 导出路径无任何保护，preview/export 语义一致性仅靠调用方约定 |
| AUD-11020 | Suggestion | risk | `src/features/composite/ui/timeline_expression.py:206-212` | medium | `derive_expression_status` 语法校验路径用宽泛 `except Exception` 把解析器崩溃当作 VALID |
| AUD-11021 | Suggestion | doc-gap | `src/features/composite/ui/timeline_expression.py:124-150` | medium | `EXPR_ERR_EMPTY` 为死常量：文档承诺空表达式映射该码，但无任何路径产出 |
| AUD-11022 | Suggestion | process | `src/features/composite/ui/timeline_expression.py:297-343` | medium | `_node_*` 鸭子类型访问器统一吞掉节点实现的真实异常 |
| AUD-11025 | Major | bug | `src/features/composite/ui/timeline_expression_editor.py:248-278, 186-211` | medium | 实时校验预览不反映正在输入的文本：provider 只见 (node_id, param_name)，读的是已存表达式 |
| AUD-11026 | Suggestion | process | `src/features/composite/ui/timeline_expression_editor.py:214-217` | high | `close()` 覆盖返回 None，破坏 `QWidget.close() -> bool` 契约 |
| AUD-11027 | Suggestion | doc-gap | `src/features/composite/ui/timeline_expression_editor.py:8-9, 26-27 对比 69-83` | high | 文档与实现的提交快捷键矛盾：文档称 "Enter / Ctrl+Enter → commit"，实现为普通 Enter 换行、仅 Ctrl/Cmd+Enter 提交 |
| AUD-1103 | Major | bug | `src/shared/effects/preview_quality.py:464` | medium | 播放中重新启用降级后 `degraded_effects` 不重建，状态追踪与决策路径脱钩 |
| AUD-11030 | Suggestion | process | `src/features/composite/ui/timeline_icons.py:463-464` | high | `_draw_graph_editor` 函数内重复局部导入顶层已有的 QPainterPath |
| AUD-1104 | Major | bug | `src/shared/effects/preview_quality.py:140` | medium | 降级参数为覆盖式合并而非钳制，可能反向增大计算量/改变画面 |
| AUD-1105 | Major | bug | `src/shared/effects/preview_quality.py:132` | medium | SKIP 路径硬编码 `{"enabled": False}`：丢弃全部原始参数，且使已注册的 `degradation_params` 成为死配置 |
| AUD-1106 | Suggestion | process | `src/shared/effects/preview_quality.py:365` | high | `_apply_playback_degradation` 把注册表全部条目标记为已降级，UI 指示器误报 |
| AUD-1107 | Suggestion | process | `src/shared/effects/preview_quality.py:484` | medium | 自定义阈值 API 与降级决策完全断链，设置阈值静默无效 |
| AUD-1108 | Suggestion | process | `src/shared/effects/preview_quality.py:280` | high | `last_degradation_time` 声明后永不更新（恒为 0.0） |
| AUD-1109 | Suggestion | process | `src/shared/effects/preview_quality.py:136` | medium | HIGH/MEDIUM/LOW 三个降级分支实现完全相同，末尾 `return` 不可达；返回值所有权不一致 |
| AUD-11100 | Suggestion | process | `src/features/composite/ui/timeline_layout.py:205-217（及 253-257 每次调用）` | medium | `_project_tokens()` 宽泛 `except Exception` 且每次解析都重建 token 映射，无缓存 |
| AUD-11101 | Suggestion | process | `src/features/composite/ui/timeline_layout.py:35` | high | 未使用的导入 `field` |
| AUD-11102 | Major | bug | `src/features/composite/ui/timeline_session.py:291-292` | medium | `TimelineSessionState.from_dict` 对外部文件中的 `schema_version` 无保护，非数值直接抛异常中断整个会话状态加载 |
| AUD-11103 | Suggestion | process | `src/features/composite/ui/timeline_session.py:217-220` | high | `ColumnModel.from_dict` 用宽泛 `except Exception: continue` 静默丢弃畸形列配置 |
| AUD-11104 | Suggestion | process | `src/features/composite/ui/timeline_session.py:142-146` | medium | `ColumnModel.column_widths` 在总宽不足时返回的列宽之和可超出 `total_width`（无紧凑回退） |
| AUD-11105 | Suggestion | process | `src/features/composite/ui/timeline_session.py:308-311` | medium | `horizontal_scroll` 反序列化无范围约束（可有负值/超大值） |
| AUD-11106 | Major | bug | `src/features/composite/ui/timeline_shortcuts.py:301-307` | medium | `handle_key` 以 `except Exception: pass` 吞掉命令处理器异常，按键静默失败 |
| AUD-11107 | Suggestion | process | `src/features/composite/ui/timeline_shortcuts.py:354-365（对照绑定表 99-102）` | medium | `CMD_SET_WORK_IN/OUT`（单字母 I/O/B/N）缺失于 `_TEXT_SUPPRESSED_COMMANDS`，焦点守卫对这几个字母失效 |
| AUD-11108 | Suggestion | doc-gap | `src/features/composite/ui/timeline_shortcuts.py:178-190` | high | `_normalise_sequence` 文档承诺 Cmd↔Ctrl 查找期折叠，实现只做空白清理，两者不一致 |
| AUD-11109 | Suggestion | process | `src/features/composite/ui/timeline_shortcuts.py:258-259、328-330` | medium | `handle_key(now_ms=None)` 把"未知时间"当作 0，省略参数的调用方任意两次 U/E 都算双击 |
| AUD-11110 | Major | bug | `src/features/composite/ui/timeline_time_display.py:152-153、253-262、268-269` | medium | 回车提交非法输入时 `editingFinished` 紧随 `returnPressed` 触发取消，设计要求的"红框 + 保持编辑态"实际不可达 |
| AUD-11111 | Major | bug | `src/features/composite/ui/timeline_time_display.py:169、248、258` | medium | 错误边框选择器写成 `QLineEdit#invalid`（objectName 匹配），代码却设置动态属性，红框规则永远不生效 |
| AUD-11112 | Suggestion | process | `src/features/composite/ui/timeline_time_display.py:132、142` | high | `setFamily` 传入逗号分隔字体列表不生效，等宽字体意图静默失败 |
| AUD-11113 | Suggestion | process | `src/features/composite/ui/timeline_time_display.py:38` | high | 未使用的导入 `C` |
| AUD-11114 | Suggestion | process | `src/features/composite/ui/timeline_time_display.py:303-305、342-344、263-266` | medium | 解析器接受负数/无上界帧号，且提交时先用未钳制值更新自身显示 |
| AUD-11115 | Major | bug | `src/features/composite/ui/timeline_view_model.py:447-453（对照 540-547 的不变量）` | medium | `select_keyframe` 替换模式只清 keyframes 不清 rows（仅丢弃旧 primary），且不把新关键帧所在行加入 rows，破坏与 `sync_from_model` 的选择不变量 |
| AUD-11116 | Suggestion | process | `src/features/composite/ui/timeline_view_model.py:407-413` | high | `_notify` 以 `except Exception: pass` 吞掉订阅者异常，无日志 |
| AUD-11117 | Suggestion | process | `src/features/composite/ui/timeline_view_model.py:312` | medium | `build_timeline_rows` 把 `current_value` 填成"最后一个关键帧的值"而非当前帧处的值，字段语义误导 |
| AUD-11200 | Major | bug | `src/features/composite/ui/viewer3d_settings_section.py:752` | medium | hdri_preset 支持任意文件路径但下拉框只有 3 个固定项，值不匹配时 UI 显示残留错项 |
| AUD-11201 | Major | bug | `src/features/composite/ui/viewer3d_settings_section.py:119` | medium | 外部持久化设置未做类型校验，畸形值在 load_from_graph → _sync_widgets 抛未捕获异常 |
| AUD-11202 | Suggestion | process | `src/features/composite/ui/viewer3d_settings_section.py:582` | medium | 整型键（nav_restore_delay_ms / inactive_view_update_rate）经 valueChanged(float) 回写后类型漂移为 float，且 _drag 边界传 int 字面量 |
| AUD-11203 | Suggestion | process | `src/features/composite/ui/viewer3d_settings_section.py:778` | medium | 7 个 _pick_*_color 方法结构完全重复（仅 key 前缀不同） |
| AUD-11204 | Major | bug | `src/features/composite/ui/viewer_handle_drawing.py:141` | medium | 角点手柄被 "_left/_right/_top/_bottom" 后缀分支截获，绘制成竖条，_draw_handle_square 对 crop/reformat 全部手柄不可达 |
| AUD-11205 | Suggestion | process | `src/features/composite/ui/viewer_handle_drawing.py:148` | medium | GL 状态恢复不完整且 GL 句柄未验证 |
| AUD-11206 | Suggestion | process | `src/features/composite/ui/viewer_handle_drawing.py:154` | high | 私有绘制函数缺类型注解、selected_node_id 参数与 GL_TRUE 常量未使用、DORMANT 头注与仓库现状不符 |
| AUD-11300 | Major | bug | `src/features/composite/ui/viewer_handles.py:1709` | medium | Mask/Roto 边缘缩放的“对边固定”中心补偿公式错误（dx 的二次式而非线性），对边随拖拽漂移 |
| AUD-11301 | Major | bug | `src/features/composite/ui/viewer_handles.py:1272` | medium | Feather 手柄外法线用 R(−θ) 旋转，与形状角点/边缘的 R(+θ) 相反，旋转后的 Mask 羽化手柄方向镜像错位 |
| AUD-11302 | Major | bug | `src/features/composite/ui/viewer_handles.py:1688` | medium | Mask/Roto 旋转手柄用绝对角度且手柄恒在 bbox 正上方：对已旋转形状发起旋转拖拽会瞬间跳回 ~0° |
| AUD-11303 | Major | bug | `src/features/composite/ui/viewer_handles.py:1693` | medium | Mask/Roto 旋转拖拽方向疑似与渲染/手柄旋转方向相反（与同文件 Transform 约定不一致） |
| AUD-11304 | Major | bug | `src/features/composite/ui/viewer_handles.py:1800` | medium | Feather 拖拽 T/L 方向与 B/R 相反（且与自身注释矛盾）：向外拖反而减小羽化 |
| AUD-11305 | Major | bug | `src/features/composite/ui/viewer_handles.py:1871` | medium | Roto opacity 手柄拖拽方向与两处行内注释相反（代码：向下=减小；注释：向下=增大） |
| AUD-11306 | Major | bug | `src/features/composite/ui/viewer_handles.py:1741` | medium | compute_mask/roto_delta_params 未归一化 modifiers：与 transform 版本同样的 PyQt6 枚举 TypeError 隐患，且 Shift 均匀缩放实际失效 |
| AUD-11307 | Suggestion | test-gap | `src/features/composite/ui/viewer_handles.py:557` | high | 六个导出无任何调用方（死代码）：定义生成器/命中测试/cornerpin 增量与真实实现双轨漂移 |
| AUD-11308 | Suggestion | process | `src/features/composite/ui/viewer_handles.py:913` | high | 拖拽热路径上每次调用都重建枚举值列表做成员检查 |
| AUD-11309 | Suggestion | process | `src/features/composite/ui/viewer_handles.py:1823` | medium | 多边形半径手柄在竖幅（H>W）画面上不跟手：归一化用 max(W,H)、手柄绘制用宽度半轴 |
| AUD-11310 | Suggestion | risk | `src/features/composite/ui/viewer_handles.py:1012` | high | Shift 语义在 Transform 与 Mask 角点之间相反：Transform 默认等比、Shift 解锁；Mask 默认自由、Shift 等比 |
| AUD-11311 | Suggestion | doc-gap | `src/features/composite/ui/viewer_handles.py:1166` | high | 文档字符串陈旧：声称 RotoShapeNode feather 范围 0-1，实际节点与代码钳制均为 0-0.5 |
| AUD-11312 | Suggestion | process | `src/features/composite/ui/viewer_handles.py:742` | high | ndc_to_screen_pixels / screen_pixels_to_ndc 的 DPR 乘除相互抵消，DPR 形同虚设 |
| AUD-11400 | Major | risk | `src/features/composite/ui/viewer_tool_system.py:116-155` | medium | `reset()` 与 `start_drag()` 均不清空 `preview_params`，上一次拖拽的增量泄漏进下一次交互 |
| AUD-11401 | Major | doc-gap | `src/features/composite/ui/viewer_tool_system.py:1604-1626（对照 1661-1672）` | medium | `compute_constrained_delta` 的 Shift-锁定纵横比约定与现行实现及文档相反 |
| AUD-11402 | Suggestion | process | `src/features/composite/ui/viewer_tool_system.py:157-180` | high | `update_drag` 的 `ndc_pos`/`format_pos` 参数被静默丢弃 |
| AUD-11403 | Suggestion | process | `src/features/composite/ui/viewer_tool_system.py:1496-1499` | high | `apply_angle_constraint` 使用可变默认参数 |
| AUD-11404 | Suggestion | process | `src/features/composite/ui/viewer_tool_system.py:1124-1125（消费点 1340）` | high | `SnapConfig.threshold_pixels`（屏幕像素阈值）定义后全库未使用 |
| AUD-11405 | Suggestion | process | `src/features/composite/ui/viewer_tool_system.py:328-329、354-355、421-422` | medium | `view_pos_to_ndc`/`ndc_to_view_pos`/`image_pos_to_ndc` 缺零除防护，与 `compute_image_ndc_rect` 的防护不一致 |
| AUD-11406 | Suggestion | process | `src/features/composite/ui/viewer_tool_system.py:888-900` | high | `parse_preset_dimensions` 对未知 preset 名静默回退 (1920, 1080) |
| AUD-11407 | Suggestion | process | `src/features/composite/ui/viewer_tool_system.py:572-587` | high | `hit_test_rect` 不归一化矩形，反拖（x0>x1）时恒为 False |
| AUD-11408 | Suggestion | process | `src/features/composite/ui/viewer_tool_system.py:114、141、163` | high | `modifier_keys`/`modifiers` 注解为 int，实际承载 PyQt6 的 `Qt.KeyboardModifiers` 标志对象 |
| AUD-11409 | Suggestion | process | `src/features/composite/ui/viewport3d_controller.py:121-134、80-86` | high | `on_mmb_release` 的 `event` 参数未使用；`reset()` 以 `__class__` 间接引用枚举 |
| AUD-11410 | Suggestion | doc-gap | `src/features/composite/ui/viewport3d_state.py:48-52（对照 80-84）` | medium | PREVIEW 档位文档与参数表不一致：文档写"简化 PBR + 可选 MSAA"，参数与 FINAL 完全相同 |
| AUD-11411 | Suggestion | process | `src/features/composite/ui/viewport3d_toolbar.py:22` | high | 未使用的导入 `QSize`、`Qt` |
| AUD-11412 | Suggestion | process | `src/features/composite/ui/viewport3d_toolbar.py:62-68（对照 129-135）` | medium | `_COMPONENT_ENABLED` 注释称 Point/Edge/Polygon "Phase 6 标记 disabled"，实际全部为 True |
| AUD-11413 | Suggestion | process | `src/features/composite/ui/viewport3d_toolbar.py:94、121` | high | `_buttons`/`_comp_buttons` 使用裸 `dict` 注解 |
| AUD-11500 | Major | perf-opportunity | `src/features/composite/utils/pass_fusion.py:234-247、343-347；调用点 src/features/composite/evaluators/base_evaluator.py:619-621` | medium | CONSERVATIVE 尺寸兼容检查把"尺寸未知"当兼容，且唯一生产调用点传入空 cache，检查恒为空转 |
| AUD-11501 | Suggestion | process | `src/features/composite/utils/pass_fusion.py:488-492` | high | get_fusion_mode_from_config 对非字符串配置值直接 .lower() 会抛 AttributeError |
| AUD-11502 | Suggestion | process | `src/features/composite/utils/pass_fusion.py:209-223` | medium | _get_texture_dimensions 的 snapshot 参数未使用，注释与实现不符 |
| AUD-11503 | Suggestion | process | `src/features/composite/utils/pass_fusion.py:315/316-319/407-423` | high | current_shader_type 状态被维护但 flush_chain 从不读取（死状态） |
| AUD-11510 | Major | bug | `src/features/composite/utils/expression_parser.py:1259-1264` | medium | propagate_change 的求值上下文缺失 time/fps/layer_attrs 等字段，同一表达式与 evaluate_all 结果不一致 |
| AUD-11511 | Major | risk | `src/features/composite/utils/expression_parser.py:1188-1196（迭代）；938-983/985-998（修改）` | medium | ExpressionManager 全程无锁，依赖图字典在并发修改下迭代可抛 RuntimeError |
| AUD-11512 | Suggestion | process | `src/features/composite/utils/expression_parser.py:178-180` | medium | 除法除零静默返回 +inf（符号丢失、0/0≠nan），DIVISION_BY_ZERO 错误类型经 `/` 不可达 |
| AUD-11513 | Suggestion | process | `src/features/composite/utils/expression_parser.py:742（FORBIDDEN 含 ast.UAdd）与 183（SAFE_OPERATORS 含 UAdd）` | high | validate() 禁止一元加号，而求值器却实现了 UAdd，行为自相矛盾 |
| AUD-11514 | Suggestion | process | `src/features/composite/utils/expression_parser.py:390-399` | high | evaluate() 每次调用都对缓存 AST 重新 ast.walk 计数（热路径重复开销） |
| AUD-11515 | Suggestion | process | `src/features/composite/utils/expression_parser.py:641-649` | medium | _eval_attribute 尾部嵌套属性分支为不可达死代码 |
| AUD-11516 | Suggestion | process | `src/features/composite/utils/expression_parser.py:1124-1126` | high | evaluate_all 的 layer_attrs 回退用 node_id[2:] 魔法剥前缀，空字典 falsy 语义混淆 |
| AUD-11520 | Suggestion | process | `src/features/composite/utils/perf_monitor.py:249-282` | medium | 丢帧计数可被 get_snapshot 提前清零，首帧前的丢帧永久丢失、history 遥测系统性偏低 |
| AUD-11521 | Suggestion | process | `src/features/composite/utils/perf_monitor.py:238-245` | medium | record_3d_stats 保存调用方传入的可变对象引用，未做防御性拷贝 |
| AUD-11530 | Major | bug | `src/features/composite/utils/preset_manager.py:142、217、251-252、302、353` | medium | 预设名未做路径安全验证即拼接文件路径，import_preset 的名字来自外部 JSON，存在路径穿越写/删任意 .json 风险 |
| AUD-11531 | Major | bug | `src/features/composite/utils/preset_manager.py:247-268` | medium | rename_preset 先删旧文件再写新文件，写失败时旧文件已丢失且内存状态分裂 |
| AUD-11532 | Suggestion | risk | `src/features/composite/utils/preset_manager.py:144-145、258-259、304-305、329-330、355-356` | high | 所有持久化写入非原子（open("w") 先截断），中途崩溃/断电损坏文件 |
| AUD-11533 | Suggestion | process | `src/features/composite/utils/preset_manager.py:155-164` | high | load_preset 返回内部缓存 dict 引用，调用方可意外篡改缓存 |
| AUD-11534 | Suggestion | doc-gap | `src/features/composite/utils/preset_manager.py:47-50` | high | 默认 presets 目录取自 os.getcwd()，与文档"项目根目录"不符且随启动目录漂移 |
| AUD-11535 | Suggestion | process | `src/features/composite/utils/preset_manager.py:77-78、287-299` | high | 加载时同名预设静默覆盖、导入不校验 version 字段 |
| AUD-11600 | Critical | bug | `src/features/composite/utils/serialization_formats.py:256-263, 271-363` | medium | 增量 delta 仅追踪硬编码白名单键，fps/duration/layers/layer_stacks 等顶层字段修改被静默丢弃（数据丢失） |
| AUD-11601 | Major | bug | `src/features/composite/utils/serialization_formats.py:131-135` | medium | serialize() 就地修改调用方传入的 dict（写入 `_format_version`），违反无副作用契约 |
| AUD-11602 | Major | bug | `src/features/composite/utils/serialization_formats.py:196-201` | medium | deserialize() 不校验顶层类型，非 dict 载荷违反返回注解，下游 AttributeError |
| AUD-11603 | Major | bug | `src/features/composite/utils/serialization_formats.py:491-500` | medium | load_incremental 对 base_version 不匹配仅告警仍强行套用 delta，且保存后基线从不推进 |
| AUD-11604 | Major | bug | `src/features/composite/utils/serialization_formats.py:280-281` | high | _compute_graph_delta 假定节点结构完好，畸形外部数据触发 KeyError/TypeError |
| AUD-11605 | Suggestion | process | `src/features/composite/utils/serialization_formats.py:131-140` | medium | msgpack 不可用回退 JSON 后，文件内 `_format_version` 仍标记为 MSGPACK |
| AUD-11606 | Suggestion | process | `src/features/composite/utils/serialization_formats.py:496` | medium | 条件表达式优先级使版本校验"碰巧正确"，可读性差且越过私有属性访问 |
| AUD-11607 | Suggestion | process | `src/features/composite/utils/serialization_formats.py:98-100` | high | detect_format 用宽泛 `except Exception` 吞掉 gzip 解压的所有异常 |
| AUD-11608 | Suggestion | process | `src/features/composite/utils/serialization_formats.py:286-289` | high | nodes_added 基于 set 遍历，序列化字节不可复现 |
| AUD-11609 | Suggestion | process | `src/features/composite/utils/serialization_formats.py:401-417` | high | apply_delta 的 nodes_modified 匹配为 O(N×M) 线性扫描 |
| AUD-11610 | Suggestion | process | `src/features/composite/utils/serialization_formats.py:90, 185-187` | medium | 对外部文件的 gzip 解压无大小上限（解压炸弹） |
| AUD-11611 | Suggestion | risk | `src/features/composite/utils/serialization_formats.py:206-234` | medium | IncrementalSaver 无任何锁保护，跨线程共用将产生数据竞争 |
| AUD-11612 | Suggestion | process | `src/features/composite/utils/serialization_formats.py:116, 289` | high | 内建名遮蔽：参数 `format` 与循环变量 `id` |
| AUD-11901 | Suggestion | risk | `src/features/deliver/core/custom_export_presets.py:156` | high | created_at 回退 os.path.getmtime 未捕获 OSError，竞态可炸掉整个预设列表 |
| AUD-11902 | Suggestion | process | `src/features/deliver/core/delivery_capability.py:77-78` | high | container 推导对 gif 与序列 codec 给出错误的 "mov" |
| AUD-11903 | Suggestion | process | `src/features/deliver/core/delivery_capability.py:91-93（配合 123 行 except）` | medium | 装饰性导入 ExportPreflightResult 使效果投影整体静默降级为 "_error" |
| AUD-11904 | Major | bug | `src/features/deliver/core/export_queue.py:335-341（缺陷）、421-425（成因）、365-368（retry 的正确对照）` | medium | resume() 在暂停排空循环后无法重启队列（_running 已被置 False） |
| AUD-11905 | Major | bug | `src/features/deliver/core/export_queue.py:188-195（及 113-119 的强转）` | medium | load_export_queue 对"合法 JSON 但形状错误"的队列文件抛未捕获异常 |
| AUD-11906 | Major | bug | `src/features/deliver/core/export_queue.py:421-425` | medium | _process_next 尾递归调度，千级批量作业触发 RecursionError |
| AUD-11907 | Suggestion | perf-opportunity | `src/features/deliver/core/export_queue.py:313-316` | high | clear_completed 用 O(n²) 值相等成员判断，且可误删字段全同的作业 |
| AUD-11908 | Suggestion | process | `src/features/deliver/core/export_queue.py:52（定义）、313（唯一读取点）` | high | JobStatus.PAUSED 从未被赋值，是永不出现的状态 |
| AUD-11909 | Major | bug | `src/features/deliver/core/video_batch_export_controller.py:229-247（配合 export_queue.py:290-291 的 raise、deliver_workspace.py:477 的无捕获调用）` | medium | restore_from_disk 无异常防护：重复 job_id / 坏快照可冒泡炸掉工程切换主流程 |
| AUD-11910 | Suggestion | process | `src/features/deliver/core/video_batch_export_controller.py:232-234` | medium | 无 timeline_id 的恢复作业被强制 PENDING，与"leave as-is"注释意图不符 |
| AUD-11911 | Suggestion | process | `src/features/deliver/core/video_export_formats.py:53-55（表）、89-94（文案）` | medium | 序列 codec 的兼容性错误文案渲染为 "Allowed: ."，用户无从理解 |
| AUD-11912 | Suggestion | process | `src/features/deliver/core/video_export_job.py:902-918（配合 156 行默认值）` | medium | _topos_pixel_format_override 用 startswith("topos") 误吞 topos_image_sequence，默认 pixel_format 必打误导性告警 |
| AUD-11913 | Suggestion | process | `src/features/deliver/core/video_export_job.py:637-666（from_dict）、757-775（from_flat_config）` | high | from_dict/from_flat_config 的 int()/float() 强转无容错，坏队列文件在恢复路径抛 ValueError |
| AUD-11914 | Major | bug | `src/features/deliver/core/video_export_preflight.py:331-349` | medium | _check_color_delivery_preflight 裸吞全部 Exception 且零日志，色彩丢弃告警可能整体静默失效 |
| AUD-11915 | Suggestion | process | `src/features/deliver/core/video_export_preflight.py:437-452（gif 告警与音频检查门）、296-307（topos 分支静默通过）` | medium | 序列 codec 的"音频将被丢弃"提示缺失或误导（仅 gif 有明确告警） |
| AUD-11916 | Suggestion | process | `src/features/deliver/core/video_export_preflight.py:213-220（检查清单）` | medium | preflight 不调用 job.validate()，模型级结构校验在入队路径无人执行 |
| AUD-11917 | Suggestion | process | `src/features/deliver/core/video_export_preflight.py:679-684` | medium | 占位片段 allow 判定含死代码 extra_options 分支，且条件表达式优先级易误读 |
| AUD-11918 | Suggestion | process | `src/features/deliver/core/video_export_preflight.py:552-558` | high | _check_disk_space 的 target_dir 表达式含完全重复的分支 |
| AUD-1200 | Suggestion | process | `src/shared/gpu/backend_selector.py:186-201` | medium | 非 macOS 平台 AUTO compute 命中后续候选时误报 `metal_compute_unavailable` 作为回退原因 |
| AUD-12000 | Major | bug | `src/features/deliver/core/video_export_presets.py:279-287（辅证 :249-252、:331-333）` | medium | 文件名模板仅消毒替换值，模板字面量中的路径分隔符原样放行，破坏"basename only"契约 |
| AUD-12001 | Suggestion | doc-gap | `src/features/deliver/core/video_export_presets.py:59-60（对照 :64-72、:316-331）` | high | VideoPreset 文档声明 `container_override` 字段但 dataclass 未定义、构建逻辑也未消费 |
| AUD-12002 | Suggestion | process | `src/features/deliver/core/video_export_presets.py:259-287、:331-333` | high | 模板展开结果可为空串，生成 ".mp4" 式隐藏文件名且能通过 validate() |
| AUD-12003 | Suggestion | process | `src/features/deliver/core/video_export_presets.py:355-368` | high | `_container_extension` 对未知容器静默回落 ".mov"，可能产生扩展名与容器不符的输出 |
| AUD-1201 | Suggestion | process | `src/shared/gpu/backend_selector.py:141, 165-169, 213` | high | 多个公共/内部函数参数缺少类型注解 |
| AUD-12010 | Major | risk | `src/features/deliver/core/video_job_runner.py:353（辅证 :194、:200、:263-272；video_export_worker.py:244-245、:332-338）` | medium | 每个作业的 VideoExporter 以 runner 为父对象创建但从不删除，长队列会话中子对象/线程句柄无界累积 |
| AUD-12011 | Major | risk | `src/features/deliver/core/video_job_runner.py:251-282（辅证 :212；export_queue.py:418-425）` | medium | 嵌套 QEventLoop 退出而无结果时无任何护栏：退出即把作业误判为失败并继续调度下一作业，应用退出时会连锁空跑整个队列 |
| AUD-12012 | Major | doc-gap | `src/features/deliver/core/video_job_runner.py:286-300（辅证 :166-177、:200-206）` | medium | request_cancel 在 exporter 尚未就绪的窗口内被静默丢弃——runner 没有与 pause 对称的 pending-cancel 标志，而文档宣称任意线程可调 |
| AUD-12013 | Suggestion | process | `src/features/deliver/core/video_job_runner.py:298` | high | 日志占位符 `%e` 配异常参数，格式化必然 TypeError，告警信息丢失 |
| AUD-12014 | Suggestion | process | `src/features/deliver/core/video_job_runner.py:213-215、:302-306、:314-321、:232-237（` | medium | 暂停计时不自洽：跨作业的 pending pause 时间戳被 run_job 清空，export_fps 把暂停时长计入耗时导致速率被低估 |
| AUD-12015 | Suggestion | process | `src/features/deliver/core/video_job_runner.py:167、:177、:183-189、:198（` | high | 早期失败路径（context 解析/config 映射/worker 创建失败）直接 raise，跳过状态清理，`_current_job_id` 残留过期值 |
| AUD-12016 | Suggestion | process | `src/features/deliver/core/video_job_runner.py:338-355（辅证 :194）` | medium | `_create_worker` 用 getattr 反射私有属性并返回从未被使用的 thread；方法缺返回类型注解 |
| AUD-1202 | Suggestion | process | `src/shared/gpu/backend_types.py:154-157` | medium | `from_dict` 对 `forced` 字段用 `bool()` 强转，字符串 `"false"` 会被判为 `True` |
| AUD-1203 | Suggestion | process | `src/shared/gpu/capabilities.py:153-161` | medium | `_major_minor` 对非数字开头的版本串抛未捕获 ValueError |
| AUD-1204 | Suggestion | process | `src/shared/gpu/capabilities.py:113 与 459-461` | medium | Metal 设备探测在同一次 capabilities 快照内执行两次 |
| AUD-1205 | Suggestion | process | `src/shared/gpu/capabilities.py:429-464（并见 213）` | medium | `detect_backend_capabilities` 无任何缓存，每次调用全量重探（含解码真实媒体文件的 VT probe） |
| AUD-1206 | Suggestion | process | `src/shared/gpu/capabilities.py:128-133` | high | `_metal_device_available` 吞掉全部异常，"不支持"与"探测失败"不可区分 |
| AUD-1207 | Major | bug | `src/shared/gpu/composite_gpu_frame_handle.py:193-204` | medium | `wait_fence` 的 `timeout_ns` 语义错误：非零值传给 `glWaitSync` 违反 GL 规范（timeout 必须为 GL_TIMEOUT_IGNORED） |
| AUD-1208 | Suggestion | process | `src/shared/gpu/composite_gpu_frame_handle.py:110-114` | high | `release()` 回调异常被静默吞掉且无日志，"恰好归还一次"契约失去可观测性 |
| AUD-1209 | Suggestion | risk | `src/shared/gpu/composite_gpu_frame_handle.py:207-215` | high | `delete_fence` 吞掉 `glDeleteSync` 失败，GLsync 泄漏无诊断 |
| AUD-1210 | Major | risk | `src/shared/gpu/gl_backend.py:35-45` | medium | `initialize` 完全忽略 `pyqt_context` 参数且无线程守卫，UI 线程调用即把 ModernGL 上下文绑到 UI 线程 |
| AUD-12100 | Major | risk | `src/features/deliver/facade.py:31` | medium | facade 只捕获 ImportError，遗漏 module.py 明确防御的 AttributeError 导入失败路径，异常可逃逸致启动崩溃 |
| AUD-12101 | Suggestion | process | `src/features/deliver/facade.py:31-32` | high | DeliverWorkspace 导入失败静默降级，无任何日志 |
| AUD-12102 | Suggestion | process | `src/features/deliver/facade.py:20` | high | get_deliver_page_class 缺参数/返回类型注解 |
| AUD-12103 | Suggestion | process | `src/features/deliver/module.py:89-92` | medium | 页面工厂捕获全部 Exception 降级到旧页，可能掩盖 DeliverWorkspace 构造期真实缺陷 |
| AUD-12104 | Suggestion | process | `src/features/deliver/module.py:142, 233, 295` | high | register_menu_actions / register_shortcuts / register_panels 参数缺类型注解 |
| AUD-12105 | Suggestion | process | `src/features/deliver/preferences.py:242-254` | medium | restore_defaults 对 defaults 直接下标访问，与同函数 .get 风格不一致；KeyError 会被调用方静默吞掉 |
| AUD-12106 | Suggestion | process | `src/features/deliver/preferences.py:45, 51` | high | 注释声称 Weak reference 但实际保存强引用 |
| AUD-12107 | Suggestion | process | `src/features/deliver/preferences.py:27-30` | high | 跨模块导入 export_panel 的私有下划线样式助手 |
| AUD-12108 | Suggestion | process | `src/features/deliver/preferences.py:49` | high | create_widget 的 parent 参数缺类型注解 |
| AUD-12109 | Suggestion | process | `src/features/deliver/srt_exporter.py:64-74` | medium | fps 解析失败时静默回退 24.0，可能整体错位时间码且无日志 |
| AUD-1211 | Major | bug | `src/shared/gpu/gl_backend.py:47-49` | high | `create_shared_context` 调用 `moderngl.create_context()`，不产生共享上下文 |
| AUD-1212 | Suggestion | process | `src/shared/gpu/gl_backend.py:118-131` | high | `cleanup()` 三处裸 `except:` 连 BaseException 一并吞掉 |
| AUD-1213 | Suggestion | process | `src/shared/gpu/gl_backend.py:5` | high | 未使用的 `QOpenGLWidget` 导入 |
| AUD-1214 | Major↓ | risk | `src/shared/gpu/gl_context_hub.py:77-105` | medium | `_do_pixel_probe` 从未在 ctx_b 上做任何验证：无上下文切换、无线程守卫，探测结果取决于调用线程的隐含当前上下文，且 `tex.release()` 会在当前上下文删除纹理 |
| AUD-1215 | Major | bug | `src/shared/gpu/gl_context_hub.py:27-38, 41-60, 111-120` | medium | 单例与共享 dict 无任何同步保护 |
| AUD-1216 | Suggestion | process | `src/shared/gpu/gl_context_hub.py:32-43, 117-120` | medium | 注册的上下文持强引用且无 unregister，上下文销毁后 hub 残留死引用 |
| AUD-1217 | Suggestion | doc-gap | `src/shared/gpu/gl_pass_timer.py:25（对照 116-124）` | high | 模块 docstring 引用不存在的 `last_composite_ms` API |
| AUD-1218 | Suggestion | perf-opportunity | `src/shared/gpu/gl_pass_timer.py:61-68, 151-171` | medium | query `__enter__` 抛出时对象不释放（构造探测与每帧测量两条路径） |
| AUD-1219 | Suggestion | perf-opportunity | `src/shared/gpu/gl_pass_timer.py:152-156` | high | 每个 pass 每帧新建 query 对象，无池化 |
| AUD-12200 | Critical | bug | `src/features/deliver/ui/deliver_page.py:258-260、691` | medium | Custom Range 的 "To End" 哨兵值 -1 被 QSpinBox 钳位为 0，Custom 范围默认导出 0 帧 |
| AUD-12201 | Major | risk | `src/features/deliver/ui/deliver_page.py:1005、1038-1045、1160-1173、998-1000` | medium | `_start_export` 异常路径不重置 `_is_exporting`，一次启动失败后导出功能被永久锁死 |
| AUD-12202 | Major | bug | `src/features/deliver/ui/deliver_page.py:663-667` | medium | `set_timeline_sequences` 引用从未创建的 `_settings_panel`，宽泛 except 吞掉 AttributeError，P1-14 同步实为空操作 |
| AUD-12203 | Major | bug | `src/features/deliver/ui/deliver_page.py:825-837` | high | Source 从 Color/Composite 切回 Timeline 后，Custom Range 输入控件不再恢复显示 |
| AUD-12204 | Major | bug | `src/features/deliver/ui/deliver_page.py:729-733、894-909、411-413` | medium | 时长/大小估算被 output_path 必填校验挡死，且路径变化不触发刷新 |
| AUD-12205 | Major | risk | `src/features/deliver/ui/deliver_page.py:89-135、173` | medium | 页面销毁时无 ExportWorker 清理，导出进行中关闭页面/退出应用会触发 QThread 析构崩溃 |
| AUD-12206 | Suggestion | process | `src/features/deliver/ui/deliver_page.py:288` | high | 分辨率下拉项文案缺右括号 `"1280x720 (720p"` |
| AUD-12207 | Suggestion | process | `src/features/deliver/ui/deliver_page.py:803-807` | high | `_on_resolution_changed` 存在死变量及基于显示文本的 i18n 脆弱判断 |
| AUD-12208 | Suggestion | process | `src/features/deliver/ui/deliver_page.py:42、44-51、171` | high | 未使用导入与不完整类型注解 |
| AUD-12209 | Major | bug | `src/features/deliver/ui/deliver_workspace.py:45-47、526-535` | medium | `export_completed`/`export_cancelled` 信号声明后从未发射，`export_started` 在仅"加入队列"时即发射 |
| AUD-12210 | Suggestion | process | `src/features/deliver/ui/deliver_workspace.py:498-506` | high | `get_export_config` 宽泛 except 静默返回 None，无日志 |
| AUD-12211 | Suggestion | process | `src/features/deliver/ui/deliver_workspace.py:472-478` | high | `_last_restored_project_id` 未在 `__init__` 中声明，依赖 getattr 默认值 |
| AUD-12212 | Suggestion | process | `src/features/deliver/ui/deliver_workspace.py:542-554` | medium | `_on_open_output` 的 Popen 即发即弃，失败无反馈 |
| AUD-12213 | Suggestion | process | `src/features/deliver/ui/deliver_workspace.py:369-373` | medium | `detach_edit_panels` 在原始 dock 为 None 时共享 viewer 会失去父级且不再显示 |
| AUD-12214 | Major | bug | `src/features/deliver/ui/detachable_panel_host.py:135-140` | medium | `float_panel` 已浮动分支的布尔表达式优先级错误，`floating` 参数在指定 area 时恒被忽略 |
| AUD-12215 | Major | bug | `src/features/deliver/ui/detachable_panel_host.py:262-266` | medium | 注释声称 "关闭浮动 dock = 回到 splitter，由 workspace 监听 visibilityChanged 处理"，但该连接在全仓库不存在 |
| AUD-12216 | Suggestion | process | `src/features/deliver/ui/detachable_panel_host.py:236-237` | low | `restore_state` 的 singleShot lambda 持有 self，宿主在 tick 前销毁会对已删 QObject 调用方法 |
| AUD-12300 | Major | bug | `src/features/deliver/ui/video_batch_export_panel.py:1053` | medium | `_on_context_menu` 调用 `QScrollArea.indexAt()`（不存在的方法），且该槽从未被连接 |
| AUD-12301 | Suggestion | process | `src/features/deliver/ui/video_batch_export_panel.py:261` | medium | `VideoQueueDelegate` 全类死代码，且内部含不可达分支与不可靠 sizeHint |
| AUD-12302 | Suggestion | process | `src/features/deliver/ui/video_batch_export_panel.py:41` | medium | 未使用的导入与 logger |
| AUD-12303 | Suggestion | process | `src/features/deliver/ui/video_batch_export_panel.py:204` | high | `update_row` 越界回退用旧快照刷新自身，更新实际被丢弃 |
| AUD-12304 | Suggestion | process | `src/features/deliver/ui/video_batch_export_panel.py:957` | medium | 每次 `jobs_changed` 全量销毁并重建整个卡片栈，且对同一批控件双份 `deleteLater` |
| AUD-12305 | Suggestion | process | `src/features/deliver/ui/video_batch_export_panel.py:586` | medium | 每个进度 tick 与每次 resize 都重设样式表/全量刷新卡片 |
| AUD-12306 | Suggestion | process | `src/features/deliver/ui/video_batch_export_panel.py:400` | medium | 类型注解缺失/不一致：事件参数无注解、builtin generics 与 typing.List 混用、返回类型引用仅局部导入的类 |
| AUD-12307 | Suggestion | process | `src/features/deliver/ui/video_batch_export_panel.py:592` | medium | 百分比标签绑定 `isVisible()`，祖先未显示时会误判为隐藏 |
| AUD-12400 | Major | risk | `src/features/deliver/ui/video_export_settings_panel.py:2271（处理器定义）、:1444-1458（实际接线）` | medium | 纵横比锁定的宽/高联动处理器从未接线，锁定功能失效 |
| AUD-12401 | Major | bug | `src/features/deliver/ui/video_export_settings_panel.py:1667、:2359（回填）、:1938-1939（label 构建）` | medium | profile 按存储 id 用 setCurrentText 回填，大小写不匹配导致预设/任务的编码器 profile 静默丢失 |
| AUD-12402 | Major | bug | `src/features/deliver/ui/video_export_settings_panel.py:1784-1787` | medium | 自定义预设从不镜像进草稿，过期草稿反向覆盖自定义预设的设置 |
| AUD-12403 | Suggestion | process | `src/features/deliver/ui/video_export_settings_panel.py:2347-2391（镜像）vs :2609-2638（覆盖比较）` | medium | 预设镜像字段集小于覆盖比较字段集，fps/pix_fmt/alpha 策略/GIF 色数会被过期草稿顶掉 |
| AUD-12404 | Major | bug | `src/features/deliver/ui/video_export_settings_panel.py:1604-1632` | medium | 编辑态 job_id 回写到循环内所有任务，可产生重复 id 且编辑态跨 Add 不清理 |
| AUD-12405 | Major | bug | `src/features/deliver/ui/video_export_settings_panel.py:2855-2859、:2309-2316` | medium | 预检预览用 `except Exception` 吞掉领域错误，而 Add 路径完全不设防——同一错误前者静默、后者可中止进程 |
| AUD-12406 | Suggestion | process | `src/features/deliver/ui/video_export_settings_panel.py:1437、:1471` | high | Include audio 复选框重复连接 `_on_draft_changed`，每次切换双倍刷新 |
| AUD-12407 | Suggestion | process | `src/features/deliver/ui/video_export_settings_panel.py:791-804、:761-775` | medium | 磁盘预设操作只捕获 ValueError，delete 路径完全裸奔，OSError 在槽内未防护 |
| AUD-12408 | Suggestion | process | `src/features/deliver/ui/video_export_settings_panel.py:260、:267` | high | `_pix_fmt_display_name` 三元表达式两分支相同，非 422/444 格式显示错误兜底 |
| AUD-12409 | Suggestion | process | `src/features/deliver/ui/video_export_settings_panel.py:1686` | high | 自定义帧率用整数 QSpinBox 承载，回填非整数 fps 被截断 |
| AUD-12410 | Suggestion | process | `src/features/deliver/ui/video_export_settings_panel.py:1469-1470、:2302-2307、:2851-2866` | medium | 每个 textChanged 字符触发同步 build_jobs + 完整预检，无去抖 |
| AUD-12411 | Suggestion | process | `src/features/deliver/ui/video_export_settings_panel.py:2869、:186-187、:2840、:170、:2945` | medium | 未用/重复导入与缺失类型注解 |
| AUD-12600 | Critical | bug | `src/features/edit/adapters/fcpxml_adapter.py:68-80（另见 213-219、331）` | medium | frameDuration 帧率/帧时长语义颠倒：真实 FCPXML 导入时间全错，导出的 frameDuration 写成秒级错误值 |
| AUD-12601 | Critical | bug | `src/features/edit/adapters/fcpxml_adapter.py:100-106` | medium | `_frames_to_time` 分母多乘 `fps.den`：NTSC 帧率导出的 offset/duration 缩小约 1001 倍 |
| AUD-12602 | Major | bug | `src/features/edit/adapters/fcpxml_adapter.py:182, 220-221` | medium | import 第二次 `ET.parse` 与 `int()` 宽度转换无异常防护，外部数据可触发未捕获异常直达 Qt 槽（PyQt 未捕获槽异常会 qFatal 中止进程） |
| AUD-12603 | Major | bug | `src/features/edit/adapters/fcpxml_adapter.py:235-278, 291` | medium | 模块声称"必须支持"的 marker/transition/字幕导入完全未实现，且无任何 DROPPED 报告条目；多 sequence 静默丢弃 |
| AUD-12604 | Major | bug | `src/features/edit/adapters/fcpxml_adapter.py:238-239, 250-267` | medium | 悬空 `ref` 的 asset-clip 生成空 `asset_id` 的 clip，报告却标 PRESERVED |
| AUD-12605 | Major | bug | `src/features/edit/adapters/fcpxml_adapter.py:214-219` | high | `except Exception: pass` 吞掉 frameDuration 解析错误，静默回退 24fps 且无报告 |
| AUD-12606 | Major | bug | `src/features/edit/adapters/fcpxml_adapter.py:122, 182` | medium | 用标准库 ElementTree 解析不可信 XML，无实体膨胀/二次膨胀防护 |
| AUD-12607 | Major | bug | `src/features/edit/adapters/fcpxml_adapter.py:384-393, 401-405` | medium | 导出的 `<transition>` 缺 offset/effect 引用、`<marker>` 挂在 spine 上，不符合 FCPXML schema |
| AUD-12608 | Suggestion | doc-gap | `src/features/edit/adapters/fcpxml_adapter.py:83-97` | high | `_time_to_frames` 解析失败静默返回 0；`int()` 截断与 docstring 声称的 floor 不符（负值） |
| AUD-12609 | Suggestion | process | `src/features/edit/adapters/fcpxml_adapter.py:134-135, 52` | high | `supported = ... or version.startswith("1.")` 使版本白名单形同虚设 |
| AUD-12610 | Suggestion | process | `src/features/edit/adapters/fcpxml_adapter.py:176, 182` | high | `import_to_canonical` 对同一文件解析两次（inspect 一次 + 自己再 parse 一次） |
| AUD-12611 | Suggestion | process | `src/features/edit/adapters/fcpxml_adapter.py:195, 199` | medium | `has_video` 只查直接子元素 `media-rep`，而取 src 用 `.//media-rep`，两处口径不一致 |
| AUD-12612 | Suggestion | process | `src/features/edit/adapters/import_builder.py:226-233, 242-244` | medium | remap 注册 basename 键，同名不同目录素材存在错连风险 |
| AUD-12613 | Suggestion | risk | `src/features/edit/adapters/import_builder.py:139, 184-194` | medium | 导入落地与预览行收集在调用线程执行阻塞 IO/逐素材探测（生产为 UI 线程同步调用） |
| AUD-12614 | Suggestion | process | `src/features/edit/adapters/import_builder.py:186-194` | low | `probe(local)` 无异常防护，单个损坏素材可使整个导入失败 |
| AUD-12615 | Suggestion | process | `src/features/edit/adapters/import_builder.py:236, 239-250` | high | 内部回调/容器类型注解用 `Any` 弱化（timeline 类型明确可得） |
| AUD-12616 | Suggestion | process | `src/features/edit/adapters/interchange_types.py:99-100, 153-155` | medium | 协议与结果类型大量使用 `Any` 承载 timeline/sequence（可用具体类型或 TYPE_CHECKING 前置类型） |
| AUD-12617 | Major | bug | `src/features/edit/adapters/otio_adapter.py:116-125, 107` | medium | OTIO 导入 `timeline_start` 取自 clip 自身 duration，且忽略 RationalTime.rate：多 clip 全部错位堆叠、非 24fps 素材时长错误 |
| AUD-12618 | Major | bug | `src/features/edit/adapters/otio_adapter.py:105-106, 129-136, 138-144` | medium | 音频 clip 收集进 `a_clips` 后从不建轨：音频静默丢失，而报告条目却标 PRESERVED |
| AUD-12619 | Major | bug | `src/features/edit/adapters/otio_adapter.py:173-177` | high | OTIO 导出 `target_url=f"file:///{asset_id}"` 用内部 uuid 伪造 URL，任何第三方工具都无法重连媒体 |
| AUD-12620 | Suggestion | process | `src/features/edit/adapters/otio_adapter.py:121` | high | MissingReference 的 `target_url` 为 `None` 时 `getattr` 默认值不生效，asset_id 变成字符串 `"None"` |
| AUD-12621 | Suggestion | process | `src/features/edit/adapters/otio_adapter.py:165-181` | high | OTIO 导出静默丢弃音频轨/markers/transitions（无任何报告条目），写文件前也不建父目录（与 FCPXML 行为不一致） |
| AUD-12622 | Suggestion | process | `src/features/edit/adapters/otio_adapter.py:74` | high | `inspect` 的 `asset_count` 统计的是轨道数而非素材数 |
| AUD-12623 | Suggestion | process | `src/features/edit/adapters/otio_adapter.py:218-239` | medium | `flatten_nested_sequence` 无环检测（循环引用触发 RecursionError）、忽略嵌套 clip 的 source_start 裁剪、只处理 video_tracks |
| AUD-12700 | Suggestion | process | `src/features/edit/ai_editing/action_contract.py:185-189` | high | PostconditionResult.to_dict 丢弃 finding 的 observed 字段 |
| AUD-12701 | Major | bug | `src/features/edit/ai_editing/agent_inbox.py:163 与 189-204` | medium | 已 claim 的条目永久占用 per-run 容量，长 run 会拒绝新的用户转向输入 |
| AUD-12702 | Major | risk | `src/features/edit/ai_editing/agent_metrics.py:92 与 120-121、101-110、157-197` | medium | 指标收集器完全无锁，跨线程读写可致迭代崩溃与计数丢失 |
| AUD-12703 | Major | bug | `src/features/edit/ai_editing/agent_metrics.py:188-194（对照 agent_orchestrator.py:256-271）` | medium | partial 判定依赖的 payload 键从未被生产，PARTIAL_SUCCESS 永远统计为 succeeded |
| AUD-12704 | Suggestion | risk | `src/features/edit/ai_editing/agent_metrics.py:163-169` | high | `_trackers` 无淘汰，随进程生命周期无界增长 |
| AUD-12705 | Suggestion | process | `src/features/edit/ai_editing/agent_models.py:260、407、774、827、830` | high | from_dict 对持久化序列字段无类型校验，字符串会被静默拆成字符元组 |
| AUD-12706 | Suggestion | process | `src/features/edit/ai_editing/agent_models.py:44 与 742、760-777` | high | schema_version 只写不读，无迁移钩子 |
| AUD-12707 | Major | bug | `src/features/edit/ai_editing/agent_orchestrator.py:273-287（对照 agent_models.py:127）` | medium | fail() 声称支持任意非终态，但 PAUSED_FOR_RESUME 无 FAILED 出边，实际抛 ValueError |
| AUD-12708 | Major | risk | `src/features/edit/ai_editing/agent_orchestrator.py:70-77` | medium | runs/events/tasks/checkpoints 四张映射无任何淘汰路径，进程级无界内存增长 |
| AUD-12709 | Suggestion | process | `src/features/edit/ai_editing/agent_orchestrator.py:567-581` | high | restore_state 用裸 `except Exception: continue` 静默吞掉损坏条目，无任何日志 |
| AUD-12710 | Suggestion | process | `src/features/edit/ai_editing/agent_orchestrator.py:375-376、407-408、457-458` | medium | record_usage/stage_artifact/save_checkpoint 对终态 run 静默 no-op，调用方无法感知写入被丢弃 |
| AUD-12711 | Suggestion | process | `src/features/edit/ai_editing/agent_orchestrator.py:686-689` | high | _notify_all 以 `except Exception: pass` 吞掉订阅者异常，无诊断痕迹 |
| AUD-12712 | Suggestion | process | `src/features/edit/ai_editing/agent_phase_presenter.py:179-183` | high | RUN_CANCELLED 事件被呈现为非终态的"正在取消…"，事件驱动 UI 会停在取消中 |
| AUD-12800 | Major | risk | `src/features/edit/ai_editing/agent_task_executor.py:617-620（另见 279-440、399-400、427-429）` | medium | 取消/审批竞态下 orchestrator 状态转换异常逃逸 execute()，无顶层异常防护 |
| AUD-12801 | Suggestion | process | `src/features/edit/ai_editing/agent_task_executor.py:309-314、546-547、579-580` | high | 规划失败原因失真：allowlist/目录过滤与 JSON 解析失败混为一谈 |
| AUD-12802 | Suggestion | process | `src/features/edit/ai_editing/agent_task_executor.py:449-455` | high | `_gather_context` 直接调用 tool_facade.invoke，绕过 ToolInvocationRunner |
| AUD-12803 | Suggestion | process | `src/features/edit/ai_editing/agent_task_executor.py:161-172` | medium | AlwaysApproveBridge 哨兵字符串可被 facade 当作合法授权（安全脚枪） |
| AUD-12804 | Suggestion | process | `src/features/edit/ai_editing/agent_task_executor.py:678-714` | high | `_facade_to_step_result` 为死代码 |
| AUD-12805 | Suggestion | process | `src/features/edit/ai_editing/agent_tool_loop.py:235` | medium | assistant envelope 构建对非 dict arguments 无防御，异常会逃出 run() |
| AUD-12806 | Suggestion | process | `src/features/edit/ai_editing/agent_tool_loop.py:201、248-250` | high | 陈旧的 final_text 被写入 assistant tool_calls envelope |
| AUD-12807 | Suggestion | process | `src/features/edit/ai_editing/analysis_models.py:78-79、118-119（对比 OcrDetection:233-239、OcrBox:199-203）` | medium | WordSegment/TranscriptSegment 缺少帧序与置信度校验，倒置区间被静默钳制 |
| AUD-12900 | Suggestion | process | `src/features/edit/ai_editing/analysis_request.py:238-251` | medium | `with_confirmation` / `with_state` 缺状态机守卫，可将终态/已取消请求转回 CONFIRMED |
| AUD-12901 | Major | risk | `src/features/edit/ai_editing/analysis_scheduler.py:351-356, 397-399` | medium | `shutdown()` 无锁清空 executor 且 `cancel_futures=True` 丢弃在途 work：job 永久 RUNNING、后续 submit 抛未捕获 RuntimeError |
| AUD-12902 | Suggestion | process | `src/features/edit/ai_editing/analysis_scheduler.py:209-224, 375-389` | medium | 出队完全依赖外部 `notify_finished`，`submit()` 不主动 drain pending |
| AUD-12903 | Major | bug | `src/features/edit/ai_editing/analysis_service.py:432-472` | medium | 调度路径的 work 闭包从不调用 `notify_finished`、从不终结 asset_job：容量被永久占用，后续分析任务全部停滞 |
| AUD-12904 | Major | risk | `src/features/edit/ai_editing/analysis_service.py:189-197, 519-533, 502-507, 597-603` | medium | AnalysisService 共享状态无锁：worker 线程写 `_records`/`_embeddings`/`_requests`，UI 线程并发枚举可抛 RuntimeError |
| AUD-12905 | Major | bug | `src/features/edit/ai_editing/analysis_service.py:418-456` | medium | `_execute_via_scheduler` 的完成计数在并发 worker 间非原子，丢失更新会使 request 永久卡 RUNNING |
| AUD-12906 | Major | bug | `src/features/edit/ai_editing/analysis_service.py:287-390` | medium | `AnalysisBudget`（时长/成本/素材数上限）在执行路径完全未强制执行 |
| AUD-12907 | Suggestion | perf-opportunity | `src/features/edit/ai_editing/analysis_service.py:813-827` | medium | `search_assets` 文本过滤对每个 segment 新建 `SemanticIndex`：O(n) 重对象构建/段 |
| AUD-12908 | Suggestion | process | `src/features/edit/ai_editing/analysis_service.py:287-326（对照 analysis_request.py:188）` | medium | `project_revision` stale 判定未实现：工程改动后 request 并不失效 |
| AUD-12909 | Major | bug | `src/features/edit/ai_editing/approval_service.py:79-97` | medium | `ApprovalScope.matches` 单侧 None 绕过：签发方绑定的 diff_hash/plan_hash 等强约束在 expected 侧缺省时不校验 |
| AUD-12910 | Suggestion | process | `src/features/edit/ai_editing/approval_service.py:186-189, 268-279` | medium | `validate` 用宽泛 `except Exception` 掩盖错误；`time_fn: Any` 注解过宽 |
| AUD-12911 | Suggestion | process | `src/features/edit/ai_editing/artifact_models.py:174-189（及其余三类同构代码）` | medium | 四个容器 artifact 只有 `to_dict` 无 `from_dict`：序列化不对称，持久化无法往返 |
| AUD-12912 | Suggestion | process | `src/features/edit/ai_editing/artifact_models.py:26-27` | high | 死导入：`import time` 与 `field` 未使用 |
| AUD-12913 | Suggestion | process | `src/features/edit/ai_editing/artifact_models.py:98-108, 161-165` | medium | 元素级 `from_dict` 不做类型收敛/校验；容器不校验 kind 与类一致 |
| AUD-12914 | Suggestion | process | `src/features/edit/ai_editing/artifact_store.py:162-180` | medium | `_merge_patch` 白名单过宽：模型 patch 可写 `model_id`/`brief_revision`/`user_edited`，集合字段可为任意 list/dict |
| AUD-12915 | Suggestion | process | `src/features/edit/ai_editing/artifact_store.py:179` | high | patch 合并重写 `created_at`：创建时间被当作更新时间覆盖，且时间戳为 str 与全库 float 惯例不一致 |
| AUD-1300 | Major | risk | `src/shared/gpu/gl_resource_registry.py:127（配合 132-141）` | medium | release_fn 抛异常时台账条目已被 pop，泄漏资源从登记表中消失且无法重试 |
| AUD-13001 | Major | bug | `src/features/edit/ai_editing/assistant_capability_router.py:360-366` | medium | `_active_profile` 用宽泛 `except Exception` 吞掉 ProfileRegistry 异常，把真实错误伪装成"未配置 Profile" |
| AUD-13002 | Suggestion | process | `src/features/edit/ai_editing/assistant_capability_router.py:56-65` | high | 模块中部 import（函数定义之后才导入 provider_profiles），违反 PEP 8 布局 |
| AUD-13003 | Suggestion | process | `src/features/edit/ai_editing/assistant_events.py:189（契约声明 :3-5）` | high | `AssistantStateSnapshot.diff_summary` 使用 `dict[str, Any]`，违反本模块"禁止 dict[str, Any] 跨层传播"的自述契约 |
| AUD-13004 | Suggestion | process | `src/features/edit/ai_editing/assistant_modes.py:71-73` | high | 兼容常量在 import 时固化插件清单，`reload_assistant_plugins()` 后过期 |
| AUD-13005 | Suggestion | process | `src/features/edit/ai_editing/assistant_modes.py:51, 63-65` | high | import 即触发磁盘扫描（经 get_assistant_plugin_registry → 注册表构造 + reload） |
| AUD-13006 | Suggestion | process | `src/features/edit/ai_editing/assistant_plugins.py:408-409` | high | `unverified` 信任标志由插件自报的 `source` 字段直接推导，清单可伪造"已验证" |
| AUD-13007 | Suggestion | process | `src/features/edit/ai_editing/assistant_plugins.py:302-309` | high | `handler` 字段无已知处理器白名单校验，未知 handler 延迟到用户发起请求时才报错 |
| AUD-13008 | Suggestion | process | `src/features/edit/ai_editing/assistant_plugins.py:413` | high | 模块 import 即构造默认注册表并扫描磁盘 |
| AUD-13009 | Major | bug | `src/features/edit/ai_editing/assistant_service.py:191-192（属性）；:115-169（__init__ 无` | medium | `config` 属性在 `start_session()` 之前访问抛 AttributeError：`__init__` 从未初始化 `_config` |
| AUD-1301 | Suggestion | process | `src/shared/gpu/gl_resource_registry.py:74、87` | high | register 覆盖同名条目时静默丢弃旧 release_fn，旧资源成孤儿 |
| AUD-13010 | Major | bug | `src/features/edit/ai_editing/assistant_service.py:984-992` | high | `cancel()` 用 `except Exception: pass` 吞掉 runtime.cancel 失败，状态机进入 CANCELLED 但下游 run 可能仍在执行 |
| AUD-13011 | Major | bug | `src/features/edit/ai_editing/assistant_service.py:998-1006（吞错）；:616-623（` | medium | `_current_timeline_revision` 宽泛 `except: pass` 把快照失败静默降级为 base_revision=0，plan 以错误基线编译 |
| AUD-13012 | Suggestion | process | `src/features/edit/ai_editing/assistant_service.py:635-650` | medium | `plan_variants` 失败时不清空旧 variants，FAILED 快照仍展示并允许选取上一轮 stale variant |
| AUD-13013 | Suggestion | process | `src/features/edit/ai_editing/assistant_service.py:373-381` | high | `set_active_run_id` 不清理旧 runtime：新 run 未带 runtime 时 cancel 会用旧 runtime 打新 run_id |
| AUD-13014 | Suggestion | process | `src/features/edit/ai_editing/assistant_service.py:451-469（append_chat_message 的 role 无白名单）；:531-549（submit_request 空 text 直入 GATHERING_CONTEXT）；:843-849（select_variant 未知 id 仍切 AWAITING_VARIANT_APPROVAL）` | medium | 动作型入口缺少输入验证：空文本、未知 variant_id、任意 role 字符串均被接受 |
| AUD-13015 | Suggestion | process | `src/features/edit/ai_editing/assistant_service.py:584` | high | `preview_brief_patch` 跨模块访问 BriefService 私有方法 `_merge` |
| AUD-13016 | Suggestion | process | `src/features/edit/ai_editing/assistant_service.py:363, 659-661` | high | 类型注解不精确：`_resolve_job_cards` 返回裸 `tuple`；`run_workflow` 的 cancel_requested/emit_node_event 标注为 `Any` |
| AUD-13017 | Major | bug | `src/features/edit/ai_editing/assistant_session_store.py:162-163` | high | `persist()` 的 `mkdir` 未纳入异常保护：只读工程目录直接抛 OSError 而非按契约返回 False |
| AUD-13018 | Major | risk | `src/features/edit/ai_editing/assistant_session_store.py:198-228（load 两处 except 仅含 OSError/json.JSONDecodeError/ValueError）；:325-328（restore_catalog 无任何保护）` | medium | `load()`/`restore_catalog` 的异常捕获缺少 TypeError/AttributeError，损坏的 ai/*.json 可使工程打开崩溃 |
| AUD-13019 | Major | bug | `src/features/edit/ai_editing/assistant_session_store.py:356-368（改名）；:300-313（_sync_catalog_to_cache 仅在 catalog 有非空会话时才写缓存）` | medium | `set_session_title` 对"仅存在于本机缓存"的会话改名后不落盘：返回 True 但重启即丢 |
| AUD-1302 | Suggestion | process | `src/shared/gpu/gl_resource_registry.py:146-150` | high | release_all 的组名快照与逐组释放之间存在窗口，并发 register 的新组会被本次遍历遗漏 |
| AUD-13020 | Major | risk | `src/features/edit/ai_editing/assistant_session_store.py:100-105（默认路径 + 构造时各自 _load_cache 内存快照）；:257-276（_save_cache 全量重写，固定 .tmp 名，无文件锁）` | medium | 本机缓存为共享单文件、整文件重写且无锁：多实例并发写互相覆盖（last-writer-wins 丢会话） |
| AUD-13021 | Suggestion | process | `src/features/edit/ai_editing/assistant_session_store.py:230-233（_touch）；:413-414 / :426-427 / :441-442（重复调用）` | medium | `add_message_summary`/`record_run`/`add_compression_checkpoint` 每次触发两轮全量缓存序列化+落盘（`_touch` 内已含 `_sync_catalog_to_cache`，方 |
| AUD-13022 | Suggestion | process | `src/features/edit/ai_editing/assistant_session_store.py:278-289` | medium | `clear_history` 只清内存并写空缓存，工程内 session_catalog.json 磁盘文件未重写，未保存工程前旧会话可复活 |
| AUD-13023 | Suggestion | process | `src/features/edit/ai_editing/audio_extractor.py:186-216（PyAV 路径）；:258-259（CLI 路径）` | medium | 整段 PCM 缓冲在内存（PyAV bytearray 全量累计 + CLI capture_output 全量 stdout），长素材内存峰值大 |
| AUD-13024 | Suggestion | process | `src/features/edit/ai_editing/audio_extractor.py:285-295` | high | `_resolve_ffmpeg` 用 `os.path.isfile("ffmpeg")` 接受 CWD 相对命中，打包环境下可能绑到错误二进制 |
| AUD-13025 | Suggestion | process | `src/features/edit/ai_editing/audio_extractor.py:163-173` | medium | PyAV 路径仅 ImportError 触发 CLI 回退：半损坏的 PyAV（缺 API，AttributeError）被翻译成"解码失败"而不再尝试 CLI |
| AUD-1310 | Suggestion | process | `src/shared/gpu/gpu_scope_engine.py:145-153（缓存写入）、226-235（唯一释放点）、273-281（VAO 缓存）` | medium | `_pool_cache` / `_points_vao_cache` 按尺寸键缓存且永不淘汰，所有历史尺寸的资源常驻至 release() |
| AUD-13100 | Suggestion | risk | `src/features/edit/ai_editing/audit_store.py:100-137` | medium | AuditStore 全程无锁，与同包 mission/checkpoint store 的线程安全约定不一致 |
| AUD-13101 | Suggestion | process | `src/features/edit/ai_editing/audit_store.py:114-128` | medium | record 的宽泛 `except Exception` 会把 JSON 序列化失败误标为"路径越权"，且 safe_filename 碰撞时静默丢审计文件 |
| AUD-13102 | Major | bug | `src/features/edit/ai_editing/autonomous_agent.py:287-293, 324-342` | medium | rollback_ai_command 两次调用均未包裹异常处理，破坏 apply "总是返回结构化结果"的契约 |
| AUD-13103 | Major | bug | `src/features/edit/ai_editing/autonomous_agent.py:378-389` | medium | 预览-提交一致性门禁过弱：仅比较 plan_id 与 operation_count，未比较可用的 operation_summaries |
| AUD-13104 | Suggestion | risk | `src/features/edit/ai_editing/autonomous_agent.py:250-264` | high | 紧急停用分支中 revoke 自身抛异常会从 apply 逃逸且 grant 泄漏 |
| AUD-13105 | Suggestion | process | `src/features/edit/ai_editing/autonomous_agent.py:268-274` | medium | 跨模块导入另一个模块的私有符号 `_expected_commit_scope` |
| AUD-13106 | Suggestion | process | `src/features/edit/ai_editing/autonomous_agent.py:149-156, 391-399` | medium | 空选择时素材范围检查被整体跳过；_asset_ids 只识别单数键 `asset_id` |
| AUD-13107 | Suggestion | process | `src/features/edit/ai_editing/autonomous_mission.py:122-130` | high | start() 对同 run_id 不同 plan_id 的 mission 静默覆盖，旧状态无提示丢失 |
| AUD-13108 | Suggestion | process | `src/features/edit/ai_editing/autonomous_recovery.py:110-149` | medium | mark()/upsert() 不校验状态转移方向，APPLIED 可被改回 PENDING 重新进入恢复队列 |
| AUD-13109 | Major | bug | `src/features/edit/ai_editing/brief_service.py:290-299, 313-319` | medium | propose_patch_from_text 的 text_lower 为死变量：英文关键词匹配用原始 user_text，大小写敏感导致推断失败 |
| AUD-1311 | Suggestion | process | `src/shared/gpu/gpu_scope_engine.py:626-642` | medium | wf_display 纹理被 waveform/parade/luma 三种输出复用，正确性依赖 scope_types 模式互斥这一跨模块不变量 |
| AUD-13110 | Suggestion | process | `src/features/edit/ai_editing/brief_service.py:149-166` | medium | _merge 对外部 patch changes 的类型校验缺失：dict 形式 target_duration 缺键抛裸 KeyError，字符串被 tuple() 拆成单字符 |
| AUD-13111 | Suggestion | process | `src/features/edit/ai_editing/capability_catalog.py:206-215` | medium | missing_evidence_for 对未声明工具返回 ()（无缺失证据），与模块 fail-closed 声明相悖 |
| AUD-13112 | Suggestion | doc-gap | `src/features/edit/ai_editing/capability_catalog.py:180-189` | medium | remove() 在 owner_plugin_id 为空时可删除任何条目（含核心条目），违背自身 docstring |
| AUD-13113 | Suggestion | process | `src/features/edit/ai_editing/capability_catalog.py:174-178, 61-67` | medium | register 的关键字参数 replace 遮蔽模块级 dataclasses.replace；CompensationAction.strategy 无枚举校验 |
| AUD-13114 | Major | bug | `src/features/edit/ai_editing/capability_manifest.py:236-239, 329-334` | medium | build_manifest 两处宽泛 except 静默吞错且全程无日志：registry/编解码器读取失败时 manifest 静默失去整个能力面 |
| AUD-13115 | Suggestion | process | `src/features/edit/ai_editing/capability_manifest.py:254-291` | medium | 效果循环内重复局部 import，且 plugin/非 plugin 两个分支代码重复仅 fallback 不同 |
| AUD-13116 | Suggestion | process | `src/features/edit/ai_editing/capability_status.py:123-125, 332-340` | medium | _provider_is_available 对未实现 is_available 的 provider 默认 True（fail-open），生成能力可能被标 AVAILABLE 而实际不可达 |
| AUD-13117 | Suggestion | process | `src/features/edit/ai_editing/capability_status.py:355-370` | medium | agent_auto_mode/automatic_export/cloud_upload 仅凭 feature flag 开启即标 AVAILABLE，未验证对应子系统真实就绪 |
| AUD-1312 | Suggestion | process | `src/shared/gpu/gpu_scope_engine.py:324-328（写入）、464-466（仅 parade 部分恢复）` | medium | 各 pass 修改 ctx.viewport 后不恢复进入前取值，GL 全局状态外泄给调用方 |
| AUD-1313 | Suggestion | process | `src/shared/gpu/gpu_scope_engine.py:44-46、367-368（佐证：gpu_scope.glsl:46，属其它批次文件）` | medium | SCOPE_RANGE_MIN/MAX 与采样着色器中硬编码的 clamp(-0.1, 1.2) 跨文件重复维护，且 HDR >1.2 值在采样阶段即被压平 |
| AUD-1314 | Suggestion | process | `src/shared/gpu/gpu_scope_engine.py:267-268、333-339、600-601` | medium | 部分公开/内部方法缺参数与返回类型注解 |
| AUD-1315 | Suggestion | process | `src/shared/gpu/gpu_scope_engine.py:161-163、178-180、230-233、239-242、245-248、251-254、258-260` | high | release/换新路径的 `except Exception: pass` 共 7 处且零日志，销毁期 GL 错误完全不可见 |
| AUD-1320 | Suggestion | process | `src/shared/gpu/host_gpu_info.py:83` | medium | metal_info_from_gpu 直接下标访问 gpu["devices"]，缺键时抛 KeyError，违背模块"失败路径都返回稳定 reason_code"的承诺 |
| AUD-13201 | Major | bug | `src/features/edit/ai_editing/chat_run_driver.py:317-318` | medium | 工具循环失败被无日志的宽泛 `except Exception` 静默降级，掩盖领域错误 |
| AUD-13202 | Major | bug | `src/features/edit/ai_editing/chat_run_driver.py:328-357（循环）、262（估算点）、395-399（用量落账）` | medium | inbox 追问循环不断追加消息但不重估窗口预算，多轮请求用量也未累计 |
| AUD-13203 | Suggestion | process | `src/features/edit/ai_editing/chat_run_driver.py:545（对照 626-627）` | medium | `max_tool_calls` 被当作 `max_steps` 传入执行器，预算语义混用且默认值不一致 |
| AUD-13204 | Suggestion | process | `src/features/edit/ai_editing/chat_run_driver.py:220-224` | medium | agent 模式在无 ToolFacade 时静默退化为普通聊天，无任何标记 |
| AUD-13210 | Major | bug | `src/features/edit/ai_editing/commercial_gate.py:62-66` | medium | `entries` 为显式 null 时导出门禁抛 TypeError 而非 fail closed |
| AUD-13220 | Suggestion | process | `src/features/edit/ai_editing/composition_plugin.py:62-69（同型逻辑另见 150-163、248-259）` | medium | 三个宿主的 register() 激活失败路径遗留孤儿插件记录，同 ID 无法再注册 |
| AUD-13221 | Suggestion | risk | `src/features/edit/ai_editing/composition_plugin.py:275-277` | high | `SubagentPluginHost._child_agents` 只增不减，长会话内存无界增长 |
| AUD-13230 | Suggestion | process | `src/features/edit/ai_editing/context_budget.py:194-205、208-222` | medium | 压缩阈值与超限判定的口径不一致：比率不含 reserved_output，存在「已超限但既不告警也不压缩」区间 |
| AUD-13240 | Major | risk | `src/features/edit/ai_editing/context_service.py:330-338` | medium | 工具 manifest 块假设每个条目都是 dict，畸形第三方工具定义会使整个上下文构建崩溃 |
| AUD-13241 | Suggestion | process | `src/features/edit/ai_editing/context_service.py:289-295` | medium | 压缩摘要块 token 估算 `len//4` 与项目估算器的 CJK 系数矛盾，系统性低估 |
| AUD-13242 | Suggestion | process | `src/features/edit/ai_editing/context_service.py:303-307` | high | 注释声称 summary 已脱敏，实际 compress() 存入的是原始拼接文本 |
| AUD-13243 | Suggestion | process | `src/features/edit/ai_editing/context_service.py:239-244（另见 115、452）` | medium | 模块中部/函数内重复导入，`"history"` 字面量未用已导入常量 |
| AUD-13250 | Suggestion | process | `src/features/edit/ai_editing/context_spill.py:63-67（对照 115-117）` | medium | `from_dict` 截断 spill_id 至 64 字符，与 `get()` 的全 ID 匹配不一致 |
| AUD-13300 | Major | bug | `src/features/edit/ai_editing/creative_assets.py:90-91, 123-130` | medium | register_package 可静默覆盖已注册 package，绕过 update_package 的影响报告契约 |
| AUD-13301 | Suggestion | process | `src/features/edit/ai_editing/creative_assets.py:30-32, 56` | high | ReferencePackage.to_dict 的 fingerprint 空串分支是死代码 |
| AUD-13302 | Major | bug | `src/features/edit/ai_editing/cross_domain.py:99-112（dirty_clip_ids 同型 118-127）` | medium | DependencyGraph 每域只保留最后一个 observed ref，同域多 artifact 时 stale 漏报且依赖输入顺序 |
| AUD-13303 | Major | bug | `src/features/edit/ai_editing/cross_domain.py:176-185` | medium | RenderContract.is_consistent_with 忽略 effects_enabled/proxy_tier/time_range，预览关特效与导出开特效被判"一致" |
| AUD-13304 | Suggestion | process | `src/features/edit/ai_editing/cross_domain.py:288-299` | medium | CrossDomainScorecard.weights 未验证非负，负权重可令加权分失真 |
| AUD-13305 | Major | perf-opportunity | `src/features/edit/ai_editing/cross_domain_cost.py:136-147` | medium | predict_recompute_cost 把 full_recompute 按 1 个 clip 计费，全量重算成本被系统性低估 |
| AUD-13306 | Suggestion | process | `src/features/edit/ai_editing/cross_domain_cost.py:147-160` | high | dirty 为空时 can_skip=True 但 estimated_cost_units 仍 >0，报告自相矛盾 |
| AUD-13307 | Suggestion | process | `src/features/edit/ai_editing/cross_domain_cost.py:215-228, 248-255, 284-291` | medium | validate_brief_against_delivery 的 platform 参数从未使用；duration_seconds 未验证有限/非负，NaN 可绕过时长上限 |
| AUD-13308 | Major | bug | `src/features/edit/ai_editing/decision_service.py:47-63` | medium | accept/reject/lock 无状态迁移校验，LOCKED 决策可被 reject 覆盖，违反 TAEP §5.5 关键规则 |
| AUD-13309 | Suggestion | process | `src/features/edit/ai_editing/decision_service.py:72-77, 109-115` | medium | 已 ACCEPTED 的决策被用户 override 后从 must_preserve 中消失 |
| AUD-13310 | Suggestion | process | `src/features/edit/ai_editing/delivery_analyzer.py:171-174, 313-314, 359-360` | medium | 三处宽泛 except Exception 丢弃根因，错误码之外无任何日志 |
| AUD-13311 | Suggestion | process | `src/features/edit/ai_editing/delivery_analyzer.py:302-305, 347-358` | medium | 音频整段 PCM 常驻内存，长时长交付物内存峰值偏高 |
| AUD-13312 | Major | risk | `src/features/edit/ai_editing/delivery_contracts.py:227-231` | medium | validate_platform_delivery 对标量/畸形 resolution 直接下标，外部 metadata 可使校验崩溃 |
| AUD-13313 | Major | bug | `src/features/edit/ai_editing/delivery_contracts.py:141-162` | medium | _fps_matches 用精确 Fraction 相等比较，十进制 NTSC 帧率（29.97/23.976）永远匹配不上分数规格 |
| AUD-13314 | Suggestion | process | `src/features/edit/ai_editing/delivery_contracts.py:65-68, 86-88, 133-138` | medium | PlatformDeliverySpec.from_dict 对畸形外部 JSON 抛裸 TypeError/ValueError |
| AUD-13315 | Suggestion | process | `src/features/edit/ai_editing/delivery_contracts.py:504-515` | medium | validate_brand_elements 同 element_id 多条测量时取第一条，顺序敏感且不报告歧义 |
| AUD-13316 | Suggestion | process | `src/features/edit/ai_editing/delivery_contracts.py:437-444` | medium | _reading_units 的 CJK 范围不含假名/韩文，非中文 CJK 字幕阅读速度被低估 |
| AUD-13317 | Suggestion | process | `src/features/edit/ai_editing/delivery_quality_gate.py:40-42, 119-127, 126-127` | medium | "未测量"（默认 0/None）与"测量为 0"不可区分，缺失测量默认放行 |
| AUD-13400 | Major | doc-gap | `src/features/edit/ai_editing/diagnostics.py:129-138` | medium | `Diagnostic.from_dict` 将未知 severity/code 静默降级为 INFO/INTERNAL_ERROR，与 docstring 承诺的"回落到原始字符串"不符 |
| AUD-13401 | Major | bug | `src/features/edit/ai_editing/edit_api.py:335-343` | medium | `_host_approval_service` 用宽泛 `except Exception` 吞掉宿主异常，把宿主缺陷伪装成授权失败 |
| AUD-13402 | Major | bug | `src/features/edit/ai_editing/edit_api.py:361-378（request_export）、125-137（PathGrant 定义）` | medium | TAEP §8"输出不允许静默覆盖"未强制执行：`PathGrant.allow_overwrite` 全库无消费者 |
| AUD-13403 | Suggestion | process | `src/features/edit/ai_editing/edit_api.py:110-117 与 236-237、244-246、255-257` | medium | `EditSessionHost` Protocol 把 composite 方法声明为必需成员，运行时却按可选 getattr 探测——类型契约与实际用法相反 |
| AUD-13404 | Suggestion | process | `src/features/edit/ai_editing/edit_api.py:309-333` | high | `apply_with_confirmation` 失败分支重复调用 `get_state()`，new_project/new_sequences 可能来自不同瞬时状态 |
| AUD-13405 | Suggestion | risk | `src/features/edit/ai_editing/edit_capability_plugin.py:45-48 与 62-66` | medium | `EditCapabilityDescriptor.display_name` 未做类型归一化，非字符串值可通过 `__post_init__` 校验后在 `to_dict` 崩溃 |
| AUD-13406 | Major | doc-gap | `src/features/edit/ai_editing/edit_project_service.py:84-88（契约）与 204-208（实现）` | medium | `CommitResult` docstring 承诺 revision 冲突返回 `applied=False`，`commit_command` 实际 raise `RevisionConflict`——公开"统一写入入口"的错误通道与 |
| AUD-13407 | Suggestion | process | `src/features/edit/ai_editing/edit_project_service.py:342-350（对照 commit_command 的 227-235）` | medium | `preview_command` 的 `validate_timeline` 未包异常保护，与 commit 路径对同一调用的处理不一致 |
| AUD-13408 | Suggestion | process | `src/features/edit/ai_editing/editorial_advanced.py:18-19` | high | `hashlib`、`json` 导入未使用 |
| AUD-13409 | Suggestion | process | `src/features/edit/ai_editing/editorial_advanced.py:96-108、116-125（对照 89-93、173-175）` | medium | `ActiveAnglePlan`、`BeatGrid` 的 confidence 未做 [0,1] 校验，与同类模型不一致 |
| AUD-13410 | Suggestion | process | `src/features/edit/ai_editing/editorial_advanced.py:265-284` | medium | `detect_conflicts` 对重复 clip_id 生成重复 conflict_id，且 ai_plan_ref/human_change 为占位空值 |
| AUD-13411 | Suggestion | process | `src/features/edit/ai_editing/editorial_models.py:553-562（UserOverride）、644-658（SemanticSegment）、288-316（EditorialBrief.safe_area_profile）、455-463（VariantScores）` | medium | 多个 `from_dict` 直通字段未做 str 归一化/范围校验，往返后类型不变量可被破坏 |
| AUD-13412 | Suggestion | process | `src/features/edit/ai_editing/editorial_quality.py:272-280` | high | `audio_continuity` 检查无条件记入 `checks_run`，通过时却不产生 finding——与其他检查的记录语义不一致 |
| AUD-13413 | Suggestion | process | `src/features/edit/ai_editing/editorial_quality.py:40-47` | medium | `UsableRange.recommendation` 注释声明枚举集合但未校验取值 |
| AUD-13414 | Suggestion | process | `src/features/edit/ai_editing/eval_runner.py:140-141 与 189-190` | medium | `require_quality_gate=False` 只容忍 gate 缺失、不豁免已失败的 gate——策略开关语义与实际行为相悖 |
| AUD-13500 | Critical | bug | `src/features/edit/ai_editing/export_service.py:765` | medium | `complete_host_export` 引用未定义名 `subtitle_duration_frames`，每次调用必抛 NameError，AI 导出完成链路整体断裂 |
| AUD-13501 | Major | bug | `src/features/edit/ai_editing/execution_policy.py:299-305 与 368-370` | medium | `restricted_auto` tier 覆盖会取消 AUTO_PUBLISH 的"独立同意边界"，自动发布可在无审批下放行 |
| AUD-13502 | Major | bug | `src/features/edit/ai_editing/generated_media_materializer.py:182-186（配合 162-163 的前置校验缺口）` | medium | 恶意/畸形 `Content-Length: 0` 头导致 `ZeroDivisionError`，逃逸下载错误边界 |
| AUD-13503 | Suggestion | process | `src/features/edit/ai_editing/generated_media_materializer.py:115-119, 137-160` | medium | 物化下载无 host allow-list，provider 句柄可指向任意 http(s) 主机 |
| AUD-13504 | Suggestion | perf-opportunity | `src/features/edit/ai_editing/generated_media_materializer.py:141-149, 218-244` | low | 缓存命中仍全文重算 sha256，每次预览 O(文件大小) |
| AUD-13505 | Suggestion | risk | `src/features/edit/ai_editing/generation_bin.py:116-118, 160-176` | medium | `GenerationBin` 无锁也无线程契约说明，而同包存在后台物化线程回调入口 |
| AUD-13506 | Suggestion | process | `src/features/edit/ai_editing/generation_bin.py:33` | high | 未使用的导入 `Callable` |
| AUD-13507 | Suggestion | process | `src/features/edit/ai_editing/eval_trace.py:306-309` | low | `clear()` 把 run_id 计数器归零，删除后再评估会产生与已导出轨迹相同的 run_id |
| AUD-13508 | Suggestion | doc-gap | `src/features/edit/ai_editing/evidence_kinds.py:179-192` | medium | `fingerprint()` 实际未纳入 `selection`，与 docstring 相悖，选择状态变化不会被 stale 检测捕获 |
| AUD-13509 | Suggestion | process | `src/features/edit/ai_editing/evidence_kinds.py:194-201` | medium | `stale_domains` 只报告双侧同名域的指纹差异，单侧新增/删除的域永远不被视为 stale |
| AUD-13510 | Suggestion | process | `src/features/edit/ai_editing/evidence_policy.py:44-58` | medium | `known_refs` 传集合形式时 selection-scope 校验静默失效（no-op 而非报错） |
| AUD-13511 | Suggestion | process | `src/features/edit/ai_editing/feedback_store.py:131-134, 149` | medium | 脱敏边界不一致：`from_dict` 不经 `sanitize_for_storage`，`to_dict` 只洗 `requested_changes` 不洗 `note` |
| AUD-13512 | Suggestion | process | `src/features/edit/ai_editing/feedback_store.py:253-260` | medium | `latest()` 按插入序取末位，忽略 `created_at`（`create()` 支持显式传入该值） |
| AUD-13513 | Suggestion | process | `src/features/edit/ai_editing/export_service.py:485-489` | low | `request_preview` 未校验 `clip_range.start` 落在序列范围内 |
| AUD-13514 | Suggestion | process | `src/features/edit/ai_editing/export_service.py:833-836` | low | manifest 脱敏只覆盖 `/Users/` 与 `/private/` 前缀，非 macOS 本机路径仍会写入交付 manifest |
| AUD-13515 | Suggestion | process | `src/features/edit/ai_editing/export_service.py:256-260, 356-362` | medium | `run_export_preflight` / `build_export_config` 关键参数与返回值缺类型注解 |
| AUD-13600 | Major | bug | `src/features/edit/ai_editing/generation_provider_pack.py:550` | medium | _normalize_result 对外部 payload 数值字段直接 float()，非数值内容抛 ValueError 穿透 poll/generate |
| AUD-13601 | Major | bug | `src/features/edit/ai_editing/http_generation_provider.py:374` | medium | 非法 Content-Length 头使 int() 抛 ValueError 穿透类型化错误契约 |
| AUD-13602 | Major | bug | `src/features/edit/ai_editing/http_generation_provider.py:204` | medium | _last_remote_job_id 为实例级共享状态，共享 worker 并发 generate 时互相覆盖，取消可能指向错误任务 |
| AUD-13603 | Major | bug | `src/features/edit/ai_editing/goal_supervisor.py:334` | medium | run() 未捕获 GoalRevisionConflict，与 record_cycle 的冲突处理不对称 |
| AUD-13604 | Suggestion | process | `src/features/edit/ai_editing/generation_provider_pack.py:371` | medium | SeedanceVideoProviderPack.poll/cancel 对未知 job_id 抛裸 KeyError，与 ContractMock 错误契约不一致 |
| AUD-13605 | Suggestion | process | `src/features/edit/ai_editing/generation_provider_pack.py:515` | medium | _normalize_result 形参注解只写 GenerationRequest，实际还会接收 _ResumableRequest |
| AUD-13606 | Suggestion | process | `src/features/edit/ai_editing/generation_provider_pack.py:427` | medium | 远端已取消的任务在 adapter 终态回退中被误标为 GEN_PROVIDER_FAILED |
| AUD-13607 | Suggestion | risk | `src/features/edit/ai_editing/generation_provider_pack.py:352` | high | SeedanceVideoProviderPack 的 _jobs/_requests 字典无锁，并发 poll/cancel 有竞态 |
| AUD-13608 | Suggestion | risk | `src/features/edit/ai_editing/generation_provider.py:109` | medium | ProviderRegistry 无锁，注册与查找可跨线程竞争 |
| AUD-13609 | Suggestion | process | `src/features/edit/ai_editing/goal_round_driver.py:98` | high | execute() 以 bool(result) 判定成败，void/None 返回的操作会发出 goal.round.failed 事件 |
| AUD-13610 | Suggestion | process | `src/features/edit/ai_editing/goal_supervisor.py:120` | medium | GoalSpec.from_dict 对超 32 条 constraint_refs 静默截断，随后以误导性 scope hash 错误失败 |
| AUD-13611 | Suggestion | process | `src/features/edit/ai_editing/help_index.py:34` | high | 未使用的导入 field、Callable |
| AUD-13612 | Suggestion | perf-opportunity | `src/features/edit/ai_editing/help_index.py:389` | medium | _feature_enabled 对每条 shortcut/panel 重复调用 list_features()，构建期 O(n·m) 且依赖源端缓存 |
| AUD-13613 | Suggestion | process | `src/features/edit/ai_editing/image_attachment.py:38` | high | _PNG_MAGIC 常量定义后未使用 |
| AUD-13701 | Major | risk | `src/features/edit/ai_editing/jobs.py:118（及 345-347、329-331）` | medium | JobWorkerPool 默认 2 个 worker，但 JobRegistry 共享事件计数器/事件表无锁，`_event_sequence += 1` 存在并发竞争 |
| AUD-13702 | Major | risk | `src/features/edit/ai_editing/jobs.py:131-133（及 123、154-155）` | medium | JobWorkerPool._futures 只增不减，且无任何清理 API，随会话无界增长（内存泄漏） |
| AUD-13703 | Suggestion | process | `src/features/edit/ai_editing/jobs.py:231-236` | medium | `fail()` 无终态防护，可把已 SUCCEEDED 的 job 覆写为 FAILED（与 `complete()` 的防护不对称） |
| AUD-13704 | Suggestion | process | `src/features/edit/ai_editing/jobs.py:304-305（及 368-370）` | medium | `from_dict` 未验证 `started_at`/`finished_at` 数值类型，恢复外部 JSON 后 `metrics()` 可能抛 TypeError |
| AUD-13705 | Suggestion | process | `src/features/edit/ai_editing/keyframe_sampler.py:136-141` | high | `derive_shot_spans` 对非 dict 的 cut 条目抛 AttributeError（try 只捕 TypeError/ValueError） |
| AUD-13706 | Suggestion | process | `src/features/edit/ai_editing/kill_switch.py:219-227` | medium | `emergency_disable_all` 汇总事件以 AI_ASSISTANT 占位，审计口径失真且与逐能力事件重复 |
| AUD-13707 | Suggestion | risk | `src/features/edit/ai_editing/llm_cost.py:54-55（对照 42-48、50-52）` | medium | `can_spend` 与 `record` 是两次独立加锁，check-then-act 非原子，并发请求可小幅超出预算 |
| AUD-13708 | Major | bug | `src/features/edit/ai_editing/llm_client.py:844（及 402-417、835-843）` | medium | `OpenAICompatClient.complete` 在 provider 返回 null content 且 finish_reason 非 "length" 时返回 None，违反 str 返回协议 |
| AUD-13709 | Major | bug | `src/features/edit/ai_editing/llm_client.py:1149、1188（协议文档 118 行）` | medium | `OpenAIResponsesClient` 把 `max_tokens=None` 强制为 1024，违反 LLMClient 协议"None 用后端默认"的约定，长输出被静默截断 |
| AUD-13710 | Suggestion | process | `src/features/edit/ai_editing/llm_client.py:842（及 925）` | medium | 空 content 防御性重试使用原始 `max_tokens` 而非阶梯降级后的 `budget`，可再次触发 max_tokens 400 直接失败 |
| AUD-13711 | Suggestion | process | `src/features/edit/ai_editing/llm_client.py:1279-1297` | medium | `_upload_file` 轮询超时后不报错，且把未就绪的 file_id 写入缓存 |
| AUD-13712 | Suggestion | process | `src/features/edit/ai_editing/llm_client.py:1304-1307（对照 1327-1333 注释与 1366-1374）` | medium | `_prepare_responses_content` 对"带 file_id、无 file_path"的块原样透传内部元数据（filename/mime），与自身"不得发送内部元数据"的约束不一致 |
| AUD-13713 | Suggestion | process | `src/features/edit/ai_editing/llm_client.py:286、289-294（size 取自 255 行）` | medium | `_post_multipart_file` 在 getsize 与实际读取之间无一致性保证，文件变动时 Content-Length 与实际字节不符 |
| AUD-13714 | Suggestion | process | `src/features/edit/ai_editing/llm_client.py:707、711（及 686-691、732-734）` | medium | `_local_document_text` 对解压后 XML 无大小上限，恶意 Office 附件可造成内存放大（zip bomb） |
| AUD-13715 | Suggestion | process | `src/features/edit/ai_editing/llm_client.py:1612-1615` | high | `make_client` 未知 provider_kind 的报错信息未列全受支持值 |
| AUD-13801 | Major | bug | `src/features/edit/ai_editing/llm_variant_planner.py:532-545` | medium | `_allocate_beat_ranges` 在 target_duration < beat 数时算出非正时长，FrameRange 抛裸 ValueError 而非 LLMPlannerError，且与规则引擎行为不一致 |
| AUD-13802 | Suggestion | process | `src/features/edit/ai_editing/llm_variant_planner.py:126-127` | high | `ContextService` / `ContextSources` 仅以字符串注解出现，从未导入（也无 TYPE_CHECKING 导入），类型检查与 IDE 解析断链 |
| AUD-13810 | Suggestion | process | `src/features/edit/ai_editing/mcp_gateway.py:286-287` | high | `handle_request` 兜底 except 把内部异常类型与消息原样回传外部客户端 |
| AUD-13811 | Suggestion | process | `src/features/edit/ai_editing/mcp_gateway.py:183, 210, 214` | high | `_sessions` 字典无并发保护、无上限与回收策略 |
| AUD-13820 | Major | risk | `src/features/edit/ai_editing/mcp_local_ipc.py:60-66` | medium | `QtMcpRequestDispatcher.dispatch` 超时后请求仍会在 GUI 线程执行：超时≠取消，客户端收到失败响应但操作可能照常发生 |
| AUD-13821 | Suggestion | process | `src/features/edit/ai_editing/mcp_local_ipc.py:217-220` | medium | 消息大小上限在整行读入内存之后才检查，超长行可先耗尽内存 |
| AUD-13822 | Suggestion | process | `src/features/edit/ai_editing/mcp_local_ipc.py:69-77` | medium | `default_socket_path` 对已存在目录的 chmod 失败被静默吞掉，端点目录可能非 0700 |
| AUD-13830 | Suggestion | risk | `src/features/edit/ai_editing/mcp_plugin.py:111, 147-149, 208-213` | medium | `McpPluginHost._servers` 自身无锁且无清理路径，`snapshot()` 迭代存在竞态 |
| AUD-13840 | Suggestion | process | `src/features/edit/ai_editing/mcp_stdio.py:126-129` | high | `_DemoFacade.list_tools` 两个分支返回同一列表，max_permission 过滤逻辑缺失（死代码） |
| AUD-13841 | Suggestion | process | `src/features/edit/ai_editing/mcp_stdio.py:81-86` | high | 行大小上限在整行读入之后才检查（与 mcp_local_ipc 同模式） |
| AUD-13850 | Major | risk | `src/features/edit/ai_editing/mcp_vision_gateway.py:383-415（对比 354-381 的` | medium | `_ensure_client` 全程不持 `self._lock`：并发首调会重复构建 client，输家的 npx 子进程永不回收；且与 `shutdown()` 存在竞态 |
| AUD-13851 | Suggestion | risk | `src/features/edit/ai_editing/mcp_vision_gateway.py:320-331, 421-426, 445-453` | medium | `_session_images` / `_server_disabled` 的读写均无锁 |
| AUD-13852 | Suggestion | process | `src/features/edit/ai_editing/mcp_vision_gateway.py:103-128, 272-277, 294；471-472（tool_definitions 间接触发）` | medium | `web_available` / `server_statuses` 每次访问都同步触发 profile 文件 + 凭据存储的磁盘 I/O |
| AUD-13853 | Suggestion | process | `src/features/edit/ai_editing/mcp_vision_gateway.py:428-435` | high | `save_and_register_image` 生成的临时图片文件无任何清理机制 |
| AUD-13900 | Major | bug | `src/features/edit/ai_editing/media_generation_service.py:439-444` | medium | 月度成本告警被计算后直接丢弃，告警链路整体失效 |
| AUD-13901 | Major | risk | `src/features/edit/ai_editing/media_generation_service.py:468-472（调用点 309-314）` | medium | CostLedger.add 为无锁读-改-写，多 worker 并发下丢失成本累计 |
| AUD-13902 | Major | bug | `src/features/edit/ai_editing/media_generation_service.py:262-264 与 316-321` | medium | 取消检查与候选入箱之间存在 TOCTOU：取消后候选仍进入素材箱 |
| AUD-13903 | Major | risk | `src/features/edit/ai_editing/media_generation_service.py:113-118` | medium | 默认 2 worker 的线程池驱动自述"不加锁"的 JobRegistry 与无锁 GenerationBin |
| AUD-13904 | Major | risk | `src/features/edit/ai_editing/media_generation_service.py:207-211` | medium | close() 以 cancel_futures=True 关闭线程池后，未启动的 Job 永远停留 QUEUED |
| AUD-13905 | Suggestion | doc-gap | `src/features/edit/ai_editing/media_generation_service.py:358-364` | medium | cancel() 的 docstring 与 JobRegistry.cancel 实际行为不符 |
| AUD-13906 | Suggestion | process | `src/features/edit/ai_editing/media_generation_service.py:150-158` | medium | request_generation_async 丢弃执行期 diagnostics，与同步入口不对称 |
| AUD-13907 | Suggestion | process | `src/features/edit/ai_editing/media_generation_service.py:461-466` | high | CostLedger._totals 用 None 哨兵默认值 + type: ignore，可变默认值反模式 |
| AUD-13908 | Major | bug | `src/features/edit/ai_editing/media_materialization_pipeline.py:63（上游 85 行；根因见 media_probe.py:254、454）` | medium | fps 为有理数字符串（"30000/1001"），_as_float 无法解析，GeneratedMediaProbe.fps 恒为 None |
| AUD-13909 | Suggestion | process | `src/features/edit/ai_editing/media_materialization_pipeline.py:82-90` | medium | `**metadata` 在精选键之后展开，探针元数据可覆盖已归一化字段 |
| AUD-13910 | Suggestion | process | `src/features/edit/ai_editing/media_materialization_pipeline.py:52-57（策略检查在 104-105 行）` | medium | 先全量哈希再检查 max_bytes，超大媒体在被拒绝前已被完整读入 |
| AUD-13911 | Suggestion | process | `src/features/edit/ai_editing/media_materialization_pipeline.py:181-188` | medium | thumbnail/proxy builder 异常未防护，与 probe 失败的 QA-block 处理不一致 |
| AUD-13912 | Suggestion | process | `src/features/edit/ai_editing/media_materialization_pipeline.py:9 与 209-211` | high | mimetypes 重复导入（模块级与函数内各一次） |
| AUD-13913 | Suggestion | process | `src/features/edit/ai_editing/media_probe.py:455-456（误分类点 414-419）` | medium | ffprobe duration 为 "N/A" 等非数值字符串时，整次 probe 误判为 PROBE_INVALID_JSON |
| AUD-13914 | Suggestion | process | `src/features/edit/ai_editing/media_probe.py:80、143-154` | high | 矩阵函数的 probe 参数注解为 FfprobeMediaProbe，但默认值 create_media_probe() 可返回 PyAVMediaProbe |
| AUD-13915 | Major | risk | `src/features/edit/ai_editing/message_record.py:131` | medium | from_dict 的 timestamp 未防护 float() 转换，损坏的持久化数据会使会话恢复崩溃 |
| AUD-13916 | Suggestion | process | `src/features/edit/ai_editing/message_record.py:88-93` | medium | 非法 role/status 被静默强制为 "user"/COMPLETED，掩盖持久化数据损坏 |
| AUD-13917 | Major | bug | `src/features/edit/ai_editing/mission_workspace.py:106-148（字段访问 127-147）` | medium | workspace_from_run_result 依赖不存在的 AgentRunResult 及与真实 AgentCheckpoint 不匹配的字段名 |
| AUD-13918 | Suggestion | process | `src/features/edit/ai_editing/mission_workspace.py:71-77` | medium | diagnostics 按关键词子串分类，"no error occurred" 也会被归为 error |
| AUD-13919 | Major | bug | `src/features/edit/ai_editing/missions.py:319-330` | medium | set_status 不校验 status 字符串、不禁止终态复活，绕过构造期校验产生僵尸状态 |
| AUD-13920 | Major | risk | `src/features/edit/ai_editing/missions.py:245-253（类 docstring 声称 209-211 行）` | medium | Store 的查询方法返回可变 Mission 活引用，锁外并发修改与锁内原子替换竞争 |
| AUD-13921 | Suggestion | process | `src/features/edit/ai_editing/missions.py:237-243` | medium | parent_run_id 语义混乱：按 mission_id 查父，字段名义却是 run id，传错时静默不链接 |
| AUD-13922 | Suggestion | process | `src/features/edit/ai_editing/missions.py:302-313` | medium | update_criterion 对未知 check 静默 no-op |
| AUD-13923 | Major | bug | `src/features/edit/ai_editing/model_router.py:42-46、64-69` | medium | _supports_reasoning 读取 AiProviderProfile 上不存在的 reasoning_efforts 字段，能力过滤退化为 no-op |
| AUD-14000 | Major | bug | `src/features/edit/ai_editing/models.py:355-358` | high | ValidationReport.from_dict 的容错回退引用不存在的 `PlanStatus.INVALID`，该分支自身抛 AttributeError |
| AUD-14001 | Suggestion | process | `src/features/edit/ai_editing/models.py:278-285` | high | EditPlan.from_dict 的 requested_outputs.preset 未做类型归一，与同函数其余字段的强转换风格不一致 |
| AUD-14002 | Suggestion | process | `src/features/edit/ai_editing/models.py:114-117（同类：390-391 SnapshotTrack.caption_metadata）` | medium | frozen 契约对象内嵌可变 dict 且构造时不做防御性拷贝，调用方保留引用即可改写"稳定合同" |
| AUD-14003 | Major | bug | `src/features/edit/ai_editing/motion_graphic_artifacts.py:210-228（配合 can_write_artifact:231-251）` | medium | scene_is_stale 在"空 context_fingerprint + 未传 current_scene_revision"的默认组合下恒返回 False，写入门的过期检测被完全旁路 |
| AUD-14004 | Major | bug | `src/features/edit/ai_editing/motion_graphic_artifacts.py:184-202` | medium | HtmlArtifact.from_dict 无条件恢复 writable 与 render_evidence，违背"恢复后必须重新验证"不变量 |
| AUD-14005 | Suggestion | process | `src/features/edit/ai_editing/motion_graphic_run.py:218-220（配合 165-176）` | medium | confirm_and_write 在门检查通过后直接 grant_approval，run 不在 WAITING_APPROVAL 时抛 RunNotTransitionableError 而非返回否定决策；request_confirm |
| AUD-14006 | Suggestion | process | `src/features/edit/ai_editing/motion_graphic_run.py:136-143（对照 229-233 complete_write）` | medium | record_verification 无条件 emit RUN_VERIFICATION_FINISHED，与 orchestrator 的同名事件构成两套语义，run 事件流中"验证"生命周期重复出现 |
| AUD-14007 | Major | bug | `src/features/edit/ai_editing/music_policy.py:93-98` | medium | license 映射把 "cleared" 吞进 LICENSED 分支，`LicenseStatus.CLEARED` 赋值分支不可达，"已清权"被归类为"已授权" |
| AUD-14008 | Suggestion | process | `src/features/edit/ai_editing/music_policy.py:99-107` | high | register_from_media_asset 对外部 metadata 的 bpm 直接 float()、mood 不验类型，恶意/损坏元数据可抛异常或产生 NaN 破坏排序 |
| AUD-14009 | Major | bug | `src/features/edit/ai_editing/ocr_provider.py:374-390` | medium | _model_version 无法区分 stub 与真实推理（两路径返回同一包版本串），切换 TOPOS_OCR_REAL 不触发重新分析，默认 stub 数据被标成真实 rapidocr 版本 |
| AUD-1401 | Major | risk | `src/shared/gpu/metal/context.py:366-385` | medium | dispatch 异常路径静默丢弃整个累积 command buffer，已成功编码的 kernel 调用一并丢失且调用方无感知 |
| AUD-14010 | Major | bug | `src/features/edit/ai_editing/ocr_provider.py:148-152（对照 288-294）` | medium | analyze 只探测 find_spec("rapidocr") 未探测 rapidocr_onnxruntime，与 _get_engine 的旧包回退设计矛盾——仅安装旧包的环境直接被拒 |
| AUD-14011 | Suggestion | process | `src/features/edit/ai_editing/ocr_provider.py:189-193, 205-211` | high | _select_frames/_total_frames 用宽泛 except Exception 静默吞掉 provider 故障且无任何日志 |
| AUD-14012 | Suggestion | process | `src/features/edit/ai_editing/ocr_provider.py:473-476` | high | NaN 置信度经 `max(0.0, min(1.0, float(score)))` 变成 1.0（NaN 比较短路），异常分数反获满置信 |
| AUD-14013 | Suggestion | process | `src/features/edit/ai_editing/ocr_provider.py:45-50, 141, 315` | high | 杂项质量：derive_shot_spans 导入未使用；analyze 的 progress 形参被忽略（改用类内 _progress_cb）；_engine_kwargs 内重复 import os |
| AUD-14014 | Suggestion | risk | `src/features/edit/ai_editing/ocr_provider.py:121, 288-304` | low | _engine 惰性初始化无锁，provider 实例被并发 analyze 时可能重复加载模型 |
| AUD-14015 | Suggestion | process | `src/features/edit/ai_editing/operation_protocol.py:73-88` | medium | `_op` 紧凑构造器的 **kw 无类型注解，且必填键缺失时报裸 KeyError |
| AUD-1402 | Major | risk | `src/shared/gpu/metal/context.py:366-380（dispatch）、387-424（submit）、451-495（read_texture）` | medium | PyObjC 路径全程无 autoreleasepool，工作线程上每次 dispatch/submit/read_texture 泄漏 autoreleased Metal 对象 |
| AUD-1403 | Suggestion | risk | `src/shared/gpu/metal/context.py:400-417` | high | `_last_gpu_time_ms` 在 submit 锁外读写，并发提交时诊断数据竞态 |
| AUD-1404 | Suggestion | process | `src/shared/gpu/metal/context.py:185-196` | medium | `create_buffer_from_numpy` 实为双重拷贝，与「零额外拷贝语义」注释不符 |
| AUD-1405 | Suggestion | process | `src/shared/gpu/metal/context.py:138-144` | medium | `compile_kernel` 以 kernel 名为缓存键，同名传入不同 MSL 源时返回陈旧 kernel |
| AUD-1406 | Suggestion | process | `src/shared/gpu/metal/context.py:353-361` | medium | `dispatch` 不校验 threads 正值与 group ≤ maxTotalThreadsPerThreadgroup，非法值直接落入 Metal 校验断言 |
| AUD-1407 | Suggestion | process | `src/shared/gpu/metal/context.py:31-33、477` | high | 模块级重复 `import numpy as np`，`read_texture` 内再次函数级导入 |
| AUD-1408 | Suggestion | process | `src/shared/gpu/metal/context.py:251-259` | medium | `write_buffer` 对 Shared 存储 buffer 也调用 `didModifyRange_`（探测的是 selector 存在性而非 storage mode） |
| AUD-1409 | Suggestion | process | `src/shared/gpu/metal/kernels.py:274-291` | medium | `make_blur_params` / `make_erode_dilate_params` 无 isfinite 与数值域校验（与 kernels_color 的 build_* 标准不一致） |
| AUD-1410 | Suggestion | process | `src/shared/gpu/metal/kernels.py:288` | high | `make_erode_dilate_params` 的 operation 非法字符串静默映射为 dilate |
| AUD-14100 | Major | bug | `src/features/edit/ai_editing/plan_compiler.py:616` | medium | `_compile_set_fade` 抛裸 `ValueError` 而非 `CompileError`，且 schema/validator 均不要求 fade 字段，异常穿透 `preview()` 的 `except Compile |
| AUD-14101 | Major | bug | `src/features/edit/ai_editing/plan_compiler.py:331` | medium | `_compile_operation` 仅把 `KeyError` 转 `CompileError`，`int()`/字符串索引强转产生的 `ValueError`/`TypeError` 全部逃逸 |
| AUD-14102 | Suggestion | process | `src/features/edit/ai_editing/plan_compiler.py:320-328` | high | REMOVE_TRANSITION 编译分支重复，第二处为不可达死代码 |
| AUD-14103 | Suggestion | process | `src/features/edit/ai_editing/plan_compiler.py:753-755` | high | `_compile_set_track_state` 抛 `CompileError` 时把 track_id 当 op_id 传入 |
| AUD-14104 | Suggestion | process | `src/features/edit/ai_editing/plan_repository.py:107-109` | medium | `StoredPlan.from_dict` 直接信任持久化的 `content_hash`，导入时不复核哈希与 plan 内容的一致性 |
| AUD-14105 | Suggestion | perf-opportunity | `src/features/edit/ai_editing/plan_repository.py:184-189` | high | `find_by_hash` O(n) 线性扫描，且仓库无任何淘汰/上限策略 |
| AUD-14106 | Major | bug | `src/features/edit/ai_editing/plan_validator.py:304-310` | medium | SET_CLIP_FADE 的 fade 字段、ADD_TRANSITION 的 duration 等值约束在 schema/validator 两层全部缺失，畸形 plan 以 VALID 放行后必然在 compile 期以未分类异常炸出 |
| AUD-14107 | Suggestion | process | `src/features/edit/ai_editing/plan_validator.py:27-28` | high | 同一 from-import 中 `get_effect_capability` 重复导入两次 |
| AUD-14108 | Suggestion | process | `src/features/edit/ai_editing/plan_validator.py:483-490` | high | `_clip_not_found_diag` 复用 `TRACK_NOT_FOUND` 诊断码上报 clip 缺失 |
| AUD-14109 | Major | bug | `src/features/edit/ai_editing/postcondition_checker.py:156-157` | medium | 未注册任何 postcondition 的任务种类返回 `outcome=PASSED`，而唯一消费者按 `outcome != "failed"` 判定成功——零检查即 SUCCEEDED |
| AUD-1411 | Suggestion | process | `src/shared/gpu/metal/kernels.py:309-310` | medium | `gaussian_blur_reference` 权重以 float64 计算后 cast float32，与 kernel 的 fp32 `exp` 存在 ulp 级偏差 |
| AUD-14110 | Suggestion | process | `src/features/edit/ai_editing/postcondition_checker.py:225-238` | high | `no_tool_failure` 已定义并导出但从未注册到任何任务种类，"工具失败阻断成功"门从未生效 |
| AUD-14111 | Suggestion | process | `src/features/edit/ai_editing/postcondition_checker.py:201-204` | high | `html_artifact_present` 回退条件过宽：任意非 HTML 产物也判通过 |
| AUD-14112 | Suggestion | process | `src/features/edit/ai_editing/privacy_guard.py:44-49` | medium | `redact_mapping` 不递归 list/tuple 内的 Mapping/str——嵌套消息列表中的密钥与路径原样透传，且该函数当前全库无调用方 |
| AUD-14113 | Suggestion | process | `src/features/edit/ai_editing/privacy_guard.py:18-20` | high | `PrivacyViolation` 定义并导出但全库无 raise/catch，死代码 |
| AUD-14114 | Suggestion | process | `src/features/edit/ai_editing/professional_skills.py:356-357` | high | `register_recommended_skills` 用宽泛 `except Exception: continue` 静默吞掉注册失败 |
| AUD-14115 | Suggestion | process | `src/features/edit/ai_editing/professional_skills.py:318-336` | medium | `SKILL_ALIASES` 把 `topos.interview_highlights` 映射到另一个不同技能 `topos.voiceover_polish`，且整个别名表无任何调用方 |
| AUD-14116 | Suggestion | process | `src/features/edit/ai_editing/profile_tts_provider.py:147-152` | medium | `_remote_speaker` 静默丢弃用户配置的非 `zh_` 前缀音色，强制替换为硬编码中文女声 |
| AUD-14117 | Suggestion | risk | `src/features/edit/ai_editing/profile_tts_provider.py:108-138` | medium | 远程合成逐 cue 同步调用 `provider.generate`（无超时/取消）+ 每 cue 一次 ffmpeg 子进程，长字幕轨长时间阻塞调用线程 |
| AUD-1412 | Suggestion | process | `src/shared/gpu/metal/kernels_color.py:223-227（校验）、143-144（kernel 消费）` | medium | `build_film_grain_params` 只校验 finite，不校验 tex_size > 0 与 visible_rect 数值域；tex_w=0 时 kernel 产生全图 NaN |
| AUD-1413 | Suggestion | process | `src/shared/gpu/metal/kernels_dct.py:45（forward_dct）、134（forward_dct_tg）、169（forward_dct_prod）、215（forward_dct_tg4）、251（forward_dct_m8）` | medium | 全部 DCT 变体的 y 维度（gid.y / grp.y）无越界守卫，x 维度守卫不对称存在，依赖调用方派发精确 y 网格 |
| AUD-1414 | Major | doc-gap | `src/shared/gpu/metal/kernels_dec.py:267（对照 524）` | medium | slice 版 `idx > 63` 发出未声明的 status=93u，与 docstring 状态集 {0,4,9,11} 及 segment 版同位置的 9u 不一致 |
| AUD-1415 | Suggestion | process | `src/shared/gpu/metal/kernels_dec.py:222、485` | medium | `st.size = me[4] - prefix`（及 segment 的 `me[4] - 4u`）无符号下溢无防护，畸形 smeta 导致 GPU 越界读 |
| AUD-1416 | Suggestion | doc-gap | `src/shared/gpu/metal/kernels_dec.py:22-25` | high | 模块 docstring 内 smeta 布局自相矛盾：12×u32 与 13×u32 并存（实际为 13） |
| AUD-1417 | Suggestion | process | `src/shared/gpu/metal/kernels_enc.py:52-53` | medium | `quant_token` 的 grp.y 无越界守卫（与 AUD-1413 同类），依赖调用方 threads.y 精确等于带高×8 |
| AUD-1418 | Suggestion | risk | `src/shared/gpu/metal/kernels_histogram.py:75-79` | high | `histogram_cdf` 每线程重复线性扫描全通道 256 桶两次（total + cum），1024 线程共约 52 万次冗余读 |
| AUD-14201 | Suggestion | risk | `src/features/edit/ai_editing/provider_plugin.py:133` | medium | `AnalysisPluginHost` 自身状态与其委托的 `AnalysisService` 无锁保护，与线程安全的 `PluginRegistry` 保护级别不一致 |
| AUD-14202 | Major | bug | `src/features/edit/ai_editing/provider_resume.py:89-95` | medium | `make_fingerprint` 用 `default=str` 序列化输入，非 JSON 原生类型按 `repr` 入哈希，重启后同一逻辑请求指纹不稳定，破坏 OS-09 幂等去重 |
| AUD-14203 | Suggestion | process | `src/features/edit/ai_editing/provider_resume.py:243-248（对照 113 行` | medium | `EXT_PENDING`（默认状态）落入"未知状态"else 分支，reason 误标已定义状态；`RECONCILE_FAIL` 已定义导出但无任何产生路径 |
| AUD-14204 | Suggestion | process | `src/features/edit/ai_editing/quality_gate.py:198-213（对照 106-111 行` | medium | 硬门槛/软 KPI 输入未做范围校验，负计数与越界比率可令硬门槛 fail-open |
| AUD-14205 | Major | bug | `src/features/edit/ai_editing/release_evidence.py:27-35` | medium | `_safe_metadata` 只做 key 黑名单 + 值前缀路径过滤，字符串值中段包含 secret/绝对路径不被清除，且未复用同包已有的 `redact_text` |
| AUD-14206 | Suggestion | perf-opportunity | `src/features/edit/ai_editing/release_evidence.py:95-96` | high | `TelemetryCollector.emit` 超限用 `list.pop(0)` 淘汰，O(n) 每事件 |
| AUD-14207 | Suggestion | risk | `src/features/edit/ai_editing/release_evidence.py:91-97、103` | medium | `TelemetryCollector` 无锁：`set_opt_in` 的 `clear()`、`emit` 的 append/pop 与 `export()` 迭代并发时无保护 |
| AUD-14208 | Major | bug | `src/features/edit/ai_editing/release_gate.py:103-107` | medium | 首 token 硬门槛在指标为 None 时被静默跳过，"runs>0 但无一次首 token"可空过发布门，与自述"No data is a failure"矛盾 |
| AUD-14209 | Suggestion | process | `src/features/edit/ai_editing/release_gate.py:58-59、76-82` | medium | 与同包 `eval_runner` 存在同名异构的 `ReleaseGateResult` / `evaluate_release_gate`，易误导入 |
| AUD-14210 | Suggestion | process | `src/features/edit/ai_editing/release_manifest.py:27-37` | medium | `default_flags` 传入时不校验键集/值类型；自动填充路径隐式依赖两常量集不漂移 |
| AUD-14211 | Suggestion | process | `src/features/edit/ai_editing/release_manifest.py:112-119` | medium | `feature_flags` 参数缺 `is_enabled` 时静默回退默认值，无任何诊断 |
| AUD-14212 | Major | doc-gap | `src/features/edit/ai_editing/release_test_matrix.py:124` | medium | `min_pass_rate` 参数被 `not failed` 条件完全蕴含，<1.0 的取值无效（死参数），与 docstring"pass rate 阈值"承诺矛盾 |
| AUD-14300 | Major | bug | `src/features/edit/ai_editing/remote_transcript_provider.py:290（words 在 :320 同样问题）` | high | 远端返回非数值 confidence 时以未净化的 ValueError/TypeError 逃逸，破坏错误净化契约 |
| AUD-14301 | Major | bug | `src/features/edit/ai_editing/remote_transcript_provider.py:343-353` | high | ffprobe 失败时整体时长回退 1 秒，整段转录的 end_frame 被钳到 1s |
| AUD-14302 | Major | bug | `src/features/edit/ai_editing/remote_transcript_provider.py:335-341（消费点 :268-274）` | medium | `_time_ms` 把非法/None 时间静默映射为 0.0，外部数据校验 fail-open |
| AUD-14303 | Suggestion | process | `src/features/edit/ai_editing/remote_transcript_provider.py:152（volcengine）、:187 与 :192（openai multipart）` | high | 100 MB 音频全量读入内存并二次拷贝（read_bytes + b"".join） |
| AUD-14304 | Suggestion | process | `src/features/edit/ai_editing/remote_transcript_provider.py:280` | medium | `additions` 字段未校验 Mapping 即调用 `.get` |
| AUD-14305 | Suggestion | process | `src/features/edit/ai_editing/remote_transcript_provider.py:91-115` | high | `analyze()` 无取消钩子，长上传期间无法取消 |
| AUD-14306 | Major | doc-gap | `src/features/edit/ai_editing/replan_policy.py:143（定义）；:178-270（` | medium | `max_clarifications` 声明但从未执行，文档承诺的硬上限失效 |
| AUD-14307 | Suggestion | process | `src/features/edit/ai_editing/replan_policy.py:33-35` | high | `ReplanDecisionKind` 是空的 str 子类，不提供任何类型约束 |
| AUD-14308 | Suggestion | process | `src/features/edit/ai_editing/replan_policy.py:103` | medium | frozen dataclass 携带可变 `Mapping` 字段，实例不可哈希且外部可改内部状态 |
| AUD-14309 | Suggestion | process | `src/features/edit/ai_editing/request_recovery.py:33-39（消费点 :54、:96-97）` | medium | `retryable_codes` 包含 `LLM_RETRY_EXHAUSTED`，外层重试会放大底层已耗尽的重试 |
| AUD-14310 | Major | bug | `src/features/edit/ai_editing/run_context.py:119-120（对照模块 docstring :19-21）` | medium | `selected_asset_ids` 为空时 `asset_in_scope` 对所有素材返回 True，与模块声明的 fail-closed 不变量矛盾 |
| AUD-14311 | Suggestion | process | `src/features/edit/ai_editing/run_context.py:164-184（对照 :100-107 的` | high | `from_dict` 不做类型收敛，畸形持久化数据触发 TypeError 而非干净校验错误 |
| AUD-14312 | Major | bug | `src/features/edit/ai_editing/runtime_capability_snapshot.py:366（` | medium | 模型包条目顺序来自 frozenset 迭代，跨进程不稳定，snapshot_id 违反"同一输入 → 同一 ID"核心契约 |
| AUD-14313 | Major | risk | `src/features/edit/ai_editing/runtime_capability_snapshot.py:452-455（GPU 探测）、:421-429（git rev-parse）、:492-503（串行探测序列）` | medium | 快照构建串行执行多个秒级 subprocess 探测且模块层无缓存，UI 线程首次访问可冻结数十秒 |
| AUD-14314 | Suggestion | risk | `src/features/edit/ai_editing/runtime_capability_snapshot.py:137-161` | medium | `CapabilitySnapshotStore` 无锁，set/subscribe/退订存在竞态窗口 |
| AUD-14315 | Suggestion | process | `src/features/edit/ai_editing/runtime_capability_snapshot.py:508` | medium | `detected_at` 用 "+0000" 时区后缀，与项目其它 ISO 时间戳格式不一致 |
| AUD-14316 | Major | risk | `src/features/edit/ai_editing/schema.py:212-215` | medium | `op.kind` 为非字符串（list/dict）时 `OperationKind()` 抛 TypeError，`except ValueError` 未捕获 → 校验器自身崩溃 |
| AUD-14317 | Major | bug | `src/features/edit/ai_editing/schema.py:9-12（docstring）与 :265-276（实现）、:359（schema 生成）` | medium | 模块声称"未知 core 字段默认拒绝"，实现只对纯数字键给 WARNING、其余未知字段静默透传 |
| AUD-14318 | Suggestion | doc-gap | `src/features/edit/ai_editing/schema.py:108-127` | high | `validate_plan_structure` 函数体内有两个连续 docstring 字面量，第二个是死代码 |
| AUD-14319 | Suggestion | process | `src/features/edit/ai_editing/schema.py:267-271` | high | `isinstance(pkey, str)` 守卫位于 `pkey.startswith(...)` 之后，守卫位置错误 |
| AUD-14320 | Suggestion | process | `src/features/edit/ai_editing/schema.py:104（签名）、:146（首个` | high | `plan_dict` 本身无 dict 守卫；`findings_to_diagnostics` 返回类型为裸 `list` |
| AUD-14400 | Suggestion | process | `src/features/edit/ai_editing/semantic_index.py:62-71` | medium | `add()` 覆盖同 ID segment 时不清理旧 embedding，缓存与内容脱节 |
| AUD-14401 | Suggestion | process | `src/features/edit/ai_editing/semantic_index.py:218-220` | low | 零向量 embedding 在 `similarity_search` 中以 -1.0 分入选 |
| AUD-14402 | Major | bug | `src/features/edit/ai_editing/session_models.py:345-357` | medium | `SessionCatalog.from_dict` 接受未来版本号，未知字段在下次落盘时被静默丢弃 |
| AUD-14403 | Suggestion | process | `src/features/edit/ai_editing/session_models.py:393-396` | high | `migrate_legacy_snapshot` 存在死代码，注释声称保留 variant/decision 引用但实际是 no-op |
| AUD-14404 | Suggestion | process | `src/features/edit/ai_editing/session_models.py:296-301, 326-330` | high | `archive_session`/`delete_session` 用插入序（创建序）选"最近"会话，与注释语义不符 |
| AUD-14405 | Suggestion | process | `src/features/edit/ai_editing/session_models.py:51-53` | high | `MessageSummary` 非法 role 被静默改写为 "user" |
| AUD-14406 | Suggestion | process | `src/features/edit/ai_editing/session_models.py:150-151, 229-232` | medium | `from_dict` 系列对容器元素类型不校验，字符串会被 `tuple()` 拆成字符 |
| AUD-14407 | Suggestion | process | `src/features/edit/ai_editing/skill_manifest.py:104-107, 197` | medium | `ToolPolicy` 空 `allowed_tools` 语义为"允许全部（≤ceiling）"，默认策略默认开放 |
| AUD-14408 | Suggestion | process | `src/features/edit/ai_editing/skill_manifest.py:194-206, 222-226` | medium | frozen `SkillManifest` 的 dict 字段可变，原地修改后 `content_hash`/signature 与内容脱节 |
| AUD-14409 | Suggestion | process | `src/features/edit/ai_editing/skill_manifest.py:109` | medium | `_PERM_ORDER` 本地镜像 ToolPermission 顺序，常量双份存在漂移风险 |
| AUD-14410 | Major | bug | `src/features/edit/ai_editing/skill_registry.py:176-184` | medium | REVISION_CONFLICT 诊断模板变量不匹配，升级失败的真实原因被整体丢弃、消息渲染为原始占位符 |
| AUD-14411 | Suggestion | process | `src/features/edit/ai_editing/skill_registry.py:174-189` | medium | `register` 升级替换时重置 enabled 状态，已禁用 Skill 升级后被静默重新启用 |
| AUD-14412 | Suggestion | process | `src/features/edit/ai_editing/skill_registry.py:143-190` | high | `register` 不校验 `roles` 是否为合法 TaskRole 值 |
| AUD-14413 | Major | bug | `src/features/edit/ai_editing/skill_runtime.py:607-611` | medium | `DefaultGraphBuilder` 静默丢弃未知 role，全部非法时产出空图并"成功"返回空结果——失败不可见 |
| AUD-14414 | Suggestion | process | `src/features/edit/ai_editing/skill_runtime.py:571-584` | medium | `_validate_input` 三处诊断缺模板变量，消息渲染为带 `{asset_id!r}` 等占位符的原文 |
| AUD-14415 | Suggestion | process | `src/features/edit/ai_editing/skill_runtime.py:567-585` | high | `_validate_input` 与 manifest `input_schema` 的 required 不一致（brief_id/base_revision 未校验） |
| AUD-14416 | Suggestion | process | `src/features/edit/ai_editing/skill_runtime.py:244-247` | medium | `DeterministicRoleExecutor._media_librarian` 把所有 citation 硬编码 `asset_id=primary`、`trust="trusted"` |
| AUD-14417 | Suggestion | process | `src/features/edit/ai_editing/skill_runtime.py:348, 634` | high | QA 时长消息误用 `role_hint` 兜底 '30s'，而 `DefaultGraphBuilder` 把 role_hint 填成 Skill title |
| AUD-14418 | Major | bug | `src/features/edit/ai_editing/snapshot_service.py:128-132` | medium | `analysis_provider.get_status` 被 `except Exception` 整体吞掉且无日志，掩盖领域错误 |
| AUD-14419 | Suggestion | process | `src/features/edit/ai_editing/snapshot_service.py:42-44` | high | `_build_track_summary` 的 `sequence` 参数从未使用 |
| AUD-14420 | Suggestion | process | `src/features/edit/ai_editing/speech_preprocessor.py:75-82` | medium | `unavailable_reason` 对"manager 存在但无 demucs_v4 spec"视为可用，失败延迟到 subprocess 才暴露 |
| AUD-14421 | Suggestion | process | `src/features/edit/ai_editing/startup_diagnostics.py:85-87` | medium | `_profile_check` 混用两个 check_id（llm_profile / llm_credentials），成功时后者无记录 |
| AUD-14500 | Suggestion | process | `src/features/edit/ai_editing/structured_output.py:29` | medium | 单行无换行的 fenced JSON 剥离失败，且与 `_decode_json` 存在两套不一致实现 |
| AUD-14501 | Suggestion | process | `src/features/edit/ai_editing/structured_output.py:60` | medium | `validate_planner_output` 接受空 variants，无最小数量契约 |
| AUD-14502 | Suggestion | process | `src/features/edit/ai_editing/subtitle_ai_planner.py:76` | medium | 字幕操作白名单字面量在两处重复，存在漂移风险 |
| AUD-14503 | Suggestion | process | `src/features/edit/ai_editing/subtitle_ai_planner.py:163` | high | `compile_plan` 的包装捕获过宽（`except Exception`） |
| AUD-14504 | Suggestion | doc-gap | `src/features/edit/ai_editing/subtitle_draft.py:3` | high | 模块 docstring 声称 ADD_SUBTITLE 未接通编译器，与现状不符 |
| AUD-14505 | Suggestion | process | `src/features/edit/ai_editing/subtitle_draft.py:99` | high | `max_cps` 未做下限验证，`fps` 有验证而阈值没有 |
| AUD-14506 | Suggestion | process | `src/features/edit/ai_editing/subtitle_draft.py:130` | medium | `speaker` 未规范化为 `str \| None`，payload 异型值直接透传 |
| AUD-14507 | Suggestion | process | `src/features/edit/ai_editing/subtitle_placement.py:83` | medium | 无词对齐回退按字符数硬切，拉丁词会被切成两半 |
| AUD-14508 | Major | bug | `src/features/edit/ai_editing/task_artifacts.py:298` | medium | QaReport 的 `approved` 字段与 `blocking_findings`/`passes` 可不一致，不变量未在数据类强制 |
| AUD-14509 | Suggestion | process | `src/features/edit/ai_editing/task_artifacts.py:275` | medium | severity/trust/category 为自由字符串，无枚举或规范化 |
| AUD-14510 | Suggestion | process | `src/features/edit/ai_editing/task_artifacts.py:64` | medium | 条目级 id 不做非空验证，与容器级验证不一致 |
| AUD-14511 | Major | risk | `src/features/edit/ai_editing/task_graph.py:324` | medium | SingleWriterPolicy 无锁 check-then-set（TOCTOU），与其设想的多 graph 并发场景矛盾 |
| AUD-14512 | Suggestion | process | `src/features/edit/ai_editing/task_graph.py:500` | medium | 超时/预算仅在执行返回后检查，无法中断挂起的 role executor |
| AUD-14513 | Suggestion | process | `src/features/edit/ai_editing/task_graph.py:522` | medium | `expected_artifact_kind` 字段从未被实际校验，失败消息引用了未使用的声明值 |
| AUD-14514 | Suggestion | process | `src/features/edit/ai_editing/task_graph.py:502` | medium | 通过 getattr 鸭子类型发现可选方法与共享可变属性，协议未声明 |
| AUD-14515 | Suggestion | process | `src/features/edit/ai_editing/task_graph.py:435` | medium | 一次 execute 触发 validate() 三次重复计算 |
| AUD-14600 | Suggestion | test-gap | `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:88-92` | medium | 已取消短路测试断言过弱：只验 `result is not None`，未断言 cancelled 降级契约与状态机 |
| AUD-14601 | Suggestion | test-gap | `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:116-130` | medium | `emit_node_event` 测试回调签名与真实调用不匹配，`emitted` 恒空且从未断言（死测试脚手架） |
| AUD-14602 | Suggestion | process | `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:207` | high | `_capture_snapshot` 缺少返回类型注解 |
| AUD-14603 | Suggestion | test-gap | `src/features/edit/ai_editing/tests/test_assistant_service_cancel.py:104、:122` | medium | `WorkflowResult` 在两个测试函数内重复局部导入，应上移模块顶部 |
| AUD-14700 | Major | bug | `src/features/edit/ai_editing/timebase.py:91-101（对照 :58-61）` | medium | 时码路径按名义帧率换算，与秒路径的真实帧率帧号体系不一致 |
| AUD-14701 | Major | bug | `src/features/edit/ai_editing/timebase.py:87-96` | high | timecode_to_frames 不校验时码字段域，畸形时码静默映射为错误帧号 |
| AUD-14702 | Suggestion | process | `src/features/edit/ai_editing/timebase.py:106-114` | high | frames_to_timecode 不拒绝负帧输入，产生畸形时码 |
| AUD-14703 | Suggestion | doc-gap | `src/features/edit/ai_editing/timebase.py:276-283` | medium | from_audio_sample 用浮点运算，违背类文档"全程有理数/整数运算"承诺 |
| AUD-14704 | Suggestion | process | `src/features/edit/ai_editing/timebase.py:58-64` | medium | seconds_to_frame_range 纯浮点乘法在整帧边界可能偏 1 帧 |
| AUD-14705 | Suggestion | process | `src/features/edit/ai_editing/timebase.py:187` | high | TimebaseConversionResult.source_range 类型注解为 Any |
| AUD-14706 | Major | bug | `src/features/edit/ai_editing/token_estimator.py:61-75` | medium | ASCII 空白字符被计入 CJK 桶，英文文本 token 估算系统性偏高约 25–30% |
| AUD-14707 | Suggestion | process | `src/features/edit/ai_editing/token_estimator.py:108-112` | high | count_tokens_accurate 内层使用宽泛 `except Exception` |
| AUD-14708 | Suggestion | process | `src/features/edit/ai_editing/token_estimator.py:83-87` | high | estimate_json_tokens 对极深嵌套结构的 RecursionError 未设防 |
| AUD-14709 | Major | bug | `src/features/edit/ai_editing/tool_call_log.py:102-111、136-145` | medium | 脱敏仅按敏感 key 黑名单匹配，值中内嵌的绝对路径/secret 原样进入审计日志 |
| AUD-14710 | Major | doc-gap | `src/features/edit/ai_editing/tool_call_log.py:165-187` | medium | ToolCallLog 无锁且 docstring 自相矛盾，并发记录时 call_id 重复/记录丢失 |
| AUD-14711 | Suggestion | perf-opportunity | `src/features/edit/ai_editing/tool_call_log.py:177-182` | high | FIFO 淘汰用 list.pop(0)，饱和后每条记录 O(n) 搬移 |
| AUD-14712 | Suggestion | process | `src/features/edit/ai_editing/tool_call_log.py:102-111` | high | _redact 不处理 set 等容器类型，原样 repr 进入摘要 |
| AUD-14713 | Major | bug | `src/features/edit/ai_editing/tool_calling_client.py:221-268` | medium | tool result 消息先于 assistant(tool_calls) 消息追加，违反 OpenAI/Anthropic 消息顺序协议 |
| AUD-14714 | Suggestion | process | `src/features/edit/ai_editing/tool_calling_client.py:226-232` | medium | tool 调用预算耗尽时 terminated_reason 误标为 "max_iterations" |
| AUD-14715 | Suggestion | process | `src/features/edit/ai_editing/tool_calling_client.py:241-248` | medium | run_tool_loop 未捕获 invoke_fn 异常，工具异常会击穿整个 loop |
| AUD-14716 | Suggestion | process | `src/features/edit/ai_editing/tool_calling_client.py:202-205` | high | temperature 硬编码 0.7，未提供配置入口 |
| AUD-14717 | Suggestion | process | `src/features/edit/ai_editing/tool_calling_client.py:169、289-297` | high | result_pruner 参数类型注解为 Any |
| AUD-14718 | Major | bug | `src/features/edit/ai_editing/tool_facade.py:753-756、889-898、900-913` | medium | render_preview（EXPENSIVE）与 request_export（FILE_WRITE）完全没有 approval 门禁 |
| AUD-14719 | Suggestion | process | `src/features/edit/ai_editing/tool_facade.py:774-781` | high | invoke 顶层宽泛 `except Exception` 一律映射 SCHEMA_INVALID，掩盖领域错误 |
| AUD-14720 | Suggestion | process | `src/features/edit/ai_editing/tool_facade.py:783-793` | high | _json_safe 每次工具返回都全量 json.dumps 校验一次，大响应双重序列化 |
| AUD-14721 | Suggestion | process | `src/features/edit/ai_editing/tool_facade.py:700-701` | high | invoke 的 plugin 分支每次重建内置工具名集合 |
| AUD-14722 | Suggestion | process | `src/features/edit/ai_editing/tool_facade.py:821-833、731-733` | medium | validate_edit_plan / get_composite_snapshot 返回未经 sanitize_untrusted 标记 |
| AUD-14723 | Suggestion | process | `src/features/edit/ai_editing/tool_facade.py:889-897` | medium | _render_preview 忽略 schema 声明的 range 参数；active_timeline 为空串时语义错误 |
| AUD-14724 | Suggestion | process | `src/features/edit/ai_editing/tool_facade.py:960-962、1016-1018` | high | search_help / get_render_diagnostics 的 limit 参数未钳制 |
| AUD-14725 | Major | bug | `src/features/edit/ai_editing/tool_invocation.py:108-131` | medium | result_from_facade 把缺少 "ok" 键的成功返回一律改判为 FAILURE 并丢弃数据 |
| AUD-14726 | Major | bug | `src/features/edit/ai_editing/tool_invocation.py:426-440` | medium | 超时为事后判定，已成功的副作用被改判为 TOOL_TIMEOUT 失败 |
| AUD-14727 | Suggestion | process | `src/features/edit/ai_editing/tool_invocation.py:161-172（对照 tool_facade.py:635-667）` | medium | 模块级 tool_execution_mode 与 facade.execution_mode 双实现，易漂移 |
| AUD-14728 | Suggestion | process | `src/features/edit/ai_editing/tool_invocation.py:656-663` | low | record_skipped 只发 RUN_TOOL_FINISHED，事件可能不成对 |
| AUD-14729 | Suggestion | process | `src/features/edit/ai_editing/tool_invocation.py:493-502、317-319、357-359` | high | _invoke_facade 每次调用 inspect.signature 未缓存；time_fn/cancel_check/approval 注解为 Any |
| AUD-14730 | Suggestion | process | `src/features/edit/ai_editing/tool_invocation.py:372-378` | medium | 非 Mapping 的 args 未经校验直接透传给 facade |
| AUD-14800 | Major | bug | `src/features/edit/ai_editing/tool_operation_ledger.py:49-54` | medium | 指纹规范化对字符串/容器做静默截断，不同参数可碰撞出相同指纹导致错误 REPLAY |
| AUD-14801 | Suggestion | process | `src/features/edit/ai_editing/tool_operation_ledger.py:55` | medium | `_canonical` 对不可序列化对象回退 `str(value)`，跨进程指纹不稳定，重启后去重失效 |
| AUD-14802 | Suggestion | process | `src/features/edit/ai_editing/tool_operation_ledger.py:110-125` | medium | `ToolResult` 的反序列化逻辑在 ledger 内手写重复实现，与 `ToolResult.to_dict` 无对称 `from_dict` |
| AUD-14803 | Suggestion | risk | `src/features/edit/ai_editing/tool_result.py:130` | medium | `to_dict` 的 `artifact_refs` 未过 `sanitize_for_storage`，与模块自述的"审计事件绝不泄漏绝对路径"规则不一致 |
| AUD-14804 | Suggestion | process | `src/features/edit/ai_editing/tool_result.py:77-83` | medium | `Diagnostic.from_dict` 未校验 severity 取值，持久化回读可注入任意严重级 |
| AUD-14805 | Suggestion | process | `src/features/edit/ai_editing/tool_result_pruner.py:55` | medium | `render` 用 `default=str` 序列化任意对象，与"确定性输出"承诺相悖 |
| AUD-14806 | Major | risk | `src/features/edit/ai_editing/toposctl.py:321（另 337、373）` | medium | 数值型选项 `int()` 抛出的 ValueError 不在任何 except 列表内，CLI 以 traceback 崩溃而非 exit 2 |
| AUD-14807 | Major | bug | `src/features/edit/ai_editing/toposctl.py:437（配合 130）` | medium | `main()` 不传 `call_log`，审计写入随进程退出即丢弃的临时内存日志 |
| AUD-14808 | Suggestion | process | `src/features/edit/ai_editing/toposctl.py:249-255` | high | 离线 capabilities 的审计写入失败被 `except Exception: pass` 完全静默 |
| AUD-14809 | Suggestion | risk | `src/features/edit/ai_editing/toposctl.py:423-426` | high | `--log-level` 非法值使 `setLevel` 抛 ValueError，`main()` 无捕获直接崩溃 |
| AUD-14810 | Suggestion | process | `src/features/edit/ai_editing/toposctl.py:311-324（snapshot；search/help 同构 327-340、365-376）` | medium | 手写参数解析静默吞掉未知 flag 与悬空选项值，拼写错误无反馈 |
| AUD-14811 | Major | doc-gap | `src/features/edit/ai_editing/transaction_service.py:536-537（与 docstring 434-438 矛盾）` | medium | `commit` 文档承诺的"直接传 ApprovalService 自动构造 scope 并 consume"路径，实现恒返回 None |
| AUD-14812 | Major | bug | `src/features/edit/ai_editing/transaction_service.py:390-401` | medium | simulate 阶段 `except Exception` 一律标 SCHEMA_INVALID，宽泛捕获掩盖领域错误并给出错误诊断码 |
| AUD-14813 | Major | bug | `src/features/edit/ai_editing/transaction_service.py:213-221（载荷 186-199）` | medium | `compute_diff` 单标签分类：跨轨移动+时长变化只报 "trimmed"，且 before/after 载荷不含 track，批准 UI 看不到轨道变更 |
| AUD-14814 | Suggestion | process | `src/features/edit/ai_editing/transaction_service.py:409` | high | `compute_diff` 在 simulate 的 try 块之外调用，其异常以裸异常逃逸 `preview` |
| AUD-14815 | Suggestion | process | `src/features/edit/ai_editing/transaction_service.py:138-149` | medium | `TimelineDiff/ClipDiff.from_dict` 未校验持久化数据形状，畸形值可被拆成字符元组或抛 AttributeError |
| AUD-14816 | Suggestion | process | `src/features/edit/ai_editing/transaction_service.py:554、572-576` | high | `_record_audit` 的 sink 异常被 `except Exception: pass` 静默，且函数内 `DiagnosticSeverity` 为死导入 |
| AUD-14817 | Suggestion | risk | `src/features/edit/ai_editing/transaction_service.py:458-486` | medium | grant 在乐观锁检查之前被 consume，revision 冲突时一次性授权被白白烧掉 |
| AUD-14900 | Major | bug | `src/features/edit/ai_editing/transcript_provider.py:446（配合 1116-1148）` | medium | analyze() 调用 merge_diarization 未传 media_fps_ratio，帧坐标与 16kHz sample 坐标直接比较，speaker 归属必然错误 |
| AUD-14901 | Major | risk | `src/features/edit/ai_editing/transcript_provider.py:318-325, 702-765, 767-798, 800-831` | medium | 惰性引擎缓存（_asr_engine/_aligner/_diar_model 等）无锁，AnalysisScheduler 的 ThreadPoolExecutor 并发调用 analyze 时存在竞态 |
| AUD-14902 | Major | bug | `src/features/edit/ai_editing/transcript_provider.py:666-674` | medium | _run_diarization 只捕获导入类异常，Sortformer 运行期错误会击溃整次已成功的转录 |
| AUD-14903 | Suggestion | process | `src/features/edit/ai_editing/transcript_provider.py:324-325, 1056-1058` | medium | set_progress_callback 注入的 _progress_cb 从未被调用，是死注入点 |
| AUD-14904 | Suggestion | risk | `src/features/edit/ai_editing/transcript_provider.py:406-407` | high | analyze() 写入的 thread_local.request_options 在调用结束后不清理，线程池线程复用会残留上一次请求的选项 |
| AUD-14905 | Suggestion | process | `src/features/edit/ai_editing/transcript_provider.py:907-914` | medium | _audio_sample_count 宽泛 except 后静默假设 60 秒，产生无标注的错误时长 |
| AUD-14906 | Suggestion | process | `src/features/edit/ai_editing/transcript_provider.py:1219, 1247` | medium | 词级时间戳置信度硬编码 0.95，虚增下游 router 的质量评分 |
| AUD-14907 | Suggestion | process | `src/features/edit/ai_editing/transcript_provider.py:1173` | high | _normalize_language 对未识别语言名截取前两个字符，产出错误 ISO 码 |
| AUD-14908 | Suggestion | process | `src/features/edit/ai_editing/transcript_provider.py:1084, 1177, 1225` | high | merge_diarization 的 sample_rate 参数与两个解析函数的 language 参数是死参数 |
| AUD-14910 | Major | bug | `src/features/edit/ai_editing/transcript_router.py:80-99` | medium | 强制"去除背景音乐"失败时静默降级用原声，provenance 不标注降级，且与"分离器不可用"路径的硬失败策略不一致 |
| AUD-14911 | Suggestion | process | `src/features/edit/ai_editing/transcript_router.py:50-63` | medium | validate_options 不区分 mode，whisperx 模式下仍按 Qwen pack 校验，可能误拒合法请求 |
| AUD-14912 | Suggestion | risk | `src/features/edit/ai_editing/transcript_router.py:245-257` | medium | ProfileAwareTranscriptProvider._remote_by_profile 缓存字典无锁，并发 analyze 可重复创建 remote provider |
| AUD-14913 | Major | bug | `src/features/edit/ai_editing/tts_provider.py:159-162, 209-211, 291-293（消费方 src/app/controllers/edit_controller.py:1356-1368）` | medium | 单条 cue 合成失败被 continue 跳过后，cue_durations 与字幕 cue 的索引对应关系断裂，controller 按索引配对会错位 |
| AUD-14914 | Suggestion | process | `src/features/edit/ai_editing/tts_provider.py:373-383` | medium | _has_model_weights 尾部有 10 行不可达死代码，且引用了静态方法中不存在的 self/pack_id |
| AUD-14915 | Suggestion | process | `src/features/edit/ai_editing/tts_provider.py:252, 267-272, 291-293` | medium | _lay_down_waves 前段含停顿的 cue_durations 记账在末尾被整体覆盖，注释"停顿计入该 cue 时长"与实际返回值矛盾 |
| AUD-14916 | Suggestion | process | `src/features/edit/ai_editing/tts_provider.py:260-264` | medium | np.asarray 不拷贝时 50ms 淡入会就地改写引擎返回的波形缓冲 |
| AUD-14917 | Suggestion | process | `src/features/edit/ai_editing/tts_provider.py:221-228` | high | _lay_down_waves 的 cues 与 fps 参数从未使用 |
| AUD-14918 | Suggestion | risk | `src/features/edit/ai_editing/tts_provider.py:61-62, 319-334` | medium | _engines 引擎缓存无锁，并发 synthesize 可重复加载 3.8GB 权重 |
| AUD-14919 | Suggestion | process | `src/features/edit/ai_editing/tts_settings.py:98-114` | medium | 高级生成参数无任何范围校验，非法值直接透传给模型 |
| AUD-14920 | Major | risk | `src/features/edit/ai_editing/variant_planner.py:235-263, 341` | medium | 用户锁定的 source range 在选段中完全未被使用（locked 参数是死参数），评分却硬编码 hard_constraints_passed=True |
| AUD-14921 | Suggestion | process | `src/features/edit/ai_editing/variant_planner.py:306-311` | medium | _compile_plan 用 __import__ 动态导入并伪造 TRANSCRIPT 类型的 SemanticSegment 生成 intent 元数据 |
| AUD-14922 | Suggestion | process | `src/features/edit/ai_editing/variant_planner.py:333-335` | medium | duration_fit 度量的是分配的 beat 区间总和而非实际选段时长，恒≈1.0 失去排序意义 |
| AUD-14923 | Suggestion | process | `src/features/edit/ai_editing/variant_planner.py:134-141` | medium | on_plan_compiled 回调抛出的异常会中断其余策略的规划循环 |
| AUD-1500 | Suggestion | process | `src/shared/gpu/metal/kernels_idct.py:54-623 与 625-1195` | medium | 两个 MSL 源常量约 570 行逐字重复，存在版本漂移风险 |
| AUD-15000 | Major | bug | `src/features/edit/ai_editing/visual_semantic_provider.py:367-386` | medium | 装配阶段宽泛 `except Exception` 把真实错误统一伪装成“缺模型包” |
| AUD-15001 | Major | bug | `src/features/edit/ai_editing/visual_semantic_provider.py:196-222（配合` | medium | `analyze` 在推理前一次性物化全部关键帧解码图像，长素材内存峰值失控 |
| AUD-15002 | Suggestion | process | `src/features/edit/ai_editing/visual_semantic_provider.py:425-430` | high | `_keyframe_image_file.__exit__` 只容忍 FileNotFoundError，清理异常会顶替原始推理异常 |
| AUD-15003 | Suggestion | process | `src/features/edit/ai_editing/visual_semantic_provider.py:98-110, 131-136` | high | Caption/Tag/Embedding 后端硬编码置信度，模型分数被丢弃 |
| AUD-15004 | Suggestion | process | `src/features/edit/ai_editing/visual_semantic_provider.py:210-222（配合 :72-74` | medium | 推理循环对单帧解码失败 / 单 backend 异常无隔离，一处失败丢弃整次分析 |
| AUD-15005 | Suggestion | process | `src/features/edit/ai_editing/visual_semantics.py:358, 408-410` | high | `_safe_id` 清洗后不同 asset_id 可碰撞，`put` 互相覆盖缓存文件 |
| AUD-15006 | Suggestion | process | `src/features/edit/ai_editing/visual_semantics.py:371-379` | medium | `VisualCacheStore.load` 不校验 `schema_version` 与 fingerprint |
| AUD-15007 | Major | bug | `src/features/edit/ai_editing/whisperx_provider.py:136-150` | medium | word 级时间戳未做外部数据校验，一条畸形 word 使整次转录失败 |
| AUD-15008 | Major | risk | `src/features/edit/ai_editing/whisperx_provider.py:88-95, 112-122` | medium | 共享 provider 实例下 `_engine` 懒加载无锁，且并发分析共用同一模型对象 |
| AUD-15009 | Suggestion | process | `src/features/edit/ai_editing/whisperx_provider.py:75-95` | medium | `progress` 参数被接受但从未上报，长转录期间任务进度停滞 |
| AUD-1501 | Suggestion | process | `src/shared/gpu/metal/kernels_idct.py:440-446（ws: params[7]=off_stride, buffer(8)=row_slice）、545-546（wsd: params[7]=dc0）、1103-1104（wsd_v8: buffer(8)=blkseg）` | medium | params[7] 与 buffer(8) 槽位语义在不同 kernel 间漂移，装配侧易错 |
| AUD-15010 | Suggestion | process | `src/features/edit/ai_editing/whisperx_provider.py:87-95` | medium | 长转录无协作取消检查点，取消后仍占满 CPU 直至转录结束 |
| AUD-15011 | Major | bug | `src/features/edit/ai_editing/workflow.py:524（` | medium | 字幕/多语言 artifact 保存未处理 `ArtifactOverrideError`，用户编辑后重跑工作流以异常而非降级结果终止 |
| AUD-15012 | Suggestion | process | `src/features/edit/ai_editing/workflow.py:666-677` | medium | 字幕 artifact 硬编码 `revision=0` / `brief_revision=0` / `created_at=""`，与多语言路径不一致 |
| AUD-15013 | Suggestion | process | `src/features/edit/ai_editing/workflow.py:638-647` | medium | `_brief_revision` 对属性式 `current_revision` 静默返回 0，且 `except Exception` 吞掉 brief 服务错误 |
| AUD-15014 | Suggestion | process | `src/features/edit/ai_editing/workflow.py:378-403` | medium | `validate_input` 不校验 `base_revision` 与 `brief_id` |
| AUD-15015 | Suggestion | process | `src/features/edit/ai_editing/workflow.py:485-490（对比 :556-563）` | high | `_run_variant_workflow` 中 planner 异常未转为降级结果，与 `_run_multilingual` 的处理不一致 |
| AUD-15016 | Suggestion | process | `src/features/edit/ai_editing/workflow_transaction.py:201-207` | medium | `add_step` 对 inputs/outputs 只做浅拷贝，嵌套结构事后可变导致审计记录失真 |
| AUD-1502 | Suggestion | process | `src/shared/gpu/metal/kernels_idct.py:1035-1038、1043（ws）；569-572（wsd）；249-250、260（sp）` | low | CSR 偏移与 sblk0 无下溢/越界守卫，完全依赖上游装配契约 |
| AUD-1510 | Major | bug | `src/shared/gpu/metal/kernels_video.py:109-111` | high | 位深仅校验下限（bd < 8），11–16 位深静默通过并生成错误码值域参数 |
| AUD-15100 | Suggestion | process | `src/features/edit/commands_facade.py:16-17,35,72,91,110,123,146,162,178,191,204,217` | medium | 全部 12 个公开函数缺少有意义的返回类型注解，且含空 TYPE_CHECKING 块 |
| AUD-15101 | Suggestion | process | `src/features/edit/commands_facade.py:31,68,87,106,119,142,158,175,188,201,214,248` | medium | ImportError 降级日志不含异常详情，会把真实的导入缺陷（如循环导入、名称改错）伪装成"模块不可用" |
| AUD-15102 | Suggestion | process | `src/features/edit/commands_facade.py:22-32` | medium | create_teaching_animation_clip_command 无生产调用方，且生产代码绕过门面直接 deep-import，违背本模块自述的边界约定 |
| AUD-1511 | Suggestion | process | `src/shared/gpu/metal/kernels_video.py:109` | medium | 非整数位深被 `int()` 静默截断 |
| AUD-1520 | Major | risk | `src/shared/gpu/metal/submission_scheduler.py:68、96-97、133-139` | medium | drain() 可快照到 submit() 预留的 None 槽并解引用 `.command_buffer`，关机路径崩溃 |
| AUD-15200 | Suggestion | doc-gap | `src/features/edit/core/__init__.py:6-14` | medium | 模块 docstring 职责清单与实际导出漂移 |
| AUD-15201 | Suggestion | process | `src/features/edit/core/adjustment_composite.py:22-26` | medium | transform_is_identity 将 pivot_x/pivot_y 计入恒等判定，center 模式下残留 pivot 值触发逐帧全帧 warp |
| AUD-15202 | Suggestion | process | `src/features/edit/core/adjustment_composite.py:119-125` | medium | blend_adjustment_cpu coverage 掩码分配并变换整个多通道 float32 帧，仅消费 1 个通道 |
| AUD-15203 | Major | bug | `src/features/edit/core/audio_capabilities.py:167,184-203` | medium | 缓存命中时静默忽略 check_output_device=True，跳过的设备探测被缓存为 available=True（未知被当作可用） |
| AUD-15204 | Suggestion | risk | `src/features/edit/core/audio_capabilities.py:198-203` | medium | get/reset 双检锁竞态可向调用方返回 None |
| AUD-1521 | Major | risk | `src/shared/gpu/metal/submission_scheduler.py:86-94` | medium | on_complete 回调异常未捕获，在 Metal completion 线程上逃逸 |
| AUD-1522 | Suggestion | process | `src/shared/gpu/metal/submission_scheduler.py:61-62、103-107` | high | submit() 返回 None 的语义二义：容量满 vs context 提交失败 |
| AUD-1523 | Suggestion | risk | `src/shared/gpu/metal/submission_scheduler.py:84、116-121、123-125` | medium | _completed/_errors 无界增长，依赖调用方自觉 poll |
| AUD-1524 | Suggestion | process | `src/shared/gpu/metal/submission_scheduler.py:135-139` | medium | drain() 不清理完成后的记账残留，wait_fn 异常中断剩余等待 |
| AUD-1525 | Suggestion | process | `src/shared/gpu/metal/submission_scheduler.py:21、32、71、97` | high | command_buffer 以裸 Any 贯穿全模块，弱化类型契约 |
| AUD-15300 | Major | bug | `src/features/edit/core/audio_dsp.py:1569-1580` | medium | ReverbEffect.set_params：predelay/采样率变化分支用旧 decay/damping 重建滤波器，新值永不应用到 comb |
| AUD-15301 | Major | perf-opportunity | `src/features/edit/core/audio_dsp.py:1771-1783` | medium | DelayEffect.set_params：time/采样率变化分支用旧 low_cut_hz 重算 highpass 系数，新 lowcut 值被吞 |
| AUD-15302 | Major | bug | `src/features/edit/core/audio_dsp.py:820-823` | medium | StripEQFilter.set_params：mode 切换的状态保留过滤条件恒为空集，Low-Cut/High-Cut 状态也被误清空 |
| AUD-15303 | Major | risk | `src/features/edit/core/audio_dsp.py:1829-1852` | medium | DelayEffect.process：ping-pong 模式在奇数声道（≥3）时 swap 索引越界，渲染线程 IndexError |
| AUD-15304 | Major | risk | `src/features/edit/core/audio_dsp.py:1193-1199, 1222` | medium | FX 效果器 dict 参数无范围校验，CompressorEffect ratio=0 时渲染线程 ZeroDivisionError |
| AUD-15305 | Suggestion | process | `src/features/edit/core/audio_dsp.py:71-74` | high | _biquad_lowshelf 中 alpha 的首次赋值是死代码，易误导维护者 |
| AUD-15306 | Suggestion | process | `src/features/edit/core/audio_dsp.py:256, 280, 289, 311` | high | pan 系列函数返回值为裸 tuple 注解，缺少元素类型 |
| AUD-15307 | Suggestion | process | `src/features/edit/core/audio_dsp.py:881-905` | medium | StripEQFilter.process 只判 _scipy_available()，未防模块级 sosfilt 为 None，与文件其余处的防护不一致 |
| AUD-15308 | Suggestion | process | `src/features/edit/core/audio_dsp.py:2412-2424, 2485` | medium | MasterLimiter 输入验证与其余效果器不一致：构造器不 clamp threshold、不防 0 声道块 |
| AUD-15400 | Major | risk | `src/features/edit/core/audio_graph_renderer.py:77, 170, 310-339` | medium | 节点持有模型 KeyframeTrack 活引用，导出线程与 UI 编辑并发读写竞态 |
| AUD-15401 | Major | bug | `src/features/edit/core/audio_graph_renderer.py:458` | medium | render_bus_stage 丢弃 cycle_buses，环中 Bus 音频静默丢失且无日志 |
| AUD-15402 | Major | bug | `src/features/edit/core/audio_graph_renderer.py:405-418, 461-463, 547` | medium | 普通/Bus 轨路由到未知 bus 目标时行为不一致，普通轨音频写入孤儿缓冲后静默丢弃 |
| AUD-15403 | Suggestion | process | `src/features/edit/core/audio_graph_renderer.py:161-173` | medium | configure_from_sequence 不清空旧节点/限幅器状态，非幂等 |
| AUD-15404 | Suggestion | process | `src/features/edit/core/audio_graph_renderer.py:526-531` | high | Bus pan automation 逐样本 Python 循环，导出热路径未向量化 |
| AUD-15405 | Suggestion | process | `src/features/edit/core/audio_graph_renderer.py:356-364` | medium | mono 源 pan ramp 未 clip 到 [-1,1]，与 stereo 路径及静态路径不一致 |
| AUD-15406 | Suggestion | process | `src/features/edit/core/audio_graph_renderer.py:200, 427` | high | `audible: set = None` 类型注解应为 Optional[set] |
| AUD-15410 | Major | doc-gap | `src/features/edit/core/audio_graph_snapshot.py:322-327, 329-331, 351-359` | medium | SnapshotHistory 无锁，却按文档契约供播放线程跨线程消费并遍历 live sequence |
| AUD-15411 | Suggestion | process | `src/features/edit/core/audio_graph_snapshot.py:104-131, 351-359` | medium | needs_rebuild 每次全量重建快照 + sha256，作音频 block 边界调用成本过高 |
| AUD-15412 | Suggestion | process | `src/features/edit/core/audio_graph_snapshot.py:133-152` | high | diff() 完全不比较 clips 字段 |
| AUD-15420 | Major | bug | `src/features/edit/core/audio_health.py:158-168, 171-183, 231-236` | medium | has_blocking 的 DSP 分支是死代码（pass），DSP 后端缺失既不阻断也不告警 |
| AUD-15421 | Major | bug | `src/features/edit/core/audio_health.py:268` | medium | output_device_available 可能取自"跳过探测"的缓存默认值，健康报告误报设备 OK |
| AUD-15422 | Suggestion | process | `src/features/edit/core/audio_health.py:54-63, 73-84, 285-286` | medium | 磁盘/目录探测使用无日志的宽泛 except，失败时降级为"未知"且不阻断 |
| AUD-15423 | Suggestion | risk | `src/features/edit/core/audio_health.py:74-82` | medium | probe_dir_size_mb 同步 os.walk 整个缓存目录，大目录会卡住调用线程 |
| AUD-15424 | Suggestion | process | `src/features/edit/core/audio_health.py:245` | high | build_audio_health_report 的 sequence 参数缺类型注解 |
| AUD-15501 | Major | risk | `src/features/edit/core/audio_playback.py:143-163, 862, 1064-1075` | medium | 全局 `_av_resampler_cache` 无界增长，且以 `id(self)` 作键存在复用污染 |
| AUD-15502 | Suggestion | process | `src/features/edit/core/audio_playback.py:287-290, 619, 937-938` | high | 类型注解缺失/裸泛型（维度 9） |
| AUD-15503 | Suggestion | process | `src/features/edit/core/audio_playback.py:949-956, 1116-1117, 1788-1791, 2325-2327, 3166-3169, 3273-3275` | medium | 多处宽泛/静默 `except`，其中 `ring.clear()` 失败被吞会掩盖重复播放旧样本的缺陷 |
| AUD-15504 | Major | bug | `src/features/edit/core/audio_playback.py:1027-1032, 1058-1062` | medium | `AudioDecoder.open()` 失败路径不关闭容器，`is_open` 语义被破坏 |
| AUD-15505 | Suggestion | risk | `src/features/edit/core/audio_playback.py:1510-1516, 1539-1543` | medium | mixer 锁内执行 `decoder.close()`（文件 IO），音频线程可能被 UI 操作拖住 |
| AUD-15506 | Suggestion | perf-opportunity | `src/features/edit/core/audio_playback.py:2013-2019, 2083, 2128, 2151, 2208, 2412` | medium | mix 热路径存在逐块 import、逐块 solo 闭包重算与 ramp 分配（维度 8） |
| AUD-15507 | Suggestion | process | `src/features/edit/core/audio_playback.py:2528-2529, 2562-2564, 2632, 2676, 2861` | medium | AudioOutput 死代码：`_buffer`/`_buffer_mutex` 从未用于读写，`buffer_low`/`sync_error` 信号从未发射 |
| AUD-15508 | Major | bug | `src/features/edit/core/audio_playback.py:3041-3043, 2716-2721, 3084` | medium | 音频输出启动失败或设备中途丢失后，播放状态残留为 playing，无恢复也无通知 |
| AUD-15509 | Major | bug | `src/features/edit/core/audio_playback.py:3046-3065` | medium | 设备格式回退后只同步时钟/帧率，解码器输出采样率与混音器采样率未同步，音频变调变速 |
| AUD-15510 | Major | risk | `src/features/edit/core/audio_playback.py:3260-3266, 3084-3085, 3214-3217` | medium | 直混兜底路径的 `mix_samples` 未被 try 包裹，异常会杀死整个播放线程 |
| AUD-15511 | Suggestion | perf-opportunity | `src/features/edit/core/audio_playback.py:3230-3241, 3298-3311` | medium | 反压分支的 `msleep(5)` 位于"仅首次"日志守卫内，后续阻塞周期不再睡眠 |
| AUD-15512 | Major | bug | `src/features/edit/core/audio_playback.py:3257, 3289-3311` | medium | 写入失败时 ring 中已读出的样本被直接丢弃，造成音频跳变 |
| AUD-15513 | Major | risk | `src/features/edit/core/audio_playback.py:3625-3634, 4232, 4269, 4370` | medium | scrub 预览 worker 与播放启动存在竞态窗口，可在播放中扰动解码器位置并复位 DSP |
| AUD-15514 | Suggestion | risk | `src/features/edit/core/audio_playback.py:4440-4443, 4018-4024` | medium | `stop()`/`quiesce_for_timeline_rebuild()` 从 UI 线程无界 `wait()` 音频线程 |
| AUD-15515 | Major | bug | `src/features/edit/core/audio_playback.py:4416-4428` | medium | `resume()` 无状态守卫，停止态调用会误发 `state_changed("playing")` |
| AUD-15516 | Suggestion | process | `src/features/edit/core/audio_playback.py:4643-4648` | high | `get_state()` 把"暂停在帧 0"误报为 STOPPED |
| AUD-15517 | Major | bug | `src/features/edit/core/audio_playback.py:4650-4660` | high | `get_progress()` 在 duration==1 时除以零 |
| AUD-15518 | Suggestion | process | `src/features/edit/core/audio_playback.py:4760-4785` | medium | `AudioVideoSynchronizer.set_sync_mode()` 重复调用 `setup()` 造成信号重复连接 |
| AUD-15600 | Suggestion | process | `src/features/edit/core/audio_timestretch.py:86-88, 100-102` | medium | `sample_rate` 缺少正值校验，0/负值静默退化为最短帧长 |
| AUD-15601 | Suggestion | risk | `src/features/edit/core/audio_waveform.py:199-208` | medium | `WaveformCache.__new__` 单例无锁双重检查竞态 |
| AUD-15602 | Suggestion | process | `src/features/edit/core/audio_waveform.py:699-701, 1064-1067` | medium | 播放抢占丢弃已完成的波形，恢复后从头全量重解码 |
| AUD-15603 | Suggestion | process | `src/features/edit/core/audio_waveform.py:1530-1532` | high | `generate_placeholder_waveform` 污染全局 `np.random` 且用进程随机 `hash()` |
| AUD-15604 | Suggestion | process | `src/features/edit/core/audio_waveform.py:842-843, 856-864` | high | 解码中途异常被吞后仍标记 `is_complete=True` 并写入磁盘缓存 |
| AUD-15605 | Suggestion | risk | `src/features/edit/core/audio_waveform.py:1101-1107` | medium | `cancel_all` 在 GUI 线程无超时 `wait()` |
| AUD-15606 | Suggestion | doc-gap | `src/features/edit/core/audio_waveform.py:74-75（文档）vs 616-619（实现）` | medium | `peaks_max` 实为 \|peak\| 幅值包络，与 `WaveformData` 文档语义不符 |
| AUD-15607 | Suggestion | risk | `src/features/edit/core/audio_waveform.py:1079（结合 969-981、719）` | medium | `_on_extraction_failed` 可能清掉新请求刚注册的回调（丢通知竞态） |
| AUD-15608 | Major | bug | `src/features/edit/core/automation_recorder.py:71-97（结合 src/app/controllers/edit_controller.py:2434-2437）` | medium | `simplify_samples` 递归 Douglas-Peucker 无深度保护，噪声之字形采样触发 RecursionError，整段录制被上层静默丢弃 |
| AUD-15609 | Major | bug | `src/features/edit/core/automation_recorder.py:8-9（文档宣称）vs 219-253（实现）（结合 src/shared/keyframe.py:142-155、src/app/controllers/edit_controller.py:2443 附近）` | medium | Write 模式"覆盖整段已录曲线"未实现：默认命令只追加关键帧，旧关键帧残留导致新旧曲线混杂 |
| AUD-15610 | Suggestion | doc-gap | `src/features/edit/core/automation_recorder.py:169-184, 201-205` | medium | `record`/`stop` 无并发保护（当前仅 UI 线程使用，约束未文档化） |
| AUD-15611 | Suggestion | doc-gap | `src/features/edit/core/automation_recorder.py:9（文档）vs 172-179（实现）` | medium | Touch 模式松手后的 release 时间回归未实现（文档描述与行为不符） |
| AUD-15612 | Suggestion | process | `src/features/edit/core/automation_recorder.py:229-234` | high | `param_bounds` 与 KeyframeTrack 值域重复定义，存在漂移风险 |
| AUD-15613 | Suggestion | process | `src/features/edit/core/blend_modes.py:213-222（对照 shaders/blend.glsl:88` | medium | `blend_rgb` 无条件 clip(0,1)（含 normal 模式），GPU normal 分支不 clip——HDR 浮点域复用时 CPU/GPU 将分叉 |
| AUD-15614 | Suggestion | process | `src/features/edit/core/blend_shader.py:33-38（结合 edit_gpu_preview_renderer.py:1625-1628 的无独立 try 的` | medium | `_read_blend_glsl` 吞 OSError 且零日志：blend.glsl 缺失时整个 GPU 预览初始化失败且根因不可见 |
| AUD-15615 | Suggestion | process | `src/features/edit/core/bwf.py:147-152` | high | RIFF 尺寸超过 4 GiB 时被静默 clamp，产出非法 WAV |
| AUD-15616 | Suggestion | process | `src/features/edit/core/bwf.py:105-112, 137-139` | medium | `inject_bext_chunk` 不校验 chunk size 越过文件末尾，畸形输入仍"成功" |
| AUD-15700 | Major | bug | `src/features/edit/core/capability_registry.py:189` | medium | probe_errors 通道永不写入，is_healthy 的"无探测错误"分支为死代码 |
| AUD-15701 | Major | bug | `src/features/edit/core/capability_registry.py:427` | medium | drift 检测把 PARTIAL 折叠为"不可用"，静态表低估方向的分歧永不上报（FCPXML 现已实际漏报） |
| AUD-15702 | Suggestion | process | `src/features/edit/core/capability_registry.py:421` | medium | find_status_drift 对映射前缀在静态表中消失时静默跳过，无任何日志 |
| AUD-15703 | Suggestion | process | `src/features/edit/core/capability_registry.py:158` | high | 音频效果探测失败时用户可见 notes 出现"缺失 -1 项" |
| AUD-15704 | Suggestion | process | `src/features/edit/core/capability_registry.py:11` | medium | 模块头宣称"状态由运行时探测得出，不手写字面量"，实际 11 个条目中仅 2 个状态来自探测 |
| AUD-15800 | Major | bug | `src/features/edit/core/captions/commands.py:834-839（成因在 829-831）` | medium | DemoteGraphicToSubtitleCommand.apply 删除空图形轨后，undo 因轨道缺失静默 no-op，撤销失败丢失图形绑定与片段 |
| AUD-15801 | Suggestion | risk | `src/features/edit/core/captions/commands.py:666-675, 690-705` | medium | PromoteSubtitleToGraphicCommand.apply 兜底路径覆盖 `_created_track_id`，首次新建的空图形轨在异常回退后泄漏且 undo 不清理 |
| AUD-15802 | Suggestion | risk | `src/features/edit/core/captions/commands.py:185-194` | medium | UpdateSubtitleTrackStyleCommand.apply 对零字段变更（no-op 样式）仍因 override=None 的锁定 cue 抛错 |
| AUD-15803 | Suggestion | doc-gap | `src/features/edit/core/captions/commands.py:445-448（声明）, 469-494, 514-525（实现）` | medium | MergeSubtitleClipsCommand 缺少相邻性校验与同 clip 校验，与文档声明不符且同 id 合并报错误导 |
| AUD-15804 | Suggestion | process | `src/features/edit/core/captions/commands.py:878（ApplyCaptionTrackTemplateCommand.apply）、964（SyncCaptionGraphicCommand.apply）` | medium | 两个命令违反模块自述的 `(new_timeline, updated_cmd)` 返回契约，返回裸 timeline |
| AUD-15805 | Suggestion | process | `src/features/edit/core/captions/commands.py:77, 124, 279` | medium | 类型注解问题：`_inner` 默认 None 却标注非 Optional；`_ALLOWED_FIELDS` 未标 ClassVar 被当作 dataclass 字段；apply/undo 普遍缺返回类型注解 |
| AUD-15806 | Suggestion | process | `src/features/edit/core/captions/delivery.py:233-240（list_subtitle_tracks 的 language/label 未做大小写归一在 124-129）` | medium | 交付文件名去重 `used_names` 区分大小写，macOS 大小写不敏感文件系统上会静默互相覆盖 |
| AUD-15807 | Major | bug | `src/features/edit/core/captions/edit_policy.py:192-195（调用方 src/features/edit/ui/timeline_view.py:6308-6317）` | medium | compute_trim_range 将"整体移动"约束混入左缘裁剪（left-trim）上限，cue 与右邻相接时左缘完全无法向内拉 |
| AUD-15808 | Major | bug | `src/features/edit/core/captions/export.py:183-187` | high | _render_scc 的 CEA-608 控制码时序颠倒：cue 开始发 9420+文本、cue 结束才发 942f，且缺少 942c 清屏 |
| AUD-15809 | Major | bug | `src/features/edit/core/captions/export.py:196-208` | medium | _format_scc_time 的 29.97 drop-frame 换算双重补偿：分钟边界取 1798 帧后又对帧内偏移 +2，时间码恒偏约 2 帧且 10 分钟块末尾误跳下一分钟 |
| AUD-15810 | Suggestion | process | `src/features/edit/core/captions/export.py:97-103` | medium | _render_vtt 输出未做 HTML 转义，含 `<`/`&` 的文本会被 WebVTT 解析器当作标记/实体 |
| AUD-15811 | Major | bug | `src/features/edit/core/captions/font_resolver.py:193-208` | medium | 无 Qt 环境下的字体扫描硬编码 4 个族名恒为"可用"，且 glob 漏掉 .otf——preflight 可能漏报缺失字体，违背模块自述目标 |
| AUD-15812 | Suggestion | process | `src/features/edit/core/captions/font_resolver.py:167-174, 336-337` | medium | 多处宽泛 `except Exception`（其一为裸 `pass`）吞掉 Qt 层错误，违反 .clinerules"不得宽泛 except 掩盖领域错误"铁律 |
| AUD-15900 | Major | doc-gap | `src/features/edit/core/captions/graphics.py:836-860` | medium | `cue_animation_envelope` 把 X/Y 两个轴向的位移合并成单一标量返回，违反自身文档契约 |
| AUD-15901 | Suggestion | process | `src/features/edit/core/captions/graphics.py:402-405` | high | `template_parameters` 不按 `published_parameters` 过滤 overrides，任意键静默接受 |
| AUD-15902 | Suggestion | process | `src/features/edit/core/captions/graphics.py:693-697` | high | `render_caption_graphic_rgba` 跨模块导入 `subtitle_renderer` 的私有下划线函数 |
| AUD-15903 | Suggestion | process | `src/features/edit/core/captions/graphics.py:430-432, 792-797` | high | ease-out 在同文件重复实现两次，且残留一个"注释字符串"伪常量 |
| AUD-15904 | Suggestion | process | `src/features/edit/core/captions/graphics.py:543-572` | medium | `renderable_graphic_for_cue` / `animation_opacity` 无生产调用方，且 track-style 解析逻辑与导出路径内联实现重复 |
| AUD-15905 | Suggestion | process | `src/features/edit/core/captions/importer.py:277-287` | medium | 编码回退链终止于 gb18030，非白名单编码的字幕文件可能"成功"解码为乱码而非报错 |
| AUD-15906 | Suggestion | process | `src/features/edit/core/captions/importer.py:197-243` | medium | `_parse_ttml`/`_ttml_text` 递归遍历无深度上限，病态嵌套 XML 触发 RecursionError |
| AUD-15907 | Suggestion | process | `src/features/edit/core/captions/model.py:689-699` | medium | `style_to_full_legacy_fields` 不传递 `base`，按其宣称的 dual-write 用途接线会破坏 legacy 扁平字段的真值 |
| AUD-15908 | Major | bug | `src/features/edit/core/captions/mux.py:24-39` | medium | container="mkv" 生成 `-f mkv`，ffmpeg 没有名为 mkv 的 muxer，命令必然失败 |
| AUD-15909 | Major | bug | `src/features/edit/core/captions/mux.py:32-38` | medium | container="mxf" 搭配 `-c:s srt`，ffmpeg 的 mxf muxer 不支持 srt 字幕流，命令在写头阶段失败 |
| AUD-15910 | Suggestion | process | `src/features/edit/core/captions/mux.py:33-39` | medium | 无条件 `-y` 且不校验输出路径与输入视频相同，存在覆盖源视频的数据丢失路径 |
| AUD-15911 | Suggestion | process | `src/features/edit/core/captions/quality.py:65-69` | medium | `_reading_units` 的 CJK 范围漏掉假名、谚文、CJK 扩展 B 与全角字符，日/韩字幕阅读速度被显著低估 |
| AUD-15912 | Suggestion | process | `src/features/edit/core/captions/render_plan.py:55-58, 120-123` | high | `from_clip` 与 `pivot_texture_local` 各自重复实现相同的 `_value` 解析闭包 |
| AUD-15913 | Suggestion | process | `src/features/edit/core/captions/schema.py:88-94, 118-135` | medium | `migrate_subtitle_clip_dict` 不拒绝负数版本号，负索引会静默命中迁移表错误条目；迁移表类型标注含 None 占位 |
| AUD-16001 | Major | bug | `src/features/edit/core/captions/style_preset.py:337-350, 364-370（字段定义 :67）` | medium | StylePresetRegistry 序列化丢失 category 字段（to_dict/from_dict 往返数据丢失） |
| AUD-16002 | Suggestion | process | `src/features/edit/core/captions/style_preset.py:303-320` | medium | include_builtins=False 时 register 可让用户预设占用 builtin id，且该条目随后无法 remove |
| AUD-16003 | Suggestion | process | `src/features/edit/core/captions/style_preset.py:353, 377-380` | high | MappingLike 别名在使用点之后定义且恒为 Any，类型注解无实际约束 |
| AUD-16004 | Suggestion | process | `src/features/edit/core/captions/style_preset.py:372-373` | high | from_dict 静默丢弃畸形用户预设条目，无任何日志 |
| AUD-16005 | Suggestion | process | `src/features/edit/core/captions/style_preset.py:32-38` | high | 未使用的导入：field、Callable、EMPTY_STYLE |
| AUD-16006 | Major | risk | `src/features/edit/core/captions/translation.py:218-246（网络调用链 :162 → :77-82）` | medium | 命令 apply 内执行实时 LLM 网络调用：redo 非确定性、redo 失败会丢失重做历史、阻塞调用线程 |
| AUD-16007 | Suggestion | process | `src/features/edit/core/captions/translation.py:161-168, 88-89` | medium | 空白源字幕未预校验：单条空 cue 会在 LLM 调用之后才令整轨翻译失败 |
| AUD-16008 | Suggestion | process | `src/features/edit/core/captions/translation.py:81` | medium | max_tokens 按 80 token/条启发式可能截断 JSON 响应导致整批失败 |
| AUD-16009 | Suggestion | process | `src/features/edit/core/captions/translation.py:198, 192, 196` | high | 独立调用 build_translated_subtitle_track 返回的轨道 index 与源轨道重复；label/name 回退表达式重复书写 |
| AUD-1601 | Major | risk | `src/shared/gpu/present_frame.py:162-170（及 29-44 协议图、424-433、439-441）` | medium | TextureLease 状态转移未统一纳入 pool 锁，渲染线程锁外 `mark_ready` 与锁内 `release_all`/`acquire_for_writing` 存在 check-then-act 竞态 |
| AUD-16010 | Suggestion | process | `src/features/edit/core/captions/translation.py:93, 103-115, 218` | medium | 弱类型注解：profile/credential_store 为 Any，workflow 适配器返回 Callable[[tuple[Any, ...], str], tuple[Any, ...]]，apply 缺返回注解 |
| AUD-1602 | Major | risk | `src/shared/gpu/present_frame.py:256、296` | medium | `GpuPresentFrame.extra` 为可变 dict，随 frozen descriptor 跨线程按引用共享 |
| AUD-1603 | Major | bug | `src/shared/gpu/present_frame.py:377-401、424-433` | medium | 消费端消失/迟到帧被丢弃时无 slot 回收路径：READY 态无超时、无 discard API，仅全局 `release_all` 兜底 |
| AUD-1604 | Major | risk | `src/shared/gpu/present_frame.py:346-356` | medium | `register_slot` 重复 slot_id 静默覆盖旧 lease，可致 fence（GLsync）泄漏；且无尺寸/句柄校验 |
| AUD-1605 | Suggestion | process | `src/shared/gpu/present_frame.py:403-410` | medium | `mark_in_viewer` 静默 no-op，Viewer 无法区分"成功进入 IN_VIEWER"与"slot 已过期/不存在" |
| AUD-1606 | Suggestion | perf-opportunity | `src/shared/gpu/present_frame.py:340、456、459-464` | high | `_reuse_violations` 计数器无任何递增路径，`assert_no_reuse_violations` 恒通过，验收机制空转 |
| AUD-1607 | Suggestion | process | `src/shared/gpu/present_frame.py:429-432、209-212` | high | 宽泛 `except Exception` 静默吞异常且无日志（`release_all` 与 `TextureLease.release` 的 fence 清理路径） |
| AUD-1608 | Suggestion | process | `src/shared/gpu/present_frame.py:265、296` | medium | frozen dataclass 含 dict 字段导致 `hash()` 必然抛 TypeError |
| AUD-1609 | Suggestion | process | `src/shared/gpu/present_frame.py:29-44（对 230-244）` | high | 模块头部协议图与实现不符：`make_present_frame(fence=...)` 参数不存在，且图中调用顺序会触发状态检查异常 |
| AUD-1610 | Suggestion | perf-opportunity | `src/shared/gpu/present_frame.py:397-400` | high | acquire 失败热路径日志 f-string 与 `_state_summary()` 急切求值，debug 未启用也每帧分配 |
| AUD-16100 | Major | bug | `src/features/edit/core/channel_layout.py:380-385（模块头 docstring 第 10 行同错）` | medium | bs1770_channel_weights 环绕声道权重 0.59/0.41 与 BS.1770-4 规定的 1.41 不符且左右不对称 |
| AUD-16101 | Major | bug | `src/features/edit/core/channel_layout.py:252-255` | medium | downmix() 结果直接 astype 回整型源 dtype，无削波导致 int16/int32 PCM 溢出回绕 |
| AUD-16102 | Suggestion | process | `src/features/edit/core/channel_layout.py:72-74` | high | channel_labels 返回裸 tuple，注解缺参数化类型 |
| AUD-16103 | Major | bug | `src/features/edit/core/clip_params.py:97-108 / 243-265 / 456-470 / 996-1009（入口 :211-219、:948-957、:1024-1034）` | medium | NaN 输入绕过全部基于比较的范围校验，from_dict 未过滤非有限值 |
| AUD-16104 | Suggestion | process | `src/features/edit/core/clip_params.py:384-390 / 948-957 / 426-431` | medium | CropParams/SpeedParams.from_dict 缺 float()/bool() 强转，SpeedPoint.from_dict 直接下标取必填键 |
| AUD-16105 | Suggestion | process | `src/features/edit/core/clip_params.py:714-719（另 :670-678、:772-779 调用；:585-589 构建）` | medium | smooth 斜坡查表每次调用全量重建，且单次帧映射内重复构建两遍 |
| AUD-16106 | Suggestion | process | `src/features/edit/core/clip_params.py:720-731` | medium | timeline_to_source_frame_pair 在 timeline_frame == 总时长时返回 (0, 1, 0.0)，与 timeline_to_source_frames 的末帧钳制不一致 |
| AUD-16107 | Suggestion | process | `src/features/edit/core/clip_params.py:474-482 / 504-515` | medium | is_normal_speed / effective_duration_factor 忽略 speed_points 且全仓无调用方 |
| AUD-16108 | Suggestion | process | `src/features/edit/core/clip_params.py:984（校验体 :996-1009 未覆盖）` | high | RawParams.black_level 完全无范围与有限性校验 |
| AUD-16109 | Suggestion | process | `src/features/edit/core/clip_params.py:571-573 / 620-622（` | high | 注解引用 "np.ndarray" 但 numpy 仅在函数体内局部导入，get_type_hints 会 NameError |
| AUD-1611 | Suggestion | process | `src/shared/gpu/present_frame.py:295、302、334-335、443、449` | medium | 类型注解使用裸 `tuple`/`dict` 泛型；`_size` 从不参与注册数量约束 |
| AUD-16110 | Major | bug | `src/features/edit/core/chapter_export.py:69-73（配合 :43 的时间码计算）` | medium | format_youtube_chapters 的 auto_first 判定用 int(fps) 截断，NTSC 分数帧率下产生两条 0:00 时间戳 |
| AUD-16111 | Suggestion | doc-gap | `src/features/edit/core/chapter_export.py:9-11（对照 :111-114 实现、:88-91 函数 docstring）` | high | 模块 docstring 称 FFMetadata 使用 TIMEBASE=1/fps 帧号直存，实现为 TIMEBASE=1/1000 毫秒 |
| AUD-16112 | Suggestion | doc-gap | `src/features/edit/core/chapter_export.py:6-8（对照 :65-67）` | medium | docstring 声称 YouTube 要求至少 3 条章节，但 1–2 条时静默输出无效章节列表 |
| AUD-16200 | Critical | bug | `src/features/edit/core/commands.py:9839-9849` | medium | InsertLinkedAssetCommand.undo 手工重建 TimelineSequence 丢弃 subtitle/master 等全部新字段 |
| AUD-16201 | Critical | bug | `src/features/edit/core/commands.py:11106-11120（AddTransition.apply）、11159-11173（AddTransition.undo）、11432-11446（RemoveTransition.apply）、11641-11655 与 11685-11699（UpdateTransition）` | medium | 转场三命令用手工 EditTrack 构造，apply/undo 均重置轨道的 strip_eq/automation/sends/subtitle_clips 等 14+ 字段 |
| AUD-16202 | Critical | bug | `src/features/edit/core/commands.py:10809-10830（对照正确写法 RippleDeleteCommand:10249-10255 用` | medium | BatchDeleteCommand(ripple=True) 的 undo 误调 move_cmd.undo，片段回移全部退化为 no-op，随后恢复被删片段造成重叠损坏 |
| AUD-16203 | Critical | bug | `src/features/edit/core/commands.py:12275-12286` | medium | OverwriteClipCommand.undo 的"清除分裂残留"启发式会删除同轨所有未受影响且 asset 不同的片段 |
| AUD-16204 | Major | bug | `src/features/edit/core/commands.py:3744-3746（CommandStack.undo）、6207-6209（ProjectCommandStack.undo）` | high | CommandStack/ProjectCommandStack 在 undo 抛异常时丢失命令（先 pop 后执行，失败不回填） |
| AUD-16205 | Major | bug | `—` | medium | 系统性缺陷：apply 计算了旧值但不持久化（无 object.__setattr__、不返回 updated 命令），7 类命令 undo 为 no-op/抛错/错位 |
| AUD-16206 | Major | bug | `src/features/edit/core/commands.py:9204-9208（删除）、9217-9227（undo 仅恢复 bus 与 output_bus 路由）` | medium | RemoveBusCommand.apply 删除指向该 Bus 的 sends，undo 不恢复（sends 永久丢失） |
| AUD-16207 | Major | bug | `src/features/edit/core/commands.py:8208-8209（apply 同步）、8216-8227（undo 无同步）` | high | UpdateSubtitleClipCommand：apply 在时序字段变更时同步 caption graphic，undo 不同步（副作用不对称） |
| AUD-16208 | Major | bug | `src/features/edit/core/commands.py:2549-2553` | medium | SwitchMulticamAngleCommand.undo 捕获 ValueError/AttributeError 后原样返回 timeline——栈状态推进但数据未回滚 |
| AUD-16209 | Major | bug | `src/features/edit/core/commands.py:1499-1518` | medium | MoveClipsOverwriteCommand 覆盖裁剪用 int() 截断量化速度映射，分数速度下保留段源窗口系统性漂移 |
| AUD-16210 | Major | bug | `src/features/edit/core/commands.py:10031-10040` | medium | MacroCommand.from_dict 仅识别 3 种子命令，其余静默丢弃——畸形/部分历史反序列化后宏残缺 |
| AUD-16211 | Major | bug | `src/features/edit/core/commands.py:12992-13021（apply 未存` | medium | AddEffectCommand.apply 不持久化生成的 effect 实例，undo 用"最后一个同类型效果"回退查找 |
| AUD-16212 | Major | bug | `src/features/edit/core/commands.py:12414-12423` | medium | AppendClipCommand.apply 用启发式识别"新追加的片段"，同素材多片段时 undo 可能删错片段 |
| AUD-16213 | Major | bug | `src/features/edit/core/commands.py:3974-3982（快照仅 5 个字段）、3988-4025（undo 仅重建该 bin）` | medium | DeleteBinCommand.undo 只恢复 bin 记录本身，未快照/恢复 remove_bin 对子 bin 与资产归属的副作用 |
| AUD-16214 | Suggestion | process | `src/features/edit/core/commands.py:28（` | high | `typing.Tuple` 未导入却在 16+ 处注解中使用 |
| AUD-16215 | Suggestion | process | `全文件 96 处（如 655、1034、2136-2138、3873、3981-3982、4197-4198、7925-7926、8201、8359-8361、14568 等）` | medium | 96 处 `object.__setattr__` 改写 frozen dataclass，模块级不可变契约名存实亡 |
| AUD-16216 | Suggestion | process | `MoveClipToExistingTrackCommand:1156（无 to_dict/from_dict）、RetimeMotionGraphicClipCommand:1815、SwitchMulticamAngleCommand:2443、UpdateProxyPolicyCommand:5204（无 to_dict/from_dict）、SetClipLabelColorCommand:15258、SetClipWaveformDisplayCommand:15301、SetTimelineRangeCommand:15364、ToggleClipEnabledCommand:15400、SetClipLinkedCommand:15443（后五者为普通 class，均无序列化）` | medium | 9 个命令类缺少 from_dict（其一连 to_dict 都没有），历史一旦持久化将丢失/变形 |
| AUD-16217 | Suggestion | process | `src/features/edit/core/commands.py:8086 vs 8100；5160-5170、5316-5327、5434-5446` | medium | 反序列化默认值与工厂默认值不一致；多类 to_dict 丢弃旧值快照 |
| AUD-16218 | Suggestion | process | `src/features/edit/core/commands.py:7393-7397（ApplyMixSnapshot strip_eq）、8902-8906、8917-8921、9027-9031、9092-9096（automation 快照恢复）、9243-9246、9423-9427（from_dict）` | high | 多处 `except Exception: pass` 静默吞掉反序列化/恢复失败 |
| AUD-16219 | Suggestion | process | `src/features/edit/core/commands.py:3684-3728（CommandStack 无 merge/revision）、7692-7697（AddTrackCommand.undo 位置回退）、9598-9605（MoveTrackCommand 越界时静默 no-op）` | medium | 栈无命令合并（coalesce）与 revision 计数；AddTrackCommand/MoveTrackCommand 的 undo 回退按位置猜测 |
| AUD-16300 | Suggestion | process | `src/features/edit/core/comp_clip_data.py:275-278（结合 :88-93）` | medium | from_dict 对显式 JSON null 字段抛 TypeError 而非受控校验错误 |
| AUD-16301 | Suggestion | process | `src/features/edit/core/comp_clip_data.py:271-273` | medium | from_dict 静默再生缺失的 comp_clip_id/asset_id/graph_id，破坏跨对象关联 |
| AUD-16302 | Suggestion | process | `src/features/edit/core/comp_clip_data.py:20` | high | 未使用的导入 `field` |
| AUD-16303 | Suggestion | process | `src/features/edit/core/composite_failure_policy.py:26-31` | high | 策略返回值为裸 dict，缺少类型参数/TypedDict |
| AUD-16304 | Major | bug | `src/features/edit/core/composite_graph_data.py:139-141（with_name，其余 7 个 with_* 同型）` | medium | frozen 数据类的 with_* 方法浅拷贝共享 nodes/connections 等可变容器，"不可变"契约可被外部突变破坏 |
| AUD-16305 | Major | risk | `src/features/edit/core/composite_graph_data.py:454-455（另 :495-505 connections/width 同类）` | medium | from_dict 对 null 字段崩溃：nodes=None 直接进 _normalize_nodes_format 迭代抛 TypeError |
| AUD-16306 | Suggestion | process | `src/features/edit/core/composite_graph_data.py:924-927（对照 :122-126）` | medium | validate()/is_valid() 与 __post_init__ 豁免规则不一致，空白合成恒判"无效" |
| AUD-16307 | Suggestion | process | `src/features/edit/core/composite_graph_data.py:132-391` | high | 8 个 with_* 方法手工复制全部 24 个字段，应改用 dataclasses.replace |
| AUD-16308 | Suggestion | process | `src/features/edit/core/composite_graph_data.py:602-604（:736-737 同型）` | medium | 默认图节点 ID 截断为 8 位十六进制（32 位），存在碰撞风险 |
| AUD-16309 | Critical | bug | `src/features/edit/core/consolidate.py:430（死变量）、:474-477、:524-525` | medium | 分段输出文件名无去重，同名素材分段相互覆盖，归档工程指向错误媒体内容 |
| AUD-16310 | Major | bug | `src/features/edit/core/consolidate.py:354-356（结合 :533-542 的分支路由）` | medium | remux 的 `-map 0:v`/`-map 0:a` 未加 `?`：纯音频素材两种模式必失败，无音轨视频在 remux 模式必失败 |
| AUD-16311 | Major | risk | `src/features/edit/core/consolidate.py:318-331、:397（调用方证据 src/features/edit/ui/media_pool_panel.py:5652-5655）` | medium | 同步阻塞 API + ffmpeg 默认无超时，唯一调用方在 UI 线程直接调用，整理期间界面冻结且不可取消 |
| AUD-16312 | Suggestion | process | `src/features/edit/core/consolidate.py:546-552、:627-629` | medium | 素材任一分段失败即整体放弃重映射，已成功的分段文件成为归档内孤儿 |
| AUD-16313 | Suggestion | process | `src/features/edit/core/consolidate.py:750-754（结合 :223-224）` | medium | _segment_for 兜底分支可把区间外 source_start 静默映射到错误分段 |
| AUD-16314 | Suggestion | process | `src/features/edit/core/consolidate.py:714-715、:743-744` | high | 多处函数缺返回类型注解 |
| AUD-16315 | Major | bug | `src/features/edit/core/crop_cpu.py:74-78、:130-141（对照 crop_render.py:163-167 与 CROP_FRAGMENT_SHADER:361-366）` | medium | 羽化渐变方向与 crop_render/GPU 参考实现相反：边界处 alpha=0、渐变在矩形内侧，同一 CropParams 在不同渲染路径产生不同可见区域 |
| AUD-16316 | Suggestion | risk | `src/features/edit/core/crop_cpu.py:27-28、:59` | low | crop_is_identity 接受长度<5 的元组但 apply_crop_cpu 解包 5 元，长度恰为 4 时崩溃 |
| AUD-16317 | Suggestion | perf-opportunity | `src/features/edit/core/crop_render.py:236` | high | apply_crop_premultiplied 中 astype 后再 copy 造成每帧冗余全帧分配 |
| AUD-16318 | Suggestion | process | `src/features/edit/core/custom_transitions.py:109-116、:184-185` | medium | reload_user_transitions 仅有 import 时的自调用，"目录变化时调用"未接线；且注册表 clear+重建非原子 |
| AUD-16400 | Suggestion | process | `src/features/edit/core/delivery_qc.py:153-161` | high | reason 分支「素材未在工程中找到」不可达，asset 缺失时误报为「素材标记为离线」 |
| AUD-16401 | Suggestion | process | `src/features/edit/core/delivery_qc.py:149` | high | `is_virtual` 守卫对真实 MediaAsset 恒为 False（模型无该字段），守卫意图未落地 |
| AUD-16402 | Suggestion | process | `src/features/edit/core/delivery_qc.py:199（判定）、360-364（入口未校验）` | high | `av_tolerance_frames` 未验证非负，负容差使任何（含 0 帧）时长差都触发 WARNING |
| AUD-16403 | Suggestion | process | `src/features/edit/core/delivery_qc.py:324-332` | medium | `inspect_output_file` 中 exists 与 getsize 之间存在 TOCTOU，文件消失时抛未捕获 OSError |
| AUD-16410 | Major | risk | `src/features/edit/core/edit_frame_coordinator.py:816-820（cleanup）；443-453（remove_clip 仅减 1 次、到 0 才 close）` | high | cleanup() 每资产只减 1 次引用，ref_count>1 的剪辑在「清理」后永不关闭，泄漏解码器会话 |
| AUD-16411 | Major | risk | `src/features/edit/core/edit_frame_coordinator.py:539` | medium | 播放热路径 request_frame 未传 allow_master_fallback=False，proxy miss 时同步解码 master 将拖死播放线程 |
| AUD-16412 | Major | perf-opportunity | `src/features/edit/core/edit_frame_coordinator.py:591-608` | high | request_frame_sync 忙等循环无 sleep/backoff，超时窗口内 100% 空转一个核心并高频轰炸 service |
| AUD-16413 | Major | risk | `src/features/edit/core/edit_frame_coordinator.py:395（clip_error.emit）、409（clip_loaded.emit），均在` | medium | 持有不可重入锁期间 emit Qt 信号（clip_error/clip_loaded），直连槽回调任一加锁方法即死锁 |
| AUD-16414 | Major | risk | `src/features/edit/core/edit_frame_coordinator.py:392（open_clip）、406（get_clip_info）、453（close_clip），均在` | high | 全局锁内执行重 I/O（open_clip/close_clip/get_clip_info），媒体打开期间阻塞所有并发帧请求 |
| AUD-16415 | Major | risk | `src/features/edit/core/edit_frame_coordinator.py:545-550（锁外 record_request）、765（锁外读` | medium | _stats 多线程读写无锁且锁纪律不一致，并发下统计丢失更新、均值漂移 |
| AUD-16416 | Suggestion | doc-gap | `src/features/edit/core/edit_frame_coordinator.py:234（声明）；全文 emit 仅 395/409/766 三处（clip_error/clip_loaded/stats_updated）` | high | frame_ready 信号已声明并写入文档，但全文无任何 emit，连接方永远收不到帧 |
| AUD-16417 | Suggestion | process | `src/features/edit/core/edit_frame_coordinator.py:543` | high | cache_hit 以 `frame is not None` 判定，真实解码（未命中缓存）也计入命中，cache_hit_rate 失真 |
| AUD-16418 | Suggestion | process | `src/features/edit/core/edit_frame_coordinator.py:324-340` | medium | set_playback_controller 不解除旧控制器连接，重复设置导致双份信号处理 |
| AUD-16419 | Suggestion | doc-gap | `src/features/edit/core/edit_frame_coordinator.py:732-758` | high | _select_quality 未实现文档承诺的「逐帧移动→master」「长时间停帧→升级 master」规则，参数未使用 |
| AUD-16420 | Suggestion | process | `src/features/edit/core/edit_frame_coordinator.py:501（形参）、539（透传）；映射行为见 src/shared/video/frame_provider_service.py:125-141` | high | quality 参数未校验，非法字符串经 service 静默映射为 AUTO |
| AUD-16421 | Suggestion | process | `src/features/edit/core/edit_frame_coordinator.py:772-785` | high | get_stats 返回类型注解 `dict[str, float \| int]` 与实际嵌套 dict 结构不符 |
| AUD-16422 | Suggestion | risk | `src/features/edit/core/edit_frame_coordinator.py:687-697、716-717、724-725（无锁写）；518-535（锁内经 _select_quality 读）` | high | _is_paused/_current_quality 在 _on_state_changed、on_pause、on_play 无锁写，request_frame 锁内读，锁纪律不一致 |
| AUD-16500 | Critical | risk | `src/features/edit/core/edit_gpu_decode_manager.py:2984-3016（invalidate_all_sources；close_source:2878-2911、close_all:2926-2969 同构）` | medium | close/invalidate 三条关闭路径无视 P2-09 租约，解码中途关闭 VT/FFmpeg 会话 → C 层 use-after-free（SIGSEGV）风险 |
| AUD-16501 | Major | risk | `src/features/edit/core/edit_gpu_decode_manager.py:751-756（线程创建，未注册）；2875-2876 / 2923-2924 / 2979-2980（close 时对 preposition 事件仅 0.12/0.3s 等待）` | medium | 交互预置位线程未纳入关闭等待注册表，仅 0.12–0.3s 协作等待，与播放预取线程的 2s join 保护不一致 |
| AUD-16502 | Major | risk | `src/features/edit/core/edit_gpu_decode_manager.py:331-335（check-then-add，无锁）；355（解码线程 finally 中 discard）` | medium | `_playback_prefetch_pending` 检查-添加非原子，破坏"每条流同时最多一个预取线程"的 dedup 不变量 |
| AUD-16503 | Major | risk | `src/features/edit/core/edit_gpu_decode_manager.py:2915（close_all 置 True 后永不复位）；1344（仅逐出路径检查` | medium | `_closing` 标志不阻止创建路径，close_all 之后迟到解码会重建并永久泄漏 VT/FFmpeg 会话 |
| AUD-16504 | Suggestion | doc-gap | `src/features/edit/core/edit_gpu_decode_manager.py:396-398（段注释）；421-423（docstring）；441-448（实际实现仅 telemetry）` | medium | `get_frame_with_policy` 的段注释与 docstring 仍描述已删除的 'placeholder' 拒绝行为，与实现矛盾 |
| AUD-16505 | Suggestion | risk | `src/features/edit/core/edit_gpu_decode_manager.py:352-353` | high | 播放预取线程 `except Exception: pass` 完全静默，违反"不得宽泛 except 掩盖领域错误"约束 |
| AUD-16506 | Suggestion | risk | `src/features/edit/core/edit_gpu_decode_manager.py:663-667（锁内登记）；751-756（锁外 start，无 try）` | medium | `preposition_interactive_frame` 在 `t.start()` 之前登记 busy 预约与 done_event，start 失败则两者永久泄漏 |
| AUD-16507 | Suggestion | process | `src/features/edit/core/edit_gpu_decode_manager.py:2994-2995（清 codec/topos）；2802-2815（` | medium | 探测缓存清理不一致：`invalidate_all_sources` 清 codec/topos 却保留 pix_fmt/resolution/route 缓存；`close_source` 完全不清 per-asset 探测缓存 |
| AUD-16508 | Suggestion | process | `src/features/edit/core/edit_gpu_decode_manager.py:1555；151、207、210、924、2337` | high | 私有符号跨模块导入 `_infer_bit_depth`；多处容器类型注解未参数化 |
| AUD-16509 | Suggestion | process | `src/features/edit/core/edit_gpu_decode_manager.py:880-898` | medium | `warmup_source` 中对同一条件的连续重复 if 块冗余 |
| AUD-16600 | Critical | bug | `src/features/edit/core/edit_gpu_preview_renderer.py:3158（生产方证据 timeline_preview_renderer.py:2055）` | medium | adjustment_layers 生产方为 3 元组，scoped 合成按 2 元解包 → 含调整剪辑的 GPU 帧必然 ValueError |
| AUD-16601 | Major | risk | `src/features/edit/core/edit_gpu_preview_renderer.py:3762-3779（acquire）；2013-2014、1994-1996、2318-2324、2326-2335、2342-2347（提前返回）；1334-1338（run() 异常路径）` | medium | present slot lease 在多条提前返回路径泄漏，WRITING 槽位永久不可回收 |
| AUD-16602 | Major | bug | `src/features/edit/core/edit_gpu_preview_renderer.py:3727-3743` | medium | 输出池尺寸变更时立即释放全部纹理并 `release_all()`，在途未 ack 的 GpuPresentFrame 指向已删除纹理 |
| AUD-16603 | Major | bug | `src/features/edit/core/edit_gpu_preview_renderer.py:4515-4517（调用）；3972-3976（分配循环）` | medium | 调整剪辑 grade 分支用槽位索引 1000+，`_ensure_graded_track_fbo` 会顺序创建 1001 张全尺寸纹理 |
| AUD-16604 | Major | bug | `src/features/edit/core/edit_gpu_preview_renderer.py:3571-3583（收缩 textures/ids）；3154（zip 消费 indices）` | medium | 转场预混合后 `track_texture_indices` 未同步收缩，scoped 合成 z 序错位 |
| AUD-16605 | Major | bug | `src/features/edit/core/edit_gpu_preview_renderer.py:5129-5297（清理清单）；3891-3917（缓存分配，单条目全分辨率纹理）` | medium | `_cleanup` 未释放复合纹理缓存（≤256MB）、bg/snapshot FBO 与多个 program/VAO；成功路径从不销毁 shared context/surface |
| AUD-16606 | Suggestion | process | `src/features/edit/core/edit_gpu_preview_renderer.py:1401、1577（clamp）；3348-3351、3747、3910（f1 纹理）` | medium | grade/YUV→RGB shader 输出 clamp(0,1) 且全链 f1 中间纹理，HDR 预览被量化为 8bit，与 `_effect_hdr`(f2) 的意图相悖 |
| AUD-16607 | Suggestion | process | `src/features/edit/core/edit_gpu_preview_renderer.py:2523-2536` | medium | `_render_yuv_to_track_fbo` 失败路径静默返回，轨道按“成功”进入合成（空轨无 CPU 回退信号） |
| AUD-16608 | Suggestion | process | `src/features/edit/core/edit_gpu_preview_renderer.py:557-569` | medium | `_ExternalTextureProxy.use()` 每次绑定新建并初始化 `QOpenGLFunctions_4_1_Core`；初始化失败时静默不绑定 |
| AUD-16609 | Suggestion | process | `src/features/edit/core/edit_gpu_preview_renderer.py:3479-3487（注释亦承认“生产转场路径不写入它”）` | high | `_ensure_transition_resources` 总是分配生产路径不用的全尺寸 f4 mask 纹理 |
| AUD-16610 | Suggestion | process | `src/features/edit/core/edit_gpu_preview_renderer.py:2063/2166/2218/2280（取值）；3017-3020、3043-3049、3063-3073（使用）；对照 3280-3282` | medium | 图层 opacity 直接进 shader 未钳制（对比：调整剪辑路径有钳制） |
| AUD-16611 | Suggestion | process | `src/features/edit/core/edit_gpu_preview_renderer.py:295（` | medium | 类型注解缺失/不准确若干处；过期注释与数据形状不符 |
| AUD-16700 | Major | bug | `src/features/edit/core/edit_playback_controller.py:738-750（J 侧同构 754-772）` | medium | JKL 首次按 L/J 直接进入 2x 倍速（档位 off-by-one），1x 档永远无法通过 J/K/L 触达 |
| AUD-16701 | Major | bug | `src/features/edit/core/edit_playback_controller.py:1031、1065` | medium | `except (ValueError, Exception)` 宽泛捕获掩盖非领域异常（两处），违反"不得宽泛 except 掩盖领域错误"铁律 |
| AUD-16702 | Suggestion | process | `src/features/edit/core/edit_playback_controller.py:1084-1086` | high | `get_progress()` 在单帧时间线（duration==1）上除零 |
| AUD-16703 | Suggestion | process | `src/features/edit/core/edit_playback_controller.py:941-953` | high | `tick()` 帧号未变化时仍发射 `frame_changed` |
| AUD-16704 | Suggestion | process | `src/features/edit/core/edit_playback_controller.py:372-388` | high | `set_playback_range` 不校验 start/end 合法性 |
| AUD-16705 | Suggestion | process | `src/features/edit/core/edit_playback_controller.py:311-325（及 95-100）` | medium | `set_fps` 未拦截 NaN/Inf，进入有理数换算后抛未捕获异常 |
| AUD-16706 | Suggestion | process | `src/features/edit/core/edit_playback_controller.py:1226-1230、61` | high | 死变量与未使用导入（质量清理） |
| AUD-16710 | Critical | risk | `src/features/edit/core/edit_project_io.py:1124-1131（另见 305、329-338、381）` | medium | 加载外部 .topos 包后的反序列化无异常边界，损坏/手改包可经主"打开工程"路径使整个应用崩溃 |
| AUD-16711 | Major | bug | `src/features/edit/core/edit_project_io.py:1432-1451` | medium | `_cleanup_old_backups` 用文件名前缀匹配备份，可误删其它工程的备份文件 |
| AUD-16712 | Major | bug | `src/features/edit/core/edit_project_io.py:806-808（对照 1397-1420）` | high | 保存时忽略 `_create_backup` 失败结果，备份失败仍继续覆盖原文件 |
| AUD-16713 | Major | bug | `src/features/edit/core/edit_project_io.py:106-114` | medium | `_collect_color_management_settings` 宽泛 `except Exception` 静默返回空 dict 且无日志 |
| AUD-16714 | Major | bug | `src/features/edit/core/edit_project_io.py:271-279` | high | 加载侧色彩管理覆盖注入失败被 `except Exception: pass` 完全吞掉 |
| AUD-16715 | Suggestion | risk | `src/features/edit/core/edit_project_io.py:919-941（对照 960-988、1304-1329）` | high | 手动保存/另存为在 UI 线程同步执行完整 ZIP 写，大工程可致长卡顿 |
| AUD-16716 | Suggestion | risk | `src/features/edit/core/edit_project_io.py:783、793（经 92-99 的 worker 调用）` | high | 后台保存线程写回 `self._last_generator_preflight` 共享诊断状态 |
| AUD-16717 | Suggestion | process | `src/features/edit/core/edit_project_io.py:801-804` | medium | 扩展名纠正使用 `with_suffix`（替换而非追加），带点号的自定义文件名被截断 |
| AUD-16718 | Suggestion | perf-opportunity | `src/features/edit/core/edit_project_io.py:1587-1589` | medium | `cleanup()` 的 `waitForDone()` 无超时，慢速/挂起 IO 可无限阻塞退出 |
| AUD-16719 | Suggestion | process | `src/features/edit/core/edit_project_io.py:587-597、654-663` | high | 回调类型注解使用内建 `callable`（非类型），9 处 |
| AUD-16800 | Major | bug | `src/features/edit/core/effect_instance.py:106` | medium | `AnimatableParam.add_keyframe` 原地修改共享 `KeyframeTrack`，破坏 copy-on-write 与 undo/历史一致性 |
| AUD-16801 | Suggestion | risk | `src/features/edit/core/effect_instance.py:35` | high | `AnimatableParam` 未加 `frozen=True`，违反模块自身"不可变保证线程安全"的设计原则 |
| AUD-16802 | Suggestion | process | `src/features/edit/core/effect_instance.py:635` | medium | `EffectInstance.__eq__/__hash__` 仅按 `effect_id` 判等，同 id 不同参数被判为相等 |
| AUD-16803 | Suggestion | process | `src/features/edit/core/effect_instance.py:611` | medium | `from_dict` 对工程文件（外部数据）的迁移转换无容错，坏值直接抛异常中断加载 |
| AUD-16804 | Major | risk | `src/features/edit/core/effect_registry.py:577` | medium | catalog 加载失败回退分支引用已被删除的 `GAUSSIAN_BLUR_EFFECT`/`SHARPEN_EFFECT`，触发即 NameError 崩溃 |
| AUD-16805 | Suggestion | risk | `src/features/edit/core/effect_registry.py:564` | medium | 宽泛 except 使单个畸形 spec 中断整个 catalog 注册循环（并叠加 AUD-16804 变成崩溃） |
| AUD-16806 | Suggestion | doc-gap | `src/features/edit/core/effect_registry.py:519` | high | 类与方法的 docstring 与实际目录结构漂移（文档腐化） |
| AUD-16807 | Suggestion | risk | `src/features/edit/core/effect_registry.py:776` | high | 模块底部延迟 `import EffectInstance`、单例与惰性全局缓存无锁 |
| AUD-16808 | Major | bug | `src/features/edit/core/effect_render_operations.py:97` | medium | FX-C02 修复计划（render-operation plan）无任何生产消费者，GPU 预览渲染器仍走 legacy 全屏后置合成路径 |
| AUD-16809 | Suggestion | process | `src/features/edit/core/effect_render_operations.py:147` | high | 循环体内 import + 弱类型字段 `tuple[Any, ...]` |
| AUD-16810 | Suggestion | process | `src/features/edit/core/export_manifest.py:429` | medium | `write_manifest_file` 失败时遗留 `.tmp` 文件、无 fsync、固定 tmp 名 |
| AUD-16811 | Suggestion | process | `src/features/edit/core/export_manifest.py:412` | medium | 输出文件被完整读取两遍（SHA-256 + 全量解码 probe），4K 长导出的后台 IO/CPU 成倍 |
| AUD-16812 | Major | bug | `src/features/edit/core/generator_clip_transaction.py:153` | medium | `_remove_asset_and_bin_entry` 逐字段重建 `EditProject` 漏传 `revision`，生成器资产 undo/GC 后工程 revision 归零 |
| AUD-16813 | Suggestion | process | `src/features/edit/core/generator_clip_transaction.py:116` | medium | `undo_generator_clip_transaction(referenced_asset_ids=None)` 无条件删除资产，安全默认依赖调用方自觉 |
| AUD-16814 | Suggestion | process | `src/features/edit/core/generator_clip_transaction.py:129` | high | 手工逐字段重建 + `"adjustment"/"solid"` 字面量与 guard 模块重复 |
| AUD-16815 | Suggestion | process | `src/features/edit/core/generator_transaction_guard.py:34` | high | 未使用的导入：`field`、`Dict`、`Optional`、`Tuple` |
| AUD-16816 | Suggestion | process | `src/features/edit/core/generator_transaction_guard.py:271` | medium | `reclaim_orphan_generators` 在扫描与移除间存在 TOCTOU（以空集跳过引用校验） |
| AUD-16817 | Suggestion | process | `src/features/edit/core/generator_transaction_guard.py:92` | high | `GeneratorTransactionGuard` 只回滚 project 引用，不回滚 timeline 与已提交到服务层的状态，保护是约定式的 |
| AUD-16900 | Major | bug | `src/features/edit/core/gpu_transition_shaders.py:183` | medium | iris 圆/椭圆半径上限 1.55 在宽幅画幅下永远盖不住左右边缘中点与四角，终点处全帧跳变 |
| AUD-16901 | Suggestion | process | `src/features/edit/core/gpu_transition_shaders.py:208` | high | u_reverse 仅在 wipe 分支生效，circle/ellipse/polygon 静默忽略 |
| AUD-16902 | Suggestion | process | `src/features/edit/core/gpu_transition_shaders.py:58` | high | blur9 的 texel 参数从未被使用 |
| AUD-16903 | Major | risk | `src/features/edit/core/interchange.py:325` | medium | build_cue_sheet_from_sequence 对 Rational 帧率执行 `fps > 0` 比较抛 TypeError，首个音频 clip 即崩溃 |
| AUD-16904 | Suggestion | process | `src/features/edit/core/loudness_analyzer.py:112` | high | analyze_loudness 未验证 samples 维度，1D 输入静默产出全 floor 报告 |
| AUD-16905 | Major | risk | `src/features/edit/core/loudness_meter.py:281` | medium | _update_integrated 每个 hop 全量重扫 _gated_blocks，实时音频路径上成本随会话时长无界线性增长 |
| AUD-16906 | Major | bug | `src/features/edit/core/mixer_preset.py:171` | medium | load_presets 宽泛 except 返回空列表 + add/remove 无条件整文件覆写，单条损坏可永久清空整个用户预设库 |
| AUD-16907 | Suggestion | process | `src/features/edit/core/mixer_preset.py:94` | high | strip_eq/effect_chain/sends 反序列化静默吞掉所有异常且无日志，预设静默缺胳膊少腿 |
| AUD-16908 | Suggestion | process | `src/features/edit/core/multicam.py:198` | high | build_multicam_group_from_assets 不拒绝重复 asset_id，asset_id 键控的查表产生歧义 |
| AUD-16909 | Suggestion | process | `src/features/edit/core/multicam_sync.py:156` | high | compute_sync_alignments 用全体信号的最小长度限制所有配对的相关窗口，一个短角度拖累全部精度 |
| AUD-16910 | Suggestion | process | `src/features/edit/core/multicam_sync.py:269` | medium | worker 中断路径不发射任何信号，若被中断则 UI 永久停留"同步中"且按钮禁用 |
| AUD-16911 | Major | bug | `src/features/edit/core/nested_sequence.py:444` | medium | build_nested_from_selection 在宿主轨道 span 内存在未选片段时因 with_clip_added 重叠校验抛 ValueError，合法多轨/非连续选择失败且报错为内部 UUID |
| AUD-16912 | Suggestion | process | `src/features/edit/core/nested_sequence.py:181` | medium | evaluate_nested_frame 的环检测依赖调用方穿透私有 _render_stack，与注释/签名矛盾；生产路径未使用该函数 |
| AUD-16913 | Suggestion | process | `src/features/edit/core/nested_sequence.py:437` | medium | 选择边界的转场（单端选中）在父时间线被静默删除，效果无提示丢失 |
| AUD-16914 | Suggestion | process | `src/features/edit/core/nested_sequence.py:452` | high | 直接调用 TimelineSequence 私有方法 _rebuild |
| AUD-1700 | Suggestion | process | `src/shared/render/source.py:6-11` | medium | 死代码：RenderSource 协议在全仓库无任何消费者 |
| AUD-17000 | Major | bug | `src/features/edit/core/placeholder_frame.py:91` | medium | 占位素材元数据宽高未经验证直接 `int()`，畸形外部数据可在渲染路径抛 ValueError/TypeError |
| AUD-17001 | Major | bug | `src/features/edit/core/placeholder_frame.py:74-79` | medium | 确定性静态帧无缓存，预览/导出逐帧重新生成，且 int64 meshgrid + 冗余 `.copy()` 造成每次调用约百 MB 级临时分配 |
| AUD-17002 | Suggestion | process | `src/features/edit/core/placeholder_frame.py:27` | high | 类型注解风格不一致：`tuple`/`Tuple` 混用、返回类型用字符串字面量 |
| AUD-17003 | Major | bug | `src/features/edit/core/playback_debug.py:20` | high | 模块导入期用 `float()` 解析外部环境变量且无容错，坏值导致 import 失败并拖垮全部依赖模块（trace 关闭时也执行） |
| AUD-17004 | Suggestion | risk | `src/features/edit/core/playback_debug.py:149` | medium | `print` 在 `_LOCK` 之外执行，多线程并发 trace 可产生交错撕裂的输出行，且 seq 与实际打印顺序可能颠倒 |
| AUD-1701 | Suggestion | process | `src/shared/render/source.py:25` | high | `get_current_output() -> Any` 返回类型过弱，削弱 shared 层协议的类型价值 |
| AUD-1702 | Suggestion | risk | `src/shared/render/source.py:25-31` | medium | 返回 GPU 资源时缺少所有权/线程亲和性契约 |
| AUD-1703 | Suggestion | process | `src/shared/render/source.py:17-31` | high | `is_available() == False` 时 `get_current_output()` 的契约未定义 |
| AUD-17100 | Critical | bug | `src/features/edit/core/project.py:4183-4243` | medium | update_comp_clip 原地修改与旧工程快照共享的合成图节点 dict，破坏不可变快照隔离（undo 历史被污染） |
| AUD-17101 | Major | bug | `src/features/edit/core/project.py:2512-2552` | medium | remove_timeline 不清理各 bin 的 timeline_ids 引用，删除时间线后留下悬挂引用 |
| AUD-17102 | Major | bug | `src/features/edit/core/project.py:2253-2297` | high | remove_bin 不处理子 bin 的 parent_id，删除后子 bin 的父引用指向不存在的 bin |
| AUD-17103 | Major | bug | `src/features/edit/core/project.py:1443-1469` | medium | import_subclip 回退分支创建的 Root bin 被后续 `new_bins = dict(self.bins)` 覆盖丢弃，root_bin_id 指向不存在的 bin |
| AUD-17104 | Major | bug | `src/features/edit/core/project.py:5014-5020` | medium | check_media_availability 只豁免 comp_clip，其余虚拟素材被 os.path.exists 判为不可用，与 is_virtual 语义不一致 |
| AUD-17105 | Major | bug | `src/features/edit/core/project.py:4330-4453` | medium | reset_clip_composite 返回类型不一致：注解为 EditProject，实际返回 (EditProject, TimelineSequence) 元组，且与早退 `return self` 混用 |
| AUD-17106 | Major | bug | `src/features/edit/core/project.py:2118-2136` | medium | duplicate_asset 丢弃素材日志字段（keywords/rating/flag/color_label/comment）与 is_offline，用户整理的元数据在复制后静默丢失 |
| AUD-17107 | Major | risk | `src/features/edit/core/project.py:4708-4766` | medium | _probe_audio_info 的 container.close() 无 finally 保护，探测中途异常时 PyAV 容器（文件句柄）泄漏 |
| AUD-17108 | Major | bug | `src/features/edit/core/project.py:2054-2077` | medium | duplicate_asset 在纯数据模型内调用全局 scene store register()，产生撤销不可回收的副作用并造成跨 feature 反向依赖 |
| AUD-17109 | Suggestion | process | `src/features/edit/core/project.py:3172-3182` | medium | invalidate_render_cache_for_asset 未按 composite 关联过滤，对每个匹配 comp clip 将全部 manifest 失效（过度失效），且直接引用该 asset 的普通 clip 反而不失效 |
| AUD-17110 | Suggestion | process | `src/features/edit/core/project.py:336-341` | high | MediaAsset.solid_color 回退色不一致：color 缺失回退黑色，color 格式损坏回退青色 |
| AUD-17111 | Suggestion | process | `src/features/edit/core/project.py:4561-4568` | medium | dirty 以 modified_at > created_at 近似，保存后/撤销回已保存状态时仍恒为 True |
| AUD-17112 | Suggestion | process | `src/features/edit/core/project.py:711-717（另见 4614-4617）` | medium | SmartBin offline 过滤与 offline_assets 属性对每个素材做同步 os.path.exists 磁盘 IO，UI 反复查询大工程时卡顿 |
| AUD-17113 | Suggestion | process | `src/features/edit/core/project.py:3090-3105` | medium | invalidate_render_cache_for_color_stack 三层嵌套（graphs×nodes×manifests）且逐 clip 再全量扫描、重复 from_dict 反序列化，调色热路径复杂度偏高 |
| AUD-17114 | Suggestion | process | `src/features/edit/core/project.py:3760-3764` | high | create_comp_clip_from_timeline_clip 的 timeline 参数缺类型注解 |
| AUD-17115 | Suggestion | process | `src/features/edit/core/project.py:2974-2976、3017` | medium | set_render_cache_manifest 对无 cache_id 的 legacy dict 以空字符串作键，多个 legacy 条目互相覆盖 |
| AUD-17116 | Suggestion | process | `src/features/edit/core/project.py:1365-1409` | medium | remove_asset 不清理关联 color_stacks / compositions / render_cache_manifests，孤儿数据随工程持久化 |
| AUD-17117 | Suggestion | process | `src/features/edit/core/project.py:4266-4300` | medium | remove_comp_clip 未失效引用该图的渲染缓存，删除后 VALID 指纹缓存仍可被查询返回 |
| AUD-17201 | Major | bug | `src/features/edit/core/project_archive.py:204-221` | medium | 目录类素材（image_sequence）SHA-256 校验被静默跳过，状态仍记为 copied |
| AUD-17202 | Major | bug | `src/features/edit/core/project_archive.py:219-232（对照 try 块 203-217；_sha256_of 定义 43-51 无异常处理）` | medium | 哈希/大小计算在 per-file try 之外，单个文件 OSError 即中断整个收集并留下无报告的半成品归档 |
| AUD-17203 | Major | risk | `src/features/edit/core/project_archive.py:140-249（签名仅 target_dir/sequences/include_unused/media_dir，无 progress/cancel 参数）；调用方 src/features/edit/ui/media_pool_panel.py:5539（菜单槽内同步调用）` | medium | collect_project 为长阻塞同步 I/O 且无进度/取消钩子，UI 调用方在主线程直接执行 |
| AUD-17204 | Suggestion | process | `src/features/edit/core/project_archive.py:205` | medium | 重复收集到同一归档目录时 copytree(dirs_exist_ok=True) 只合并不清理，旧残留媒体永久留在归档内 |
| AUD-17205 | Suggestion | process | `src/features/edit/core/project_archive.py:144` | high | sequences 参数类型为 Iterable[Any]，丢失类型约束 |
| AUD-17211 | Suggestion | process | `src/features/edit/core/project_migration.py:88-106（_walk 原地改写嵌套 dict）、211-215（仅 shallow copy）` | medium | migrate_project_payload 就地修改传入载荷，浅拷贝仅保护顶层版本键，与"返回新载荷"语义不符 |
| AUD-17221 | Suggestion | process | `src/features/edit/core/project_serializer.py:171-178（save 走 ToposProjectIO ZIP 包）vs 204-205（load 用 open+json.load）` | medium | save 写 ZIP 工程包而 load 仍按裸 JSON 解析，类内读写不对称（load 永远打不开本类保存的文件） |
| AUD-17222 | Suggestion | process | `src/features/edit/core/project_serializer.py:184-188、285-289` | high | save/load 捕获宽泛 Exception 且用 traceback.print_exc() 输出，错误细节不进日志体系 |
| AUD-17223 | Suggestion | process | `src/features/edit/core/project_serializer.py:25（import uuid）、27（from dataclasses import asdict）、338-365（_to_relative_path 全文无调用）、392-425（参数未使用）` | high | 死代码与未使用导入：_to_relative_path 无调用、uuid/asdict 未使用、create_default_project 的 project_dir/sample_rate 参数被忽略 |
| AUD-17231 | Major | bug | `src/features/edit/core/proxy_generator.py:324-326（取消 return None 在 try 内，仅 finally 关容器）；对照 375-378（只有 except 分支才 _remove_quietly）` | medium | PyAV 取消路径残留截断的代理文件，之后会被识别为 ready 代理进入播放 |
| AUD-17232 | Major | bug | `src/features/edit/core/proxy_generator.py:445-449` | high | CLI 兜底超时/失败不清理部分输出文件，且 kill 后不回收子进程 |
| AUD-17233 | Major | bug | `src/features/edit/core/proxy_generator.py:345-348` | high | 进度百分比按 frame.time*100 计算，未除以总时长，任何 >1 秒的视频进度立即到 95% |
| AUD-17234 | Suggestion | process | `src/features/edit/core/proxy_generator.py:131、141-142` | high | ProxyGeneratorWorker._cancelled 无任何公开设置途径，worker 路径的取消能力不可达 |
| AUD-17235 | Major | bug | `src/features/edit/core/proxy_generator.py:750-755` | high | delete_all_proxies_in_directory 以子串匹配删除文件，可误删文件名含 "proxy" 的用户原片 |
| AUD-17236 | Suggestion | process | `src/features/edit/core/proxy_generator.py:310（对照 79 行` | high | PyAV 路径音频码率硬编码 128000，忽略 config.audio_bitrate |
| AUD-17237 | Suggestion | process | `src/features/edit/core/proxy_generator.py:286-291、340-342` | medium | 代理统一转 8bit yuv420p 且不处理色彩范围/原色域，HDR 源的代理预览会偏色 |
| AUD-17238 | Suggestion | process | `src/features/edit/core/proxy_generator.py:184、198-206（清理）vs 483-484（图片路径内部改扩展名）` | medium | generate_proxy 的异常清理删除的是扩展名改写前的路径，图片代理实际输出 (.jpg) 不会被清理 |
| AUD-17239 | Suggestion | process | `src/features/edit/core/proxy_generator.py:161、217、385、467 等` | high | progress_callback 类型标注为 Optional[Any]，丧失可调用契约 |
| AUD-17251 | Major | bug | `src/features/edit/core/proxy_policy.py:144-150` | medium | mode="all" 分支先于尺寸未知检查，违反模块自述的"未知尺寸不盲生成"不变量 |
| AUD-17252 | Suggestion | process | `src/features/edit/core/proxy_policy.py:166-171` | medium | export_source_path 在 master 路径为空时回退返回 effective_path（可能是代理），与"导出强制原片"语义冲突 |
| AUD-17253 | Suggestion | process | `src/features/edit/core/proxy_policy.py:163、178` | high | export_source_path/_policy_of 参数类型为 Any，绕过静态检查 |
| AUD-17300 | Major | risk | `src/features/edit/core/realtime_playback/engine.py:364-373（持锁）vs 640-646、652-673、701-707、814-833、1022-1045（无锁）` | medium | 引擎状态字段的加锁策略不一致，多线程入口下存在数据竞争 |
| AUD-17301 | Suggestion | risk | `src/features/edit/core/realtime_playback/engine.py:366-373 + 640-646` | medium | set_mode 在持有 self._lock 期间发射 state_changed 信号，锁内执行直连槽代码 |
| AUD-17302 | Suggestion | process | `src/features/edit/core/realtime_playback/engine.py:915-927` | medium | _execute_delegate 旧签名 fallback 以 `except TypeError` 判定签名不兼容，会把 delegate 内部业务的 TypeError 误判并丢弃 decisions 重试 |
| AUD-17303 | Suggestion | process | `src/features/edit/core/realtime_playback/engine.py:876-884（对照 request_present 的 807-808）` | medium | request_scrub_present 记录 telemetry 但不发射 telemetry_recorded 信号 |
| AUD-17304 | Suggestion | process | `src/features/edit/core/realtime_playback/engine.py:716-723 + 1007` | medium | back-pressure 分支的 _handle_miss 会消费并顶替 worker 上报的 miss 归因槽 |
| AUD-17305 | Suggestion | risk | `src/features/edit/core/realtime_playback/engine.py:1104-1112` | high | get_default_engine() 单例 check-then-act 无锁 |
| AUD-17306 | Suggestion | process | `src/features/edit/core/realtime_playback/engine.py:428-433` | high | configure_adaptive_quality 直接改写 AdaptiveQualityController 私有字段 |
| AUD-17307 | Suggestion | process | `src/features/edit/core/realtime_playback/engine.py:716 与 785-787` | medium | 预算记账不对称：before_render 仅 REALTIME 调用，after_render 全模式调用 |
| AUD-17310 | Major | bug | `src/features/edit/core/realtime_playback/clip_decode_stream.py:464-475` | medium | P010/P016 位深回退正则不命中，10/16-bit HDR 帧被标记为 8-bit |
| AUD-17311 | Suggestion | process | `src/features/edit/core/realtime_playback/clip_decode_stream.py:292-298` | medium | _decode 的 read_next_frame 回退路径 `except Exception: pass` 完全静默 |
| AUD-17312 | Suggestion | process | `src/features/edit/core/realtime_playback/clip_decode_stream.py:434-437` | medium | _build_handle 把原始 timebase 单位的 pts 当作秒写入 pts_seconds |
| AUD-17315 | Major | bug | `src/features/edit/core/realtime_playback/decode_manager_source.py:41-52` | medium | read_frame 宽泛 except 吞掉解码管理器全部异常且零日志，真实错误伪装成 miss |
| AUD-17320 | Suggestion | process | `src/features/edit/core/realtime_playback/audio_ring_buffer.py:132-137` | high | write() 通道数不匹配时静默截断多余通道，无日志无统计 |
| AUD-17325 | Suggestion | process | `src/features/edit/core/realtime_playback/avsync_controller.py:8-11（注释）与 74-76、130-152（实现）` | medium | 模块头注释的阈值描述与实现常量不一致（80ms vs 150ms） |
| AUD-17400 | Major | doc-gap | `src/features/edit/core/realtime_playback/gpu_frame_pool.py:148-152（对照 41-46、206-220、226-241）` | medium | 池缓存命中返回共享 handle，而模块文档教调用方 `gpu_frame.release()`，会释放仍驻留池中的纹理（use-after-release / 双重释放） |
| AUD-17401 | Major | risk | `src/features/edit/core/realtime_playback/gpu_frame_pool.py:86-98、100-117（release=None）；206-220、226-241、250-261（释放路径均要求` | medium | 默认/external 上传路径产出的 handle `release=None`，容量淘汰、release_generation、shutdown 全部跳过释放 → GPU 纹理泄漏 |
| AUD-17402 | Suggestion | process | `src/features/edit/core/realtime_playback/gpu_frame_pool.py:59、275` | high | `_gl_calls_from_decode_thread` 统计从未被递增，恒为 0，规格书 §25 断言指标失效 |
| AUD-17403 | Suggestion | process | `src/features/edit/core/realtime_playback/gpu_frame_pool.py:118-121、156-160` | high | `_default_upload` 宽泛 except 吞掉上传失败（ImportError 静默 pass、Exception 仅 debug 日志），失败不可归因 |
| AUD-17404 | Suggestion | risk | `src/features/edit/core/realtime_playback/gpu_frame_pool.py:226-241、250-261` | medium | release_generation/shutdown 可在非 GL 线程调用 `handle.release()`（GL 删除操作），与"UI 线程不得操作 OpenGL"/deferred-release 约束冲突 |
| AUD-17405 | Suggestion | perf-opportunity | `src/features/edit/core/playback_budget 路径 src/features/edit/core/realtime_playback/playback_budget.py:145-151（登记）、153-190（唯一清理点）、192-199（reset_pressure）` | medium | before_render 成功后若渲染路径异常中断，in-flight 永久占位且无超时/abort API → 后续所有帧被 drop、播放永久冻结 |
| AUD-17406 | Suggestion | process | `src/features/edit/core/realtime_playback/playback_budget.py:205-207` | high | `should_defer_render` 是"查询"语义但内部委托 `before_render`，产生登记 in-flight / 计 drop 的副作用 |
| AUD-17407 | Suggestion | risk | `src/features/edit/core/realtime_playback/playback_budget.py:153-169、104-112` | medium | after_render 不校验 frame 一致性且信任调用方 start_ns；`budget_ms`/`is_back_pressured` 之外的 `set_target_fps` 双写与无锁读存在轻微竞态 |
| AUD-17408 | Suggestion | risk | `src/features/edit/core/realtime_playback/policy.py:333-341` | high | `get_default_policy` 惰性单例无锁，注释声称"线程安全由 GIL 保证"不准确 |
| AUD-17409 | Major | bug | `src/features/edit/core/realtime_playback/render_cache_adapter.py:346-359（根因）；policy.py:172-182 与 source_resolver.py:259-275（误用链）` | medium | `has_render_cache_ready`/`render_cache_path` 是"任意 clip 就绪"的全局语义，而 policy 逐层查询，多 clip 时间线下会把 clip A 的缓存路径判给 clip B → 错误解码 |
| AUD-17410 | Major | bug | `src/features/edit/core/realtime_playback/render_cache_adapter.py:310-320、361-372` | medium | `register_manifest` 硬编码 NTSC 帧率与 1080p 默认值，且 `get_cache_entry` 失效校验不比对 fps/cache_version/source_path 等关键字段 |
| AUD-17411 | Suggestion | process | `src/features/edit/core/realtime_playback/render_cache_adapter.py:294-297、330-340、392-398、400-405` | medium | `_key_index`/`_last_keys` 只写不读（死状态），invalidate_by_clip 不清理索引 |
| AUD-17412 | Suggestion | process | `src/features/edit/core/realtime_playback/render_cache_adapter.py:86-89` | medium | `speed_params_signature` 对既无 `.speed` 又不可 `float()` 的输入抛 TypeError 未捕获 |
| AUD-17413 | Suggestion | risk | `src/features/edit/core/realtime_playback/source_resolver.py:221、299-315` | medium | `_ensured_proxies` 集合无锁 check-then-add，跨线程调用可重复触发后台代理生成 |
| AUD-17414 | Suggestion | risk | `src/features/edit/core/realtime_playback/source_resolver.py:155-165` | medium | `DiskProxyServiceAdapter._resolve_service` 失败后把 `self._service` 固化为 None，服务恢复后不再重试；赋值无锁 |
| AUD-17415 | Suggestion | process | `src/features/edit/core/realtime_playback/source_resolver.py:227-230` | high | `bind_engine` 从外部写 engine 私有属性 `_source_resolver`（带 type: ignore），破坏封装且无类型契约 |
| AUD-17416 | Suggestion | process | `src/features/edit/core/realtime_playback/task_priority.py:80-92` | high | `EvictionVerdict.action` 用裸 str 而非 Literal/枚举，调用方拼写错误无法静态发现 |
| AUD-17417 | Suggestion | process | `src/features/edit/core/realtime_playback/telemetry.py:207-222（空分支，211 行）与 248-274（非空分支，251 行）` | medium | summary() 空窗口分支与非空分支的键名不一致：`averaged_total_ms` vs `total_ms_mean` |
| AUD-17418 | Suggestion | risk | `src/features/edit/core/realtime_playback/telemetry.py:200-235（233-234 行读 ts）、20 行（import field）` | medium | summary() 在释放锁后读取 `_first_record_ts`/`_last_record_ts`（与 reset()/record() 竞态）；`field` 导入未使用 |
| AUD-17419 | Major | perf-opportunity | `src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:168-207（工厂调用在 196-201 行、close 在 182-184/193 行，均在` | medium | configure() 持有 `self._lock` 期间调用用户回调 `media_source_factory()` 与 `stream.close()`，可长时间阻塞所有调度热路径 |
| AUD-17420 | Suggestion | risk | `src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:510-511、560-564（无锁 +=）；666-688（stats 在 _lock 下读 _prefetch_*）；690-696（reset_stats 不清 prefetch 计数）` | medium | `request_bundle` 的 `_bundle_hits/_bundle_misses` 无锁 `+=`；stats()/reset_stats() 与 prefetch 计数器的锁不对称 |
| AUD-17421 | Suggestion | process | `src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:170-176、209-214` | medium | configure 用 `is` 身份比较 callable（bound method/闭包），同配置重复 configure 可能误判 changed 而取消活跃预取 |
| AUD-17422 | Suggestion | process | `src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:276-294` | medium | prepare_window 中 source_frame_provider 异常时静默按 1:1 回退，变速/倒放 clip 会预取错误的源帧 |
| AUD-17423 | Suggestion | perf-opportunity | `src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:652-657` | medium | shutdown() 同步等待 prefetch executor，活跃预取内的同步解码无 deadline，可能长时间阻塞关闭流程 |
| AUD-17500 | Major | risk | `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:159` | medium | attach_backend 部分连接失败不回滚已建立的信号连接，detach_backend 依据 _connected 跳过断连导致连接与对象泄漏 |
| AUD-17501 | Major | bug | `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:324` | medium | _build_backend_layer 兜底分支为 backend 必然拒绝的 payload 类型构造任务，submit 返回 True 但帧永不产出且 miss 归因误导 |
| AUD-17502 | Suggestion | process | `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:237` | high | gpu_composite_frames 统计语义错位：计数的是提交被接受而非合成成功 |
| AUD-17503 | Suggestion | process | `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:334` | medium | reset_generation 的 _generation 为只写死状态，backend 重置失败被无日志吞掉 |
| AUD-17504 | Suggestion | process | `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:29` | high | 未使用导入：QOpenGLContext、PlaybackMode、SourceKind |
| AUD-17505 | Suggestion | risk | `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:233` | high | 统计计数器跨线程读写无锁：worker 线程自增、GUI/telemetry 线程读取与清零 |
| AUD-17506 | Suggestion | process | `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:283` | medium | submit() 输入验证不足：opacity 未 clamp/NaN 检查，输出尺寸未验证正值 |
| AUD-17507 | Suggestion | process | `src/features/edit/core/realtime_playback/types.py:284` | medium | classify_miss_reason 子串匹配顺序敏感且 "effect" 单词级宽匹配，存在持续性失败被误归为瞬态/反之的误分类风险 |
| AUD-17508 | Suggestion | process | `src/features/edit/core/realtime_playback/types.py:123` | medium | FrameHandle 默认 dataclass eq/hash 语义对 ndarray payload 是潜在陷阱 |
| AUD-17600 | Critical | risk | `src/features/edit/core/recording_manager.py:1079-1090（触发点 1089）、src/features/edit/core/recording_manager.py:1211（死锁点）` | medium | 磁盘耗尽安全终止路径在持有 `_buffer_lock` 时调用 `_finalize_take`，非重入锁同线程二次加锁 → 死锁挂死应用 |
| AUD-17601 | Major | risk | `src/features/edit/core/recording_manager.py:1162、1164-1169、1220-1225` | medium | take 落盘（整段 WAV 流式写盘）在 UI/主线程同步执行，长 take 卡死界面 |
| AUD-17602 | Major | bug | `src/features/edit/core/recording_manager.py:123-141、119` | medium | `_spool_oldest` 先 pop 后磁盘预检，预检抛出时该块既未落盘也未回内存 → 保留的 partial take 缺一段音频且时长虚高 |
| AUD-17603 | Suggestion | process | `src/features/edit/core/recording_manager.py:1055-1056、1099-1100、674` | medium | `RecordingConfig` 无输入验证：`channels=0` 触发 ZeroDivisionError 被宽泛 except 吞成 warning，录音静默无声 |
| AUD-17604 | Suggestion | process | `src/features/edit/core/recording_manager.py:1099-1100` | medium | `_on_audio_ready` 最外层宽泛 `except Exception` 掩盖音频处理链中的领域/程序错误 |
| AUD-17605 | Suggestion | process | `src/features/edit/core/recording_manager.py:948-951、1034-1036` | medium | 设备启动失败置 `STATE_FAILED` 后 `start_recording` 仍返回 True，返回值语义失真 |
| AUD-17606 | Suggestion | doc-gap | `src/features/edit/core/recording_manager.py:270、286、1228` | high | `TakeInfo.status` 文档枚举与实际取值不一致（缺 "pending"/"completed_partial"） |
| AUD-17607 | Suggestion | process | `src/features/edit/core/recording_manager.py:1102-1118、1202-1208` | medium | `_finalize_take` 内 `stop()` 可能同步再入 `stateChanged` → 二次 `_finalize_take`，存在 take 重复归档/重复回调风险 |
| AUD-17610 | Suggestion | process | `src/features/edit/core/render_cache.py:49-64` | medium | `RenderCacheSettings.from_dict` 对持久化外部数据零验证，非法类型/范围直接进入后续算术与缓存管理 |
| AUD-17611 | Suggestion | process | `src/features/edit/core/render_cache.py:309` | medium | `RenderCacheManifest.create_new` 生成 CWD 相对路径 `Cache/...`，状态检查与 worker 均按进程 CWD 解析 |
| AUD-17612 | Suggestion | process | `src/features/edit/core/render_cache_encoder.py:169-191、123-124、148-149` | medium | `close()` 异常路径不清理临时文件且 `_is_open`/编码器状态不复位；未 open 时 `write_frame*` 静默丢帧 |
| AUD-17613 | Suggestion | process | `src/features/edit/core/render_cache_encoder.py:219-220` | high | `_parse_codec` 返回注解为裸 `tuple`，泛型参数缺失 |
| AUD-17614 | Major | risk | `src/features/edit/core/render_cache_status.py:301-307（全 clip 遍历）、157（每 clip 指纹）、130-133（manifest 全量反向扫描）；触发频率证据 src/features/edit/ui/timeline_view.py:611-613、1544-1548` | medium | 缓存状态条每 500ms 在 UI 线程全量重建：每 clip 全量指纹（JSON 序列化 + sha256 + os.stat）× 每 clip 线性扫描全部 manifest（含逐个 from_dict）→ 大时间线周期性主线程开销 |
| AUD-17700 | Major | risk | `src/features/edit/core/render_cache_worker.py:388-417（对照 541-544）` | medium | run() 在 try 块之前的初始化代码抛异常时不发 failed 信号，该 clip 的缓存渲染在本次会话内被永久锁死 |
| AUD-17701 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:447-460、789-848、427-430` | medium | motion_graphic 第二分支与 `_render_motion_graphic_frames` 为不可达死代码 |
| AUD-17702 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:865-873` | medium | `_parse_fps` 未处理 None/TypeError，小数 fps 有损取整 |
| AUD-17703 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:683-687（对照 636-639 与死代码注释 812-819）` | medium | HTML 实时录制将浏览器视口设为 manifest 尺寸而非场景 authored 尺寸 |
| AUD-17704 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:713-728` | medium | 实时录制结果的帧数未与 manifest 时长校验 |
| AUD-17705 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:512-516` | medium | dtype 分派 else 分支把非 uint8/float32/float64 帧按 uint8 直送编码器 |
| AUD-17706 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:327-337、545-550` | medium | 资源清理路径 `except Exception: pass` 完全无日志 |
| AUD-17707 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:139-141` | high | `_resolve_source_clip` 在引用 timeline 缺失时静默回退到任意第一条 timeline |
| AUD-17708 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:392-397` | high | 旧版相对路径回退以进程 CWD 为根，缓存文件可能落到任意目录 |
| AUD-17709 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:116-119、269、299-307、331-335` | medium | 跨类访问 `PreviewRenderWorker` 私有成员 |
| AUD-17710 | Suggestion | process | `src/features/edit/core/render_cache_worker.py:360-366` | high | 类型注解缺失：裸 `dict` 泛型与未注解参数 |
| AUD-17801 | Major | bug | `src/features/edit/core/scene_cut/apply_ops.py:352-390（` | medium | apply_splits 不去重相同 timeline_frame 的候选，宏执行时第二条 SplitClipCommand 抛 ValueError 导致整个操作回滚失败 |
| AUD-17802 | Suggestion | process | `src/features/edit/core/scene_cut/apply_ops.py:751` | high | detect_conflicts 用真值判断跳过 timeline_frame==0，与 apply_markers 已修复的同类缺陷不一致 |
| AUD-17803 | Suggestion | process | `src/features/edit/core/scene_cut/apply_ops.py:538-556` | medium | verify_split_preserves_attributes 与 SplitClipCommand/MacroCommand 实际语义不符，会产生误报与漏报 |
| AUD-17804 | Suggestion | process | `src/features/edit/core/scene_cut/apply_ops.py:416-440` | medium | _split_linked_clip 只在 audio_tracks 中查找链接剪辑，反向链接（目标为音频剪辑、链接对象为视频）永远失败 |
| AUD-17805 | Suggestion | process | `src/features/edit/core/scene_cut/apply_ops.py:549-552、588、627` | high | 访问私有属性 _source_fps_num 削弱封装；apply_all 的命令列表类型退化为 List[Any] |
| AUD-17810 | Major | bug | `src/features/edit/core/scene_cut/cache.py:341-367（对照 models.py:752-775、models.py:605-606、models.py:126-135）` | medium | load_analysis 未捕获 ValueError，损坏/异构的外部 JSON 会击穿 "invalid → None" 契约 |
| AUD-17811 | Suggestion | process | `src/features/edit/core/scene_cut/cache.py:386-400（对照 437、459 的 glob("*.json")）` | medium | save_analysis 固定 .tmp 临时名：并发写同 key 互相覆盖，非 OSError 失败残留 .tmp 且永不清理 |
| AUD-17812 | Suggestion | doc-gap | `src/features/edit/core/scene_cut/cache.py:600-608、699-721、746-776、880-906（对照 19-23 行设计原则）` | medium | ThumbnailCache 全程无锁，与模块 docstring "Thread-safe operations" 的声明不符 |
| AUD-17813 | Suggestion | risk | `src/features/edit/core/scene_cut/cache.py:1113-1123、617-645、591-608（对照 scene_cut_dialog.py:1873、1884）` | medium | get_scene_cut_cache 非单例且每次构造全量 stat 扫描 thumbs 目录，UI 线程上重复付出 O(n) 开销 |
| AUD-17814 | Suggestion | process | `src/features/edit/core/scene_cut/cache.py:1056-1063` | medium | SceneCutCache.save_analysis 内含未完成的死循环：计算结果弃用、访问私有方法、候选缩略图键永不回填 |
| AUD-17815 | Suggestion | process | `src/features/edit/core/scene_cut/cache.py:1080-1093（对照 687-721 的 has_thumb、779-814 的 load_thumb）` | medium | invalidate_asset 绕过 ThumbnailCache 跟踪直接 unlink，造成 has_thumb 与 load_thumb 结果矛盾及 _current_size 虚高 |
| AUD-17816 | Suggestion | process | `src/features/edit/core/scene_cut/cache.py:752-757（对照 save_analysis 的 temp+replace，388-393）` | high | save_thumb 直接写最终路径，非原子写 |
| AUD-17817 | Suggestion | process | `src/features/edit/core/scene_cut/cache.py:505-526（对照 check_cache_validity 1126-1164，后者校验 settings.to_hash()）` | medium | AnalysisCache.is_cache_valid 的 settings 参数从未使用，校验语义与同名顶层函数不一致 |
| AUD-17901 | Major | bug | `src/features/edit/core/scene_cut/detector.py:477` | medium | 取消令牌抛出的 CancelledError 被宽泛 except 吞掉，取消被误报为 ERROR 相位 |
| AUD-17902 | Major | bug | `src/features/edit/core/scene_cut/detector.py:477-493（契约声明在 270-272）` | medium | 检测失败被转换为"成功完成的空结果"，违背自身 Raises 契约并掩盖领域错误 |
| AUD-17903 | Major | bug | `src/features/edit/core/scene_cut/detector.py:589-592` | high | SceneCutDetector.cancel() 无法取消进行中的检测 |
| AUD-17904 | Major | bug | `src/features/edit/core/scene_cut/detector.py:339-342, 382-386` | medium | "流式管线"仍把全部采样帧像素数据留在内存，长素材内存 O(N×~108KB) |
| AUD-17905 | Suggestion | process | `src/features/edit/core/scene_cut/detector.py:125-142, 495-504, 506-514` | high | 死代码：calculate_media_fingerprint/_create_analysis 无人调用，phase 参数被忽略 |
| AUD-17906 | Suggestion | process | `src/features/edit/core/scene_cut/detector.py:529-534` | high | SourceFrameTimeIndex.media_fingerprint 恒为空串，注释声称的回填从未发生 |
| AUD-17907 | Suggestion | process | `src/features/edit/core/scene_cut/detector.py:354, 394-400` | high | 子范围检测时进度分母错位，百分比严重偏低 |
| AUD-17910 | Major | bug | `src/features/edit/core/scene_cut/filters.py:310-321（配合 134-144 的窗口裁剪）` | medium | 自适应阈值实际仅反映"片尾最后 30 帧"统计，而非逐帧滚动，全片共用单一阈值 |
| AUD-17911 | Suggestion | risk | `src/features/edit/core/scene_cut/filters.py:260-269, 301-303` | high | process() 重置 state 与 rolling_stats 但不清 diagnostics，复用时计数器跨次累积 |
| AUD-17912 | Suggestion | process | `src/features/edit/core/scene_cut/filters.py:443-446` | high | FLASH 分类计入 flash_suppressed_count 但候选并未被抑制；guard 间 kind 覆盖不清理 reasons |
| AUD-17913 | Suggestion | risk | `src/features/edit/core/scene_cut/filters.py:340-345, 449-456, 465-470` | medium | motion_frames/black_frames 无界增长且逐候选全表扫描，长素材 O(N²) 风险 |
| AUD-17914 | Suggestion | process | `src/features/edit/core/scene_cut/filters.py:781, 893` | high | min_scene_distance 原地排序修改调用方列表；black_gap_detector 默认阈值与管线设置不一致 |
| AUD-17915 | Suggestion | process | `src/features/edit/core/scene_cut/filters.py:29, 39, 579, 637` | high | 未使用导入与变量：math、FrameTimePoint、consecutive、next_frame |
| AUD-17920 | Major | bug | `src/features/edit/core/scene_cut/frame_sampler.py:431-432, 793-805（sample_frames 469-471 / build_time_index 698-700 同受影响）` | medium | start>0 时 seek 丢弃起始帧且后续帧 source_frame 整体错位一格 |
| AUD-17921 | Major | bug | `src/features/edit/core/scene_cut/frame_sampler.py:882-897` | medium | _convert_frame_data 转换失败静默回退全黑帧，错误像素数据进入检测管线 |
| AUD-17922 | Suggestion | process | `src/features/edit/core/scene_cut/frame_sampler.py:506-508` | high | sample_frames 中 `except StopIteration: pass` 为死分支且可能吞掉循环体异常 |
| AUD-17923 | Suggestion | process | `src/features/edit/core/scene_cut/frame_sampler.py:565-593` | high | sample_single_frame 宽泛 except 把解码/seek 真实错误吞为"帧不存在"，且成功/失败路径留下不一致的游程状态 |
| AUD-17924 | Suggestion | process | `src/features/edit/core/scene_cut/frame_sampler.py:521-522, 356-358` | high | 类型注解瑕疵：`Optional[Any]` 无意义、__exit__ 参数无注解，与文件头"完整类型注解"声明不符 |
| AUD-18001 | Suggestion | process | `src/app/app_context.py:20-31` | high | 全部字段与 getter 返回类型为 Any，类型注解形同虚设 |
| AUD-18002 | Suggestion | process | `src/app/app_context.py:36-39` | medium | feature_flags 缺失时 is_feature_enabled 一律放行（fail-open） |
| AUD-18003 | Suggestion | process | `src/features/edit/core/scene_cut/metrics.py:229-237（仅校验 None 与 shape）；167-170（calcHist）；159（cvtColor）` | medium | 帧输入未校验 dtype/通道数，非 uint8-BGR 帧产生 NaN 指标或 cv2 异常 |
| AUD-18004 | Suggestion | process | `src/features/edit/core/scene_cut/metrics.py:879-882` | high | extract_frame_features 内 try: import cv2 为死代码 |
| AUD-18005 | Suggestion | process | `src/features/edit/core/scene_cut/metrics.py:452-460 与 895-896、933` | medium | Sobel 边缘量纲在两条路径下不一致，粗扫与精扫分数不可比 |
| AUD-18006 | Suggestion | process | `src/features/edit/core/scene_cut/metrics.py:686-699、727-731` | medium | AdaptiveThresholdCalculator 构造参数无验证，window_size<10 时自适应阈值永不生效 |
| AUD-18007 | Suggestion | process | `src/features/edit/core/scene_cut/metrics.py:527-534` | medium | 单侧黑场分数公式方向疑似反转，且中间分支在默认阈值下不可达 |
| AUD-18010 | Major | bug | `src/features/edit/core/scene_cut/models.py:137-151（字段定义 101-106；cache.py:226 消费 settings.to_hash()）` | medium | SceneCutSettings.to_hash() 遗漏 5 个误报抑制阈值字段，缓存键不随设置变化失效 |
| AUD-18011 | Major | bug | `src/features/edit/core/scene_cut/models.py:874-886、896-908（字段定义 809）` | medium | with_updated_candidate / with_all_decisions 重建时丢失 proxy_fingerprint |
| AUD-18012 | Major | bug | `src/features/edit/core/scene_cut/models.py:1285-1311` | medium | timecode_to_frames 未处理 drop-frame 与小数帧率，与 _frames_to_timecode 的 DF 修复不对称 |
| AUD-18013 | Major | bug | `src/features/edit/core/scene_cut/models.py:1186-1203、1205-1207` | medium | generate_scene_subclips 硬编码首场景起始秒为 0.0，范围分析的时间元数据错误 |
| AUD-18014 | Suggestion | process | `src/features/edit/core/scene_cut/models.py:808、886、907` | high | diagnostics 为可变 dict，破坏 frozen 不可变约定且在派生副本间共享引用 |
| AUD-18015 | Suggestion | doc-gap | `src/features/edit/core/scene_cut/models.py:493-494（实现见 metrics.py:642-647）` | high | SceneCutMetrics.combined_score 文档公式与实现不符（多写了 0.05*motion 项） |
| AUD-18016 | Suggestion | process | `src/features/edit/core/scene_cut/models.py:462-474（from_dict）；329-383（source_frame_to_seconds 二分查找）` | medium | 反序列化不校验 points 的排序与秒单调性，二分查找对乱序数据静默给出错误映射 |
| AUD-18100 | Major | risk | `src/features/edit/core/scene_cut/subclips.py:410` | medium | add_subclips_to_bin 手工重建 EditProject 时丢失 revision 字段，乐观锁版本号被静默重置为 0 |
| AUD-18101 | Suggestion | process | `src/features/edit/core/scene_cut/subclips.py:427` | high | add_subclips_to_bin 变更工程内容但不刷新 modified_at |
| AUD-18102 | Suggestion | process | `src/features/edit/core/scene_cut/subclips.py:484` | high | duplicate_strategy 未知取值无 else 分支，静默按 APPEND 语义执行 |
| AUD-18103 | Suggestion | process | `src/features/edit/core/scene_cut/subclips.py:614` | medium | 子剪辑元数据读取/校验函数不验证值类型与范围，反序列化数据可触发 TypeError |
| AUD-18110 | Major | bug | `src/features/edit/core/scene_cut/timeline_mapping.py:388` | medium | _cfr_source_to_timeline 反向分支忽略 fps 换算，与正向/逆向映射不对称，误差随帧距线性增长突破 ≤1 帧承诺 |
| AUD-18111 | Suggestion | process | `src/features/edit/core/scene_cut/timeline_mapping.py:508` | medium | 越界边界结果用 timeline_fps 计算 source_seconds，fps 不同时数值错误 |
| AUD-18112 | Suggestion | process | `src/features/edit/core/scene_cut/timeline_mapping.py:873` | medium | create_mapper 未复用同模块 fps_to_fraction 的 NTSC 快照对齐，两套 fps→Fraction 规则不一致 |
| AUD-18113 | Suggestion | process | `src/features/edit/core/scene_cut/timeline_mapping.py:154` | medium | ClipMappingConfig.__post_init__ 不校验 speed，0 值致 ZeroDivisionError、负值致全部帧 clamp 到片段头 |
| AUD-18114 | Suggestion | process | `src/features/edit/core/scene_cut/timeline_mapping.py:201` | medium | from_clip_instance 直接依赖 ClipInstance 私有下划线属性，且保留不可达的负标量速度分支 |
| AUD-18115 | Suggestion | process | `src/features/edit/core/scene_cut/timeline_mapping.py:944` | medium | estimate_fps_conversion_error 未校验 source_fps>0，0 值直接 ZeroDivisionError |
| AUD-18120 | Major | risk | `src/features/edit/core/scene_cut/worker.py:942` | medium | SceneCutWorkerManager._workers 的清理键与写入键格式不一致，条目永不移除，已完成 worker 强引用持续泄漏 |
| AUD-18121 | Suggestion | process | `src/features/edit/core/scene_cut/worker.py:658` | medium | start_detection 中 self.start() 失败时 registry 条目永不注销，该任务键被永久占用 |
| AUD-18122 | Suggestion | process | `src/features/edit/core/scene_cut/worker.py:746` | medium | ProgressThrottler 的 pending 机制无补发路径，节流窗口内的最后一次进度被丢弃；is_complete 时可能双发同一进度 |
| AUD-18123 | Suggestion | process | `src/features/edit/core/scene_cut/worker.py:542` | medium | CancelToken 一次性且从不复位，已取消的 SceneCutWorker 对象被复用重启时立即误报取消并丢弃结果 |
| AUD-18124 | Suggestion | process | `src/features/edit/core/scene_cut/worker.py:424` | high | SceneCutWorkerSignals 为死代码，且其 QRunnable 相关注释已过时 |
| AUD-18125 | Suggestion | process | `src/features/edit/core/scene_cut/worker.py:867` | medium | cancel_worker 不支持 clip_id 参数，无法取消基于时间线片段的检测任务 |
| AUD-18200 | Major | bug | `src/features/edit/core/scene_cut_controller.py:347` | medium | "Add Cuts to Media Pool" 绕过命令/undo 系统，直接改写并发布项目 |
| AUD-18201 | Major | bug | `src/features/edit/core/scene_cut_controller.py:543` | medium | `_on_add_to_media_pool` 无异常保护，`add_subclips_to_project` 的 ValueError 会逃逸进 Qt 槽 |
| AUD-18202 | Suggestion | process | `src/features/edit/core/scene_cut_controller.py:149` | high | `_task_registry` 赋值后从未使用（死代码） |
| AUD-18203 | Suggestion | process | `src/features/edit/core/scene_cut_controller.py:145` | medium | `_session_analyses` 会话内无上限增长 |
| AUD-18204 | Major | bug | `src/features/edit/core/selection.py:411` | medium | `TimelineSelection.__eq__` 与 dataclass 自动生成的 `__hash__` 违反相等/哈希契约 |
| AUD-18205 | Major | bug | `src/features/edit/core/selection.py:419` | medium | `__eq__` 忽略 primary_item、item_type 与 items 顺序，语义不同的选择被判相等 |
| AUD-18206 | Critical | bug | `src/features/edit/core/serialization.py:656` | medium | v4→v5 迁移把 `project["timelines"]`（TimelineSequenceRef 字典）当序列列表迭代 → AttributeError → 旧版工程加载必败 |
| AUD-18207 | Major | bug | `src/features/edit/core/serialization.py:467` | medium | animated_params 迁移从未触及其实际所在（wrapper["timeline_sequences"]），transform 关键帧动画静默丢失 |
| AUD-18208 | Major | bug | `src/features/edit/core/serialization.py:698` | high | 工程保存非原子写入，中断即损坏唯一工程文件 |
| AUD-18209 | Major | bug | `src/features/edit/core/serialization.py:764` | medium | load/save 四处 `except Exception` 宽泛吞错，掩盖领域错误并混淆失败原因 |
| AUD-18210 | Suggestion | risk | `src/features/edit/core/serialization.py:564` | medium | 反序列化对显式 JSON null 无防御（tuple(None)/metadata=None 直接崩溃） |
| AUD-18211 | Suggestion | process | `src/features/edit/core/serialization.py:625` | high | 版本注释过期："目前版本为 3"，实际 SERIALIZATION_VERSION = 5 |
| AUD-18212 | Major | risk | `src/features/edit/core/sidechain_ducker.py:266` | medium | `auto_duck` 对 sidechain 短于 music 无校验 → 越界 IndexError 崩溃 |
| AUD-18213 | Major | bug | `src/features/edit/core/sidechain_ducker.py:156` | medium | duck 中途禁用/启用产生瞬时增益跳变（click），违背本模块"避免突变"的设计目标 |
| AUD-18214 | Suggestion | process | `src/features/edit/core/sidechain_ducker.py:263` | high | `auto_duck` 传入 1-D music 时被静默直通（无处理也无提示） |
| AUD-18215 | Suggestion | perf-opportunity | `src/features/edit/core/sidechain_ducker.py:179` | medium | 逐样本 Python 循环：实时块尚可、离线整段处理数量级偏慢 |
| AUD-18216 | Major | bug | `src/features/edit/core/stem_export.py:288` | high | stem 渲染将整条时间线 PCM 全量驻留内存（output + 每 Bus 一份全长缓冲），长工程内存爆炸 |
| AUD-18217 | Major | bug | `src/features/edit/core/stem_export.py:84` | high | stem 文件名仅按名称净化生成，同名轨道/组互相覆盖且均记成功 |
| AUD-18218 | Suggestion | process | `src/features/edit/core/stem_export.py:322` | high | 最内层逐帧循环内重复执行模块 import，且 frames_to_samples 重复导入 |
| AUD-18219 | Suggestion | process | `src/features/edit/core/stem_export.py:253` | medium | 嵌套序列音频展开失败被 `except Exception: pass` 静默吞掉，stem 悄悄缺内容 |
| AUD-18220 | Suggestion | process | `src/features/edit/core/stem_export.py:446` | high | write_stem_wav 失败时残留半截损坏 WAV 文件 |
| AUD-18221 | Suggestion | process | `src/features/edit/core/stem_export.py:78` | high | 多处类型注解缺失泛型参数（bare tuple/dict/Callable） |
| AUD-18301 | Major | risk | `src/features/edit/core/subtitle_renderer.py:397-411（及调用方 src/features/edit/ui/program_viewer.py:2880、src/features/edit/core/timeline_export.py:5720-5741 → 6394）` | medium | 全局字幕纹理缓存 `_GLOBAL_CACHE` 无锁，被 UI 绘制线程与导出线程并发读写 |
| AUD-18302 | Major | bug | `src/features/edit/core/subtitle_renderer.py:309-331` | medium | `_warp_rgba_to_canvas` 对旋转四边形外像素做边缘 clamp 采样，旋转字幕出现拖尾条纹伪影且与预览不一致 |
| AUD-18303 | Suggestion | process | `src/features/edit/core/subtitle_renderer.py:191（对照 209-218）` | high | `render_subtitle_clip_to_rgba` 中 4K 全画布分配为死存储，每次调用白付一次大额分配与清零 |
| AUD-18304 | Suggestion | process | `src/features/edit/core/subtitle_renderer.py:82-92, 117-121（对照 src/features/edit/core/captions/model.py:140-157 的 STYLE_FIELDS）` | medium | 34 个样式字段清单在三个地方重复声明，且 `_clip_to_text_params` 用 `style[f]` 直接索引 |
| AUD-18310 | Suggestion | process | `src/features/edit/core/take_sheet.py:238-257（对照 199-203）` | medium | `build_take_sheet_from_project` 对持久化元数据做强类型转换但无逐条异常防御，与 `from_dict` 不一致 |
| AUD-18320 | Major | risk | `src/features/edit/core/timeline_clip_frame_provider.py:913, 1154-1157（仅 2082 一处清理）` | medium | `_pending_source_frames` 无界累积：scrub 期间后台解码帧仅在精确同帧再次请求时弹出，CPU 内存持续增长 |
| AUD-18321 | Major | bug | `src/features/edit/core/timeline_clip_frame_provider.py:524-529, 879-884, 1077-1079` | medium | 源纹理缓存"淘汰即释放"，返回中的 `_TexResult` 无引用保护——同图多素材求值时纹理名可在采样前被释放 |
| AUD-18322 | Major | bug | `src/features/edit/core/timeline_clip_frame_provider.py:449, 1792（对照 1769-1774 已知 bit_depth）` | medium | YUV→RGB 转换输出纹理固定 8-bit（dtype='f1'），10/12-bit HDR 源在 IDT 前被量化 |
| AUD-18323 | Suggestion | risk | `src/features/edit/core/timeline_clip_frame_provider.py:625, 2050-2058` | medium | `_gl_thread_id` 每次 `get_timeline_clip_texture` 无条件覆写，provider 被两个 GL 线程先后复用时跨线程 clear 协议失真 |
| AUD-18324 | Suggestion | process | `src/features/edit/core/timeline_clip_frame_provider.py:454-457, 494-499, 974-977` | medium | deferred GPU 帧导入失败路径：调用方立即 `safe_release_gpu_frame` 抢先于 fence 延迟释放，违背"安全时机释放"契约 |
| AUD-18325 | Suggestion | risk | `src/features/edit/core/timeline_clip_frame_provider.py:2021-2034` | medium | `clear_debug_cache` 名不副实：实际释放全部 owned 纹理/源缓存/解码会话，且无 clear_source_cache 的跨线程停靠保护 |
| AUD-18326 | Suggestion | risk | `src/features/edit/core/timeline_clip_frame_provider.py:1256-1261` | high | `_detect_source_color_space` 的 info=None 早退不写缓存，无 clip info 的素材在渲染线程逐帧重复探测 |
| AUD-18327 | Suggestion | process | `src/features/edit/core/timeline_clip_frame_provider.py:1605-1607, 1895-1897` | high | 上传路径宽泛 `except Exception` 且仅 logger.debug，真实 GL/解码异常根因不可诊断 |
| AUD-18328 | Suggestion | risk | `src/features/edit/core/timeline_clip_frame_provider.py:1115-1127, 1194-1206, 2079-2082` | medium | 异步解码停止协议竞态：join=False 即弃引用，随后 `_ensure_async_decode_thread` 复位 stop 标志可"复活"未退出的旧 worker 并在清空后回写 pending |
| AUD-18400 | Critical | bug | `src/features/edit/core/timeline_export.py:1212, 1232, 1530, 1590, 1373, 1491` | medium | CPU 效果处理器硬编码 /255 归一与 uint8 输出——高位深（uint16）导出帧经 sharpen/denoise/edge_detect 后整帧饱和 |
| AUD-18401 | Major | risk | `src/features/edit/core/timeline_export.py:5132-5153（对照：取消路径 5077 有 join；主路径异常 3028 有 join）` | medium | `_export_direct_topos` 异常路径未汇合音频渲染线程——线程跨导出存活并竞争共享音频状态 |
| AUD-18402 | Major | risk | `src/features/edit/core/timeline_export.py:3089-3101, 3125, 3129-3134；3221-3237；2667-2669` | medium | 后台 manifest 写线程与后续导出共享可变状态竞态（individual-clips / 同路径连续导出） |
| AUD-18403 | Major | bug | `src/features/edit/core/timeline_export.py:6769-6800（转场侧）对照 6919-6995（主循环）；Metal 侧 7730-7774 支持这些属性` | medium | CPU 转场双源预合成丢弃 clip 效果链/crop/transform/frame-blend——转场期间处理静默丢失 |
| AUD-18404 | Major | bug | `src/features/edit/core/timeline_export.py:553-558, 4685-4687, 4739-4812；UI 暴露点 src/features/deliver/ui/deliver_page.py:358` | medium | PNG/TIFF 图像序列通过配置校验但无编码器——必然运行时失败 |
| AUD-18405 | Major | bug | `src/features/edit/core/timeline_export.py:514-516, 3700-3704, 2799` | medium | CUSTOM 帧范围验证缺口：负值 start_frame 产出前导黑帧；start ≥ 时长产出 0 帧文件且按成功交付 |
| AUD-18406 | Major | bug | `src/features/edit/core/timeline_export.py:4871（bridge 侧` | medium | native VT bridge 以 `int(round(fps))` 传递帧率，非整数帧率时间线音画时长漂移 |
| AUD-18407 | Major | bug | `src/features/edit/core/timeline_export.py:7187-7189（对照 yuv420 门已修的逐帧判据 7141-7146）` | medium | `_gpu_yuv422p10_output_allowed` 一刀切拒绝默认 `burn_subtitles=True`——TOPOS 10-bit Metal 原生 YUV 直出在默认配置下永不生效 |
| AUD-18408 | Major | perf-opportunity | `src/features/edit/core/timeline_export.py:10066-10090（消费点：CPU 主循环 6873/7664；GPU` | medium | `_get_image_frame` 每帧完整磁盘 imread 无缓存——静态图层导出按帧数重复解码 |
| AUD-18409 | Major | bug | `src/features/edit/core/timeline_export.py:8943-8951（` | medium | 效果链/调色运行期失败被宽泛 except 吞掉后帧保持未处理继续交付——违反"不得静默缺效果"原则 |
| AUD-18410 | Suggestion | process | `src/features/edit/core/timeline_export.py:1746（` | medium | 类型注解缺失/弱类型标注 |
| AUD-18411 | Suggestion | risk | `src/features/edit/core/timeline_export.py:2786-2792（对照` | medium | 取消标志合并存在覆盖窗口——极窄竞态可吞掉一次取消请求 |
| AUD-18500 | Critical | bug | `src/features/edit/core/timeline_gpu_export_renderer.py:1634` | medium | deep-u16 效果桥代码块缩进脱出 `if deep_u16:`，效果链在两条主路径上失效或取错纹理 |
| AUD-18501 | Major | bug | `src/features/edit/core/timeline_gpu_export_renderer.py:1361` | medium | 效果链执行路径宽泛 `except Exception: return None` 无日志无计数，掩盖失败（含 AUD-18500） |
| AUD-18502 | Major | risk | `src/features/edit/core/timeline_gpu_export_renderer.py:2152` | medium | release() 在跨线程分支前先闩死 `_released`，一次误调即永久泄漏整个 GL context |
| AUD-18503 | Suggestion | process | `src/features/edit/core/timeline_gpu_export_renderer.py:2164` | medium | release() 资源清单遗漏 f2 ping-pong 与 u16↔f2 桥程序 |
| AUD-18504 | Suggestion | process | `src/features/edit/core/timeline_gpu_export_renderer.py:2088` | medium | opacity uniform 未统一裁剪到 [0,1]，与 direct YUV420 路径口径不一致 |
| AUD-18505 | Suggestion | process | `src/features/edit/core/timeline_gpu_export_renderer.py:961` | medium | release() 后 `_ensure_ready()` 可"复活"实例，且已释放纹理属性未置 None |
| AUD-18506 | Suggestion | process | `src/features/edit/core/timeline_gpu_export_renderer.py:1883` | high | dip 着色器编译失败时静默跳过 dip_to_color 而非 fail-closed 回落 CPU |
| AUD-18507 | Suggestion | process | `src/features/edit/core/timeline_gpu_export_renderer.py:1088` | medium | GpuYuvSource 平面在 pass1 通用路径缺少 dtype/ndim 验证（direct 路径有） |
| AUD-18508 | Major | bug | `src/features/edit/core/timeline_frame_mapper.py:196` | medium | `map_vfr_frame` 完全忽略 `reverse`，VFR 素材反向播放帧序错误 |
| AUD-18509 | Suggestion | process | `src/features/edit/core/timeline_frame_mapper.py:178` | medium | CFR `cadence_action` 在 reverse 下 repeat/skip 判定反转 |
| AUD-18510 | Suggestion | process | `src/features/edit/core/timeline_frame_mapper.py:274` | high | `compute_cadence` 复制请求时丢弃 `source_pts_index`，VFR 模板静默降级为 CFR |
| AUD-18511 | Suggestion | doc-gap | `src/features/edit/core/timeline_frame_mapper.py:185` | medium | `source_time_seconds` 实际语义与文档不符（忽略入点偏移与 reverse 翻转） |
| AUD-18512 | Suggestion | process | `src/features/edit/core/timeline_frame_mapper.py:216` | medium | VFR 时间基准与入点叠加约定未定义，绝对 PTS 语义下双重计入入点 |
| AUD-18513 | Suggestion | process | `src/features/edit/core/timeline_frame_mapper.py:347` | medium | `_apply_frame_blend_layer` 参数无类型注解，且每帧热路径函数内 import |
| AUD-18600 | Critical | bug | `src/features/edit/core/timeline_metal_export_renderer.py:4292` | medium | 同帧多个转场复用同名上传缓冲，CPU 复写覆盖已编码 dispatch 的参数/掩码 |
| AUD-18601 | Major | bug | `src/features/edit/core/timeline_metal_export_renderer.py:293` | medium | Metal u16 / YUV422P10 交付链路在 `to_u8` 处降为 8-bit 精度，与 GL 的 RGB16UI 全链及本文件声明不符 |
| AUD-18602 | Major | bug | `src/features/edit/core/timeline_metal_export_renderer.py:4825` | medium | `render_layer_stack` 的 GPU trail 未记录 CPU 上传字节，full-GPU 证据系统性低估 CPU 搬运 |
| AUD-18603 | Suggestion | process | `src/features/edit/core/timeline_metal_export_renderer.py:5084` | medium | `_make_transition_mask_params` 未验证 progress/angle/softness/vertices 有限性 |
| AUD-18604 | Suggestion | process | `src/features/edit/core/timeline_metal_export_renderer.py:4484` | medium | `GpuYuvSource` 色度平面与 luma/抽样因子的一致性未校验 |
| AUD-18605 | Suggestion | perf-opportunity | `src/features/edit/core/timeline_metal_export_renderer.py:4533` | medium | 单个小半径效果在 planar/packed 源上走逐 tap 全解码+调色的融合路径，4K 性能风险 |
| AUD-18606 | Suggestion | process | `src/features/edit/core/timeline_metal_export_renderer.py:4374` | high | GPU 句柄/来源大量使用 `Any` 注解；个别 TypeError 被归因为 `metal_render_error` |
| AUD-18700 | Major | bug | `src/features/edit/core/timeline_occlusion.py:747-769（成因在 468-477）` | medium | O6 ROI 计划的"单矩形缩小区域"分支不可达，且若可达则把遮挡矩形当作可见区域（语义反转） |
| AUD-18701 | Suggestion | process | `src/features/edit/core/timeline_occlusion.py:40` | high | 未使用的导入 `field` |
| AUD-18702 | Suggestion | process | `src/features/edit/core/timeline_occlusion.py:394-395` | high | `effects_background_independent` 的宽泛 except 静默吞异常、无日志 |
| AUD-18703 | Suggestion | process | `src/features/edit/core/timeline_occlusion.py:682, 685-694` | medium | `compute_visible_region_plan` 的 fail-closed 保护未覆盖函数入口（解包与转场分支在 try 之外） |
| AUD-18800 | Critical | perf-opportunity | `src/features/edit/core/timeline_ops.py:2317（slip_clip；同类：2031-2046 slide、1812-1844 roll、2451-2466 ripple left、2536-2551 ripple right、1452-1540 overwrite 半片/残片）` | medium | 活路径纯操作（slip/slide/roll/ripple trim/overwrite）重建 ClipInstance 时丢弃十余个扩展字段，调色联动、关键帧动画、淡入淡出、混音增益、冻结帧、FPS 元数据等静默丢失 |
| AUD-18801 | Critical | bug | `src/features/edit/core/timeline_ops.py:2139-2145` | medium | slide_clip 中间态触发重叠校验：紧邻轨道上任意方向的 slide 都抛 ValueError，Slide 功能不可用 |
| AUD-18802 | Major | bug | `src/features/edit/core/timeline_ops.py:1800-1809` | medium | roll_trim_clips 调整 source 范围时未按 effective_speed 缩放，变速片段 roll 后内容错位 |
| AUD-18803 | Major | bug | `src/features/edit/core/timeline_ops.py:1319-1339` | medium | insert_clip_at_playhead 不分割跨越播放头的片段，直接抛 ValueError("Clip overlaps")，插入操作失败 |
| AUD-18804 | Major | bug | `src/features/edit/core/timeline_ops.py:2516-2533（ripple right，仅校验 new_source_duration > 0）；1752-1764（roll delta>0 仅校验右片段最短 1 帧）` | medium | ripple_trim_clip_right / roll_trim_clips 左延长无素材上界校验，source 末尾可超出素材实际长度 |
| AUD-18805 | Major | bug | `src/features/edit/core/timeline_ops.py:1022-1033（remove_transition）、1101-1112（update_transition）` | medium | remove_transition / update_transition 手工重建 EditTrack，丢弃 volume/pan/is_sync_locked/is_visible/color/stem_group/automation |
| AUD-18806 | Suggestion | test-gap | `src/features/edit/core/timeline_ops.py:223-238（trim_clip_left）、290-305（trim_clip_right）、343-358（move_clip）、1452-1540（overwrite 各分支）等` | medium | 遗留纯函数 trim_clip_left/trim_clip_right/move_clip/split_clip/add_transition 同样丢弃扩展字段（仅测试/基准调用） |
| AUD-18807 | Suggestion | process | `src/features/edit/core/timeline_ops.py:1544-1549` | high | overwrite_clip_at_playhead 不可达 else 分支中 clip_duration 先用后赋（潜在 UnboundLocalError） |
| AUD-18808 | Suggestion | process | `src/features/edit/core/timeline_ops.py:1301、1396、1661` | high | insert/overwrite/append 未校验 speed>0，speed=0 时除法先于模型校验抛 ZeroDivisionError |
| AUD-18809 | Suggestion | doc-gap | `src/features/edit/core/timeline_ops.py:1339（insert 撞重叠）、1613（overwrite）、375-376（move_clip 跨轨 with_clip_added）、815（add_transition 时长越界经 with_transition_added）` | high | 异常契约不一致：docstring 承诺 TimelineOperationError，模型层 ValueError 直接穿透 |
| AUD-18810 | Suggestion | process | `src/features/edit/core/timeline_ops.py:1896-1951（签名 1900-1901，函数体仅使用 left_max_source_frames，1940-1949）` | medium | get_roll_trim_constraints 的 right_max_source_frames 参数从未被使用（死参数） |
| AUD-18811 | Suggestion | process | `src/features/edit/core/timeline_ops.py:3221-3229` | high | numeric_trim_bounds left 分支 ripple 与非 ripple 计算完全相同（冗余 if/else） |
| AUD-18812 | Suggestion | process | `src/features/edit/core/timeline_ops.py:1598-1601` | high | overwrite_clip_at_playhead 删除被覆盖片段时不清理序列级 marker.clip_id，留下悬挂引用 |
| AUD-18813 | Suggestion | process | `src/features/edit/core/timeline_ops.py:126、156` | medium | split_clip 左右两半共享同一 render_cache_id |
| AUD-18814 | Suggestion | process | `src/features/edit/core/timeline_ops.py:3139-3141（clip_handle_headroom 的 clip）、3159（nearest_marker_frame 的 markers）、3179（_neighbor_bounds 的 track/clip）、3198-3204（numeric_trim_bounds 的 track/clip）、3240-3249（build_numeric_trim_command 无返回注解）、596/622（moves: "List[Dict[str, int]]"）` | medium | 类型注解缺失/不准确：P1-04 辅助函数参数无注解、moves 快照标注 Dict[str, int] 实含 str 值 |
| AUD-18815 | Suggestion | process | `src/features/edit/core/timeline_ops.py:2766-2783、2975` | medium | SnapManager.add_point 重复添加同一点时帧索引列表出现重复项；find_nearest 对 pixels_per_frame=0 除零 |
| AUD-18816 | Suggestion | perf-opportunity | `src/features/edit/core/timeline_ops.py:1328-1336` | medium | insert_clip_at_playhead 逐个 with_clip_updated 平移后续片段，O(n²) 且每步全量重排序 |
| AUD-18817 | Suggestion | process | `src/features/edit/core/timeline_ops.py:381-392` | medium | move_clip ripple 边界用 > 而非 >=，紧邻后续片段不跟随；跨轨移动时 ripple 被静默忽略 |
| AUD-18900 | Critical | risk | `src/features/edit/core/timeline_preview_renderer.py:447-470` | medium | `_GuardedPreviewSource` 的 acquire/retire 为无锁 check-then-act，UI 线程 retire 可关闭正在被 worker 读取的 PyAV 源 |
| AUD-18901 | Major | bug | `src/features/edit/core/timeline_preview_renderer.py:2073-2081` | medium | GPU 路径 solid 分支缩进缺陷：`sc` 在素材缺失时未定义（NameError），或沿用上一层 solid 的旧颜色 |
| AUD-18902 | Major | risk | `src/features/edit/core/timeline_preview_renderer.py:1288-1303` | medium | `PreviewRenderWorker.stop()` 5 秒等待超时后仍强制关闭解码管理器与线程池，复活其注释自认的在飞解码 SIGSEGV 路径 |
| AUD-18903 | Major | bug | `src/features/edit/core/timeline_preview_renderer.py:5535-5544` | medium | CPU 回退把 limited-range yuv420p 直接按 full-range 矩阵转 BGR，黑位/白位错误 |
| AUD-18904 | Suggestion | process | `src/features/edit/core/timeline_preview_renderer.py:1616-1619` | medium | worker 侧 `_playback_session_emitted` 恒为 False，realtime CPU 回退遥测门（RP-P0-10 验收指标）为死代码 |
| AUD-18905 | Suggestion | risk | `src/features/edit/core/timeline_preview_renderer.py:2669-2670, 2934-2940` | medium | `_composite_layers` 保存/恢复 `_current_render_timeline_frame` 无 try/finally，异常时帧号泄漏到后续渲染 |
| AUD-18906 | Suggestion | risk | `src/features/edit/core/timeline_preview_renderer.py:4119-4121, 679-682` | medium | `_mg_static_frames` UI 线程 clear() 与 worker `next(iter(...))` 竞态可抛 StopIteration 丢一帧 |
| AUD-18907 | Suggestion | process | `src/features/edit/core/timeline_preview_renderer.py:9847-9850, 9880-9883` | medium | 去重集合截断用 `list(set)[-N:]`，set 无序导致"保留最新"语义失效 |
| AUD-18908 | Suggestion | process | `src/features/edit/core/timeline_preview_renderer.py:3071-3073, 3103-3109` | medium | `setup_comp_gl_context` 异常路径不恢复主 GL context 的 current surface |
| AUD-18909 | Suggestion | risk | `src/features/edit/core/timeline_preview_renderer.py:3066-3088, 1304-1311` | medium | `_comp_gl_context` / `_comp_gl_surface` 创建后无任何销毁路径（GL 资源泄漏） |
| AUD-18910 | Suggestion | process | `src/features/edit/core/timeline_preview_renderer.py:10127-10142, 10639-10661` | medium | `update_sequence_silent` 不递增 generation，在飞旧渲染结果可短暂上屏/入缓存 |
| AUD-18911 | Suggestion | process | `src/features/edit/core/timeline_preview_renderer.py:672, 201-203, 3041, 4950, 10008, 670` | medium | 类型注解缺失/不一致（含 `if False:` 类型提示块与未使用的 `_mutex`） |
| AUD-19000 | Critical | bug | `src/features/edit/core/timeline_render_plan.py:1425-1434` | medium | TOPOS 直通导出判定漏检 `animated_params` 关键帧，opacity/transform 动画在直通成品中静默丢失 |
| AUD-19001 | Suggestion | process | `src/app/controllers/agent_v2_chat_worker.py:84-86` | medium | run() 顶层 except Exception 将任意异常转为用户可见 failed(str) |
| AUD-19002 | Suggestion | process | `src/app/controllers/agent_v2_chat_worker.py:40-42, 84-89` | medium | finished_result 信号契约与实现不一致（异常路径不发） |
| AUD-19003 | Suggestion | risk | `src/app/controllers/agent_v2_chat_worker.py:92-97` | medium | cancel_requested 与已成功结果存在竞态：晚到的取消会丢弃完整回复 |
| AUD-19004 | Suggestion | process | `src/app/controllers/agent_v2_chat_worker.py:55` | high | 构造参数 parent 缺类型注解 |
| AUD-19005 | Suggestion | process | `src/features/edit/core/timeline_render_plan.py:1050-1065` | medium | dip_to_color 的 `blend_color` 未做类型转换与数值域验证 |
| AUD-19006 | Suggestion | process | `src/features/edit/core/timeline_render_plan.py:930-943 与 1114-1127` | high | fade/opacity 有效值计算在两处重复实现，存在漂移风险 |
| AUD-19007 | Suggestion | process | `src/features/edit/core/timeline_render_plan.py:143` | high | `ClipLayerInfo.transform` 使用裸 `tuple` 注解，未表达 8 元素结构 |
| AUD-19008 | Major | bug | `src/features/edit/core/timeline_thumbnail_scheduler.py:183、356-366` | medium | `_completed_cache` 缩略图缓存无容量上限，且 `clear_cache` 在全库无调用者 |
| AUD-19009 | Suggestion | process | `src/features/edit/core/timeline_thumbnail_scheduler.py:184-185、191-193、209-245` | medium | viewport 派发路径为死代码：`_on_batch_callback` 从未被触发、`_debounce_pending` 从未被读写、`update_viewport` 全库无调用者 |
| AUD-19010 | Suggestion | process | `src/features/edit/core/timeline_thumbnail_scheduler.py:243` | high | `update_viewport` 的 `_pending_jobs` 以 clip_id 为键，同 clip 的 filmstrip 多 job 只保留最后一个 |
| AUD-19011 | Suggestion | process | `src/features/edit/core/timeline_thumbnail_scheduler.py:297、317` | medium | `_compute_clip_jobs` 未验证 `source_duration <= 0`，start_end/filmstrip 模式可生成越界源帧 job |
| AUD-19012 | Suggestion | process | `src/features/edit/core/timeline_thumbnail_scheduler.py:209-227` | high | `update_viewport` 的 `viewport_width` 参数未被使用 |
| AUD-19013 | Suggestion | process | `src/features/edit/core/timeline_thumbnail_scheduler.py:141 与 267` | high | 同模块两处 `thumb_w` 常量算法不一致（整数 `16 // 9` vs 浮点 `16.0/9.0`） |
| AUD-19100 | Major | bug | `src/features/edit/core/timeline_v2.py:2491（with_strip_eq）、2511（with_effect_chain）、2544（with_name）、2563（with_color）、2605（with_output_bus）` | medium | 五个手工枚举字段的 EditTrack with_* 方法静默丢失 is_sync_locked 与 track_role |
| AUD-19101 | Major | bug | `src/features/edit/core/timeline_v2.py:3427（to_dict）；契约来源 2973–2979 行` | medium | __post_init__ 显式支持 int/float fps，但 to_dict 无条件访问 fps.num/.den |
| AUD-19102 | Major | bug | `src/features/edit/core/timeline_v2.py:2748–2752` | medium | EditTrack.from_dict 用 `except Exception: pass` 静默丢弃损坏的轨级 automation 数据 |
| AUD-19103 | Major | bug | `src/features/edit/core/timeline_v2.py:3451–3456` | high | TimelineSequence.from_dict 对 master_automation 同样宽泛静默吞异常 |
| AUD-19104 | Major | bug | `src/features/edit/core/timeline_v2.py:2088（TrackEffectInstance.params: dict）、2203（EditTrack.automation: dict，值为可变 KeyframeTrack）、3278（TimelineSequence._rebuild 原样传递 master_automation 引用）、3531–3533（with_master_automation 仅浅拷贝 dict）` | medium | frozen 模型内嵌可变共享子对象，快照/undo 历史间引用别名 |
| AUD-19105 | Suggestion | process | `src/features/edit/core/timeline_v2.py:3462–3466（第一次）与 3501（第二次）` | medium | TimelineSequence.from_dict 对 subtitle_tracks 做了两次完整反序列化 |
| AUD-19106 | Suggestion | process | `src/features/edit/core/timeline_v2.py:1150–1156（ClipInstance）、2780–2786（EditTrack）、3543–3549（TimelineSequence）等` | medium | 所有实体模型 __eq__/__hash__ 仅按 ID 判等，内容不同的两个修订判相等 |
| AUD-19107 | Suggestion | process | `src/features/edit/core/timeline_v2.py:2450–2478` | medium | with_transition_added 不校验同一切点的重复/重叠转场 |
| AUD-19108 | Suggestion | process | `src/features/edit/core/timeline_v2.py:2363 与 2399–2400` | medium | _rebuild 的 caption_metadata 哨兵类型注解非法（裸字符串参与 `\|` 联合）且以字符串字面量作哨兵 |
| AUD-19109 | Suggestion | process | `src/features/edit/core/timeline_v2.py:1646（` | medium | 多处容器字段/参数类型注解未参数化（Suggestion 级维度） |
| AUD-19200 | Suggestion | process | `src/features/edit/core/title_templates.py:36` | high | 用户模板存储目录依赖 `os.getcwd()`，换启动目录后模板"消失" |
| AUD-19201 | Suggestion | process | `src/features/edit/core/title_templates.py:133-140` | medium | `save_title_template` 失败路径残留 `.tmp` 文件；`vars(style)` 兜底可能抛未处理 TypeError |
| AUD-19202 | Major | bug | `src/features/edit/core/transition_catalog.py:153-157` | medium | `dip_to_color` 是合法用户转场 base，但空间/composite 判定不继承它——基于 dip 的用户转场在预览与导出中均退化为普通溶解，dip 效果不渲染 |
| AUD-19203 | Suggestion | process | `src/features/edit/core/transition_catalog.py:111-116` | high | `GPU_FULL_FRAME_TRANSITION_TYPES` 与 `FULL_FRAME_TRANSITION_TYPES` 完全重复且全库无引用 |
| AUD-19204 | Suggestion | process | `src/features/edit/core/transition_catalog.py:148-157` | high | `is_composite_transition` / `is_full_frame_transition` 全库无调用者，实际判定逻辑被调用方内联重复 |
| AUD-19205 | Suggestion | process | `src/features/edit/core/transition_effects.py:385-391 与 245` | medium | 高斯 sigma 上限 96 与 kernel 上限 127 不匹配，大模糊被截断归一化 |
| AUD-19206 | Suggestion | process | `src/features/edit/core/transition_effects.py:150 与 411-422 与 471-480（对照 64 行）` | medium | `_mix_effect`/`_apply_radial_blur`/`_apply_rgb_split`/`_blend_transition_frames` 未校验输入 ndim，与 `_apply_directional_motion |
| AUD-19207 | Major | bug | `src/features/edit/core/transition_mask.py:141-144` | medium | 未知/无顶点类型回退到硬边方向 wipe，与 GPU 侧同类输入返回 None 不一致；full-frame 常数 mask 保护未覆盖 `cross_dissolve`/`dip_to_color` |
| AUD-19208 | Suggestion | perf-opportunity | `src/features/edit/core/transition_mask.py:37-43（对照 70 行 heart 48 顶点）` | medium | `_polygon_mask` 顶点级 Python 循环 × 全帧临时数组，每帧重算无缓存 |
| AUD-19209 | Suggestion | process | `src/features/edit/core/transition_mask_gpu.py:8` | high | 跨模块导入私有符号 `_shape_vertices` |
| AUD-19210 | Major | bug | `src/features/edit/core/transition_wipe.py:46-51` | medium | `get_wipe_params` 对 `angle`/`softness` 的 `float()` 无异常防护，坏外部参数可崩渲染 |
| AUD-19211 | Suggestion | process | `src/features/edit/core/transition_wipe.py:131-141 与 144-168` | low | `lru_cache` 缓存全分辨率数组，`_angle_coord` 以连续 float 角度为键 |
| AUD-19300 | Suggestion | process | `src/features/edit/facade.py:23-28, 112-121, 133-138, 146-151, 163-174, 186-191` | medium | except ImportError 包裹整个构造/调用过程，可掩盖运行期导入错误并静默返回 None |
| AUD-19301 | Suggestion | doc-gap | `src/features/edit/facade.py:48-50` | high | parse_composite_graph_data 对非 dict 输入原样返回，外部数据未验证且与 docstring 不符 |
| AUD-19302 | Major | perf-opportunity | `src/features/edit/module.py:287-331（zoom_in 287-295、zoom_out 296-304、fit 305-313、playback.hud 315-322、export_perf_report 324-331）；自认注释 332-334` | medium | 5 个菜单动作注册到不可达的 View/Timeline 路径，playback HUD 与性能报告导出入口完全不可达 |
| AUD-19303 | Suggestion | process | `src/features/edit/module.py:49-54` | high | __init__ 导入 EditPage 仅作可用性探测，导入即弃置且触发重型 UI 依赖链 |
| AUD-19304 | Suggestion | process | `src/features/edit/module.py:193, 433, 553, 557（register_menu_actions(self, action_registry)、register_shortcuts(self, shortcut_manager)、get_edit_page(self) -> Optional[Any]、register_panels(self, panel_registry)）` | medium | 多个注册方法参数与返回值缺类型注解 |
| AUD-19305 | Major | bug | `src/features/edit/preferences.py:303-313, 406-416, 419-433` | medium | load/restore_defaults/get_scene_cut_settings_from_config 直接消费外部 config 值，无类型与范围验证，异常路径下静默半加载并可能把默认值写回覆盖用户设置 |
| AUD-19306 | Suggestion | process | `src/features/edit/preferences.py:123, 280-282, 384-386` | high | video_preview_max_queue 三处默认值不一致（控件 4，load/restore 3） |
| AUD-19307 | Suggestion | process | `src/features/edit/preferences.py:305, 408` | high | 灵敏度浮点换算用 int() 截断而非 round()，保存值经一次往返漂移 |
| AUD-19308 | Suggestion | process | `src/features/edit/preferences.py:347-353` | medium | apply() 每次硬编码覆写 7 个 decklink_monitor_* 配置键，会冲掉任何外部修改 |
| AUD-19309 | Suggestion | process | `src/features/edit/preferences.py:49` | high | create_widget 的 parent 参数缺类型注解 |
| AUD-19400 | Major | risk | `src/features/edit/ui/adr_take_sheet_panel.py:173-190` | medium | PyAV 解码异常路径泄漏 container 文件句柄 |
| AUD-19401 | Major | risk | `src/features/edit/ui/adr_take_sheet_panel.py:125-127, 178-187` | medium | 试听在 UI 线程同步全量解码整个 take 文件，无时长/大小护栏 |
| AUD-19402 | Suggestion | process | `src/features/edit/ui/adr_take_sheet_panel.py:149-157` | low | `_feed` 忽略 `QIODevice.write` 返回值，部分写入会丢音频数据 |
| AUD-19403 | Suggestion | risk | `src/features/edit/ui/adr_take_sheet_panel.py:346, 412` | high | 设备枚举在 UI 线程同步执行，且面板构造期即触发一次 |
| AUD-19404 | Suggestion | process | `src/features/edit/ui/adr_take_sheet_panel.py:535-542` | high | 试听启动失败静默返回，用户无任何反馈 |
| AUD-19405 | Suggestion | process | `src/features/edit/ui/adr_take_sheet_panel.py:59-60, 469, 486, 529, 460` | high | 类型注解缺失/不一致（含 `dbfs: float` 却判 None） |
| AUD-19406 | Suggestion | process | `src/features/edit/ui/ai_assistant_composer.py:671, 692` | medium | 插件重扫时当前 mode 被静默 normalize，不发 `mode_changed`，controller 侧视图可能滞后 |
| AUD-19407 | Suggestion | risk | `src/features/edit/ui/ai_assistant_composer.py:654-664` | medium | 每次打开会话菜单都在 UI 线程同步重扫插件目录 |
| AUD-19408 | Suggestion | process | `src/features/edit/ui/ai_assistant_composer.py:1392-1393` | high | `item.label` 未转义直接拼入 RichText HTML |
| AUD-19409 | Suggestion | process | `src/features/edit/ui/ai_assistant_composer.py:1089-1102` | high | `set_state` 每次全量销毁重建所有活动行控件 |
| AUD-19410 | Major | bug | `src/features/edit/ui/ai_brief_editor.py:126-129, 172-176, 196` | medium | 批准编辑会静默丢弃不在固定 4 项清单里的自定义 beat |
| AUD-19411 | Suggestion | process | `src/features/edit/ui/ai_brief_editor.py:188-192` | high | 时长 min/max 被静默 clamp 改写，无用户反馈 |
| AUD-19412 | Suggestion | risk | `src/features/edit/ui/ai_decision_inspector.py:68-77` | medium | 锁定/拒绝/替换按钮不按 decision 状态门控 |
| AUD-19500 | Major | bug | `src/features/edit/ui/ai_edit_assistant_panel.py:3583-3595（对照 3578-3581）` | medium | "改用内置模板"按钮与"确认生成"发出完全相同的信号，内置模板路径永远无法触发 |
| AUD-19501 | Major | bug | `src/features/edit/ui/ai_edit_assistant_panel.py:2008-2043（签名只用于滚动决策）；2226（每次重建重复解码图片）` | medium | 每次 refresh_state 全量销毁重建全部消息卡，含 base64 附件解码与 Markdown 重解析 |
| AUD-19502 | Major | bug | `src/features/edit/ui/ai_edit_assistant_panel.py:3253-3267（try/finally 结构）；3304、3309-3313、3459、3528（多处未校验的 dict 访问）` | medium | set_motion_graphic_proposal_card 只有 finally 没有 except，畸形 card_data 的异常直接逃逸到 Qt 槽 |
| AUD-19503 | Major | bug | `src/features/edit/ui/ai_edit_assistant_panel.py:1958-1959；2347-2350；3201-3205；3288` | medium | AI/模型生成的文本未做 HTML 转义直接插入 RichText/AutoText 标签 |
| AUD-19504 | Major | risk | `src/features/edit/ui/ai_edit_assistant_panel.py:2908-2914（对照 2126-2136）` | medium | QTimer.singleShot 滚动 lambda 未防护面板销毁竞态（同类定时器路径已有防护） |
| AUD-19505 | Suggestion | process | `src/features/edit/ui/ai_edit_assistant_panel.py:2382、2422-2423` | high | 超长输入被静默截断到 2000 字符，用户无感知 |
| AUD-19506 | Suggestion | process | `src/features/edit/ui/ai_edit_assistant_panel.py:1914-1947` | medium | 变体卡列表每次刷新全量重建，无签名/差异检查 |
| AUD-19507 | Suggestion | process | `src/features/edit/ui/ai_edit_assistant_panel.py:174；1183-1196；1411-1413；1446-1447` | medium | 多处宽泛 except 静默降级且不记录日志 |
| AUD-19508 | Suggestion | process | `src/features/edit/ui/ai_edit_assistant_panel.py:2100-2102；2526-2530` | high | 硬编码中文提示未走 i18n（tr/_trf） |
| AUD-19509 | Suggestion | process | `src/features/edit/ui/ai_edit_assistant_panel.py:1497；1812-1841（1/2/3/4）；2271（== 5）` | medium | QStackedWidget 页码使用魔法数字索引 |
| AUD-19510 | Suggestion | process | `src/features/edit/ui/ai_edit_assistant_panel.py:254-306（source 参数）；921（menu: Any）；1547（sessions: object）；1949（v）；2184（m）；2321（b）；3254（card_data: dict）` | medium | 多个公开/私有方法参数与容器缺类型注解 |
| AUD-19511 | Suggestion | risk | `src/features/edit/ui/ai_edit_assistant_panel.py:2465-2473；2659-2664` | medium | 图片附件的解码与 JPEG 编码在 UI 线程同步执行 |
| AUD-19512 | Suggestion | process | `src/features/edit/ui/ai_edit_assistant_panel.py:798-810` | medium | @ 媒体候选在每次按键时同步调用 media-pool provider |
| AUD-19513 | Suggestion | perf-opportunity | `src/features/edit/ui/ai_edit_assistant_panel.py:1679-1684；1734-1750` | medium | 无 Run Drawer 时 shimmer 定时器空转 |
| AUD-19600 | Suggestion | process | `src/features/edit/ui/ai_help_answer.py:118-123` | high | 富文本标签直接插值未转义的帮助索引文本 |
| AUD-19601 | Suggestion | process | `src/features/edit/ui/ai_help_answer.py:25-32（另见 ai_skill_card.py:48-56、ai_timeline_diff.py:36-45）` | medium | `_trf` 兜底函数在三个文件中重复实现且行为分叉 |
| AUD-19602 | Suggestion | process | `src/features/edit/ui/ai_job_panel.py:107-122` | medium | set_jobs 每次快照全量拆建行控件，高频进度刷新下开销与体验受损 |
| AUD-19603 | Major | test-gap | `src/features/edit/ui/ai_preferences_page.py:1359-1362` | medium | "测试连接"在 UI 线程同步发起阻塞 HTTP 请求，最长可冻结界面数分钟 |
| AUD-19604 | Major | test-gap | `src/features/edit/ui/ai_preferences_page.py:1328-1336` | medium | SOURCE_LOCAL 分支未经任何验证即写入 `_connection_results=True`，能力矩阵将显示"连接测试已通过" |
| AUD-19605 | Major | bug | `src/features/edit/ui/ai_preferences_page.py:1074-1076、878-886、928` | medium | 输入中的 API Key 在切换 profile 时被静默丢弃，与其他字段"切换即 flush"行为不一致 |
| AUD-19606 | Major | bug | `src/features/edit/ui/ai_preferences_page.py:1291-1299` | medium | 删除 profile 时立即从磁盘永久删除已存凭据，与"Apply 才落盘"的提交语义不一致 |
| AUD-19607 | Major | perf-opportunity | `src/features/edit/ui/ai_preferences_page.py:974-977、1109-1178（flush 内` | medium | 每次键入触发两次全量列表重建与两次凭据读盘，性能反模式 |
| AUD-19608 | Suggestion | process | `src/features/edit/ui/ai_preferences_page.py:80-92（模块级 tr）、117-133、440-444、560、723` | medium | 模块级 I18N.tr 快照与大段硬编码中文绕过 i18n |
| AUD-19609 | Suggestion | process | `src/features/edit/ui/ai_preferences_page.py:918、634-638、1165` | high | 上下文上限以"k"为单位 round 后回写，静默篡改原始 token 数 |
| AUD-19610 | Suggestion | process | `src/features/edit/ui/ai_preferences_page.py:419-431` | high | MCP 勾选回调 `except Exception: pass` 静默吞掉网关启停失败 |
| AUD-19611 | Suggestion | process | `src/features/edit/ui/ai_preferences_page.py:97-103、1211-1217` | medium | `_PROVIDER_OPTIONS` 中 "openai" 值重复，智谱 GLM 条目的 placeholder 永不可达 |
| AUD-19612 | Suggestion | process | `src/features/edit/ui/ai_preferences_page.py:234、899-906、1116-1122、1186、1287` | medium | `dict[str, QWidget]` 存取控件 + 密集 `type: ignore[assignment]`，类型安全持续侵蚀 |
| AUD-19613 | Suggestion | process | `src/features/edit/ui/ai_result_card.py:101-103、110-113` | high | 富文本标签未转义 AI 侧来源的 title/status 文本 |
| AUD-19614 | Suggestion | process | `src/features/edit/ui/ai_result_card.py:50-59` | high | `_KIND_LABELS` 硬编码中文不走 i18n |
| AUD-19615 | Suggestion | process | `src/features/edit/ui/ai_skill_card.py:211-215、197、239-250、278-281` | medium | number/required 表单字段缺输入校验，form_values 返回类型不一致 |
| AUD-19616 | Suggestion | process | `src/features/edit/ui/ai_skill_card.py:264-274` | medium | set_form_values 逐字段 `except Exception: pass` 吞掉类型错误 |
| AUD-19617 | Suggestion | process | `src/features/edit/ui/ai_skill_card.py:78、158-159、278-281、287-289` | medium | `AiSkillCard._updating` 是永不生效的死守卫；`is_ready` 与开始按钮判定不一致 |
| AUD-19618 | Suggestion | process | `src/features/edit/ui/ai_skill_card.py:189` | high | `_build_form(self, fields)` 参数缺类型注解 |
| AUD-19619 | Major | bug | `src/features/edit/ui/ai_timeline_diff.py:145-159` | medium | `set_diff` 读取的 `warnings` 从未使用，预检警告在 diff 视图中被整体丢弃 |
| AUD-19620 | Suggestion | doc-gap | `src/features/edit/ui/ai_timeline_diff.py:3-4、151-153` | medium | 高风险置顶仅覆盖 removed，与文档承诺的"替换/音乐版权/降级 effect"不符 |
| AUD-19621 | Suggestion | process | `src/features/edit/ui/ai_timeline_diff.py:123、192-195` | high | 函数内局部 import QPushButton；`_on_apply` 不防空 variant_id |
| AUD-19622 | Major | bug | `src/features/edit/ui/ai_variant_list.py:60` | medium | 硬编码 24.0 fps 做帧→秒换算，非 24fps 工程时长显示错误 |
| AUD-19623 | Suggestion | process | `src/features/edit/ui/ai_variant_list.py:111、151-152` | high | 超过 MAX_VISIBLE 的 variant 被静默截断且无任何提示 |
| AUD-19700 | Suggestion | risk | `src/features/edit/ui/caption_workspace.py:502-505（配合 437-446、477-479）` | medium | Find 命中后 select_clip 被 `_building_ui` 防递归锁抑制，`cue_selected` 不发射，时间线/查看器不会跳转到命中 cue |
| AUD-19701 | Suggestion | process | `src/features/edit/ui/caption_workspace.py:52-62` | high | 未使用的导入：`UpdateCaptionMetadataCommand`、`EditTrack`、`Tuple` |
| AUD-19702 | Suggestion | process | `src/features/edit/ui/caption_workspace.py:480-488` | medium | 多选时 `ids[0]` 依赖 `selectedRows()` 返回顺序，"first selected cue" 可能不是模型序最靠前的 cue |
| AUD-19703 | Suggestion | process | `src/features/edit/ui/caption_workspace.py:480-489` | high | 取消选择（空选区）时早退，状态栏"N selected"计数不刷新 |
| AUD-19710 | Suggestion | process | `src/features/edit/ui/chapter_export_dialog.py:42-46` | high | `sequence: Any` / `parent: Optional[Any]` 弱类型，duck-typing 校验全部下沉到 getattr 默认值 |
| AUD-19711 | Suggestion | process | `src/features/edit/ui/chapter_export_dialog.py:161-163` | high | 保存路径已选但无章节内容时静默 return，无任何用户反馈 |
| AUD-19720 | Major | bug | `src/features/edit/ui/clip_audio_effect_dialog_adapter.py:127-131` | medium | `from_dict` 失败被宽泛 except 吞掉并静默替换为 `default()`，提交时会把默认值写回真实效果参数 |
| AUD-19721 | Suggestion | process | `src/features/edit/ui/clip_audio_effect_dialog_adapter.py:185-191` | high | `_on_committed` 的 `to_dict` 失败被静默丢弃，无日志 |
| AUD-19722 | Suggestion | process | `src/features/edit/ui/clip_audio_effect_dialog_adapter.py:154-159` | high | 注册表信号名 `getattr(dialog, committed_attr, None)` 未命中时静默不连接 |
| AUD-19723 | Suggestion | process | `src/features/edit/ui/clip_audio_effect_dialog_adapter.py:116-125` | medium | 缓存对话框复用时不刷新参数，外部变更（undo/属性面板）后被旧值覆盖 |
| AUD-19730 | Suggestion | doc-gap | `src/features/edit/ui/clip_transform_handle_controller.py:3-17（对照 151-168）` | medium | 模块 docstring 描述过时：声称操作"normalized TransformParams (position_x/y, scale_x/y, anchor_x/y)"并做归一化↔像素桥接，实际代码直接使用像素空间字段 |
| AUD-19731 | Suggestion | process | `src/features/edit/ui/clip_transform_handle_controller.py:42、24` | high | 死代码：`_IDENTITY` 常量与 `Any` 导入均未使用 |
| AUD-19732 | Suggestion | process | `src/features/edit/ui/clip_transform_handle_controller.py:264-266` | high | `hit_test_body` 捕获全部异常且不记录日志，与同类方法不一致 |
| AUD-19733 | Suggestion | process | `src/features/edit/ui/clip_transform_handle_controller.py:351-357（对照 324-327 的防护写法）` | medium | `update_hover` 直接调用 `self._viewer.update()`，缺少其他路径都有的 RuntimeError 防护 |
| AUD-19734 | Suggestion | process | `src/features/edit/ui/clip_transform_handle_controller.py:99-103` | medium | `on_drag_updated` / `on_drag_ended` 回调属性无类型注解（裸 `= None`） |
| AUD-19740 | Critical | risk | `src/features/edit/ui/comp_clip_settings_dialog.py:441（配合 268-270、397-407）` | medium | fps 下拉的 "Custom" 项无输入控件，选中后确定触发 `float("__custom__")` ValueError，在调用方 finished 槽内未捕获导致崩溃 |
| AUD-19741 | Suggestion | process | `src/features/edit/ui/comp_clip_settings_dialog.py:121-123（配合 309-312）` | medium | `background_color` 初始值未验证长度/类型，畸形数据直接进 `linear_rgba_to_qcolor` 解包 |
| AUD-19742 | Suggestion | process | `src/features/edit/ui/comp_clip_settings_dialog.py:24、71-88` | medium | 死代码：`_color_to_qcolor`、`_color_swatch_style` 无任何调用；`C` 导入未使用 |
| AUD-19743 | Suggestion | process | `src/features/edit/ui/comp_clip_settings_dialog.py:413-419` | high | 名称为空时静默拒绝接受（仅 setFocus），无提示信息 |
| AUD-19800 | Critical | bug | `src/features/edit/ui/edit_effects_panel.py:1534（方法定义 1524–1536；唯一导入在 1446–1449；信号连接在 1483–1485）` | medium | MG 生成器卡片双击触发 NameError（`template_display_name` 仅为函数内局部导入），未捕获异常逃出 Qt 槽可致应用中止 |
| AUD-19801 | Major | bug | `src/features/edit/ui/edit_effects_panel.py:270–300` | medium | MG 悬停预览的 ToolTip 弹窗与 QMovie 均无父对象，卡片在悬停期间被销毁时留下孤儿窗口和持续解码的 GIF |
| AUD-19802 | Major | risk | `src/features/edit/ui/edit_effects_panel.py:621–623、1492–1495；对照 src/features/teaching_animation/core/motion_graphic_generator_registry.py:57–63` | medium | 注册表监听器在调用方线程内同步重建 Qt 控件，无 UI 线程亲和性保护；注册表侧还会静默吞掉监听器异常 |
| AUD-19803 | Major | bug | `src/features/edit/ui/edit_effects_panel.py:1872–1892（每击键重建）；1363–1409（逐效果徽标解析 + 双重图标渲染）` | medium | 搜索无防抖：每击键全量销毁并重建整棵 Video 树，且每个效果重复栅格化两次 SVG 图标并做逐项文件系统 IO |
| AUD-19804 | Suggestion | risk | `src/features/edit/ui/edit_effects_panel.py:2053–2061（` | medium | `search_input.clear()` 程序化更新无状态锁，`textChanged` 重入导致 `_populate_video_effects` 双重全量重建（违反“程序化更新控件须状态锁”铁律） |
| AUD-19805 | Suggestion | process | `src/features/edit/ui/edit_effects_panel.py:628–638` | high | `effect_favorites` 外部持久化配置值未验证类型/元素，损坏配置可使面板初始化抛 TypeError |
| AUD-19806 | Suggestion | process | `src/features/edit/ui/edit_effects_panel.py:1363–1377` | medium | 效果徽标解析的 `except Exception` 静默吞错且无日志（有 noqa 说明意图，但零可观测性） |
| AUD-19807 | Suggestion | risk | `src/features/edit/ui/edit_effects_panel.py:1815` | high | 局部变量注解引用未导入的 `CaptionStylePreset`（静态/类型检查错误；PEP 526 下运行时不求值故不崩溃） |
| AUD-19808 | Suggestion | process | `src/features/edit/ui/edit_effects_panel.py:516–532` | medium | `_reflow` 仅对 `range(3)` 设置列 stretch，tile 模式允许 4 列时第 4 列不参与拉伸分布 |
| AUD-19809 | Suggestion | process | `src/features/edit/ui/edit_effects_panel.py:1086/1095/1104（` | medium | 大量硬编码中文 UI 字符串与颜色绕过 I18N/主题令牌；上下文菜单用硬编码标签页索引判断转场页 |
| AUD-19810 | Suggestion | process | `src/features/edit/ui/edit_effects_panel.py:1975–2005` | medium | `_start_effect_drag` 为死代码（已被注入式 `custom_start_drag` 取代） |
| AUD-19811 | Suggestion | process | `src/features/edit/ui/edit_info_panel.py:75–77` | high | `_format_frame_time` 用 `int()` 截断构造时基，分数帧率（23.976/29.97）下时码显示漂移 |
| AUD-19812 | Suggestion | process | `src/features/edit/ui/edit_info_panel.py:88、181、375` | high | `_value_labels` 只写不读（死状态），且以显示文本为键，跨区段同名标签互相覆盖 |
| AUD-19900 | Major | bug | `src/features/edit/ui/edit_page.py:3256` | medium | 字幕轨语言设置调用不存在的方法 `_execute_subtitle_command`，功能必然失败 |
| AUD-19901 | Major | bug | `src/features/edit/ui/edit_page.py:2474-2475（辅证 710、949-958）` | medium | 隐藏页面帧请求缺 `_preview_page_is_active()` 门控：Color 页播放可触发隐藏 Edit 预览渲染 |
| AUD-19902 | Major | bug | `src/features/edit/ui/edit_page.py:2913-2919` | medium | 场景剪切候选叠加未绑定素材、未做源帧→时间线帧映射，标记位置错误/串轨 |
| AUD-19903 | Suggestion | process | `src/features/edit/ui/edit_page.py:2544-2562` | medium | `create_timeline()` 为空壳：emit 空 timeline_id 并返回 ""，不创建任何时间线 |
| AUD-19904 | Suggestion | process | `src/features/edit/ui/edit_page.py:1900-1926（关键行 1924）` | high | `_update_audio_track_state` 死代码且穿透私有 `_mixer` 属性 |
| AUD-19905 | Suggestion | process | `src/features/edit/ui/edit_page.py:2099-2104、2148-2149、2176-2177、2191-2193` | medium | 字幕叠加路径多处静默吞异常（`except Exception: pass/return None`，无日志） |
| AUD-19906 | Suggestion | process | `src/features/edit/ui/edit_page.py:2397-2416、2418-2433` | low | `set_fps`/`_frame_to_timecode` 缺 fps>0 防御校验 |
| AUD-19907 | Suggestion | process | `src/features/edit/ui/edit_page.py:3246-3256` | medium | BCP-47 语言标签输入无任何校验（空串/任意文本直接入库） |
| AUD-19908 | Suggestion | process | `src/features/edit/ui/edit_page.py:2114、2152、2759` | high | 若干辅助函数参数/返回类型注解缺失 |
| AUD-2000 | Critical | risk | `src/app/controllers/ai_edit_controller.py:146（run 内交换），5000（重叠触发点）` | medium | `_PlanWorker` 对 `on_state_changed` 的非原子摘除/恢复在规划 worker 重叠时造成跨线程 UI 回填竞态 |
| AUD-20000 | Critical | bug | `src/features/edit/ui/edit_properties_panel.py:838, 2127-2130, 351-352` | medium | 效果关键帧 ◆ 按钮永远在 frame 0 写/删关键帧：set_current_frame 全库无任何调用者 |
| AUD-20001 | Major | bug | `src/features/edit/ui/edit_properties_panel.py:1661-1663, 1593-1597, 1688-1696` | medium | 特效 section 的 Reset 写入“全局选中特效”而非被重置的 section（_selected_effect_id 误用） |
| AUD-20002 | Major | risk | `src/features/edit/ui/edit_properties_panel.py:1665-1676（对照 1259-1266、1421-1438）` | medium | _sync_paired_widget 注释称“同步 slider”但只更新 DragValue，链锁下用滑块拖 scale 时对侧滑块停滞 |
| AUD-20003 | Major | risk | `src/features/edit/ui/edit_properties_panel.py:3884-3892, 3908-3915` | medium | _create_effect_xy_row：链锁状态下拖 DragValue 不同步自身 slider（else 分支才同步） |
| AUD-20004 | Major | bug | `src/features/edit/ui/edit_properties_panel.py:3188-3197（同型：3217-3232、3291-3306、3313-3327）` | medium | Solid Color 数值行重接线：valueChanged.disconnect() 移除了 _build_numeric_row 的拖动→滑块同步闭包 |
| AUD-20005 | Major | bug | `src/features/edit/ui/edit_properties_panel.py:2385-2392` | medium | 字幕 Family 可编辑下拉同时挂 currentIndexChanged 与 editTextChanged：下拉选择一次提交两条命令、手输每键一条 |
| AUD-20006 | Major | bug | `src/features/edit/ui/edit_properties_panel.py:4505-4508（对照控制器 edit_controller.py:2221-2230）` | medium | 轨道名 QLineEdit 用 textChanged 逐字符提交 RenameTrackCommand（无 debounce、无 _updating 守卫） |
| AUD-20007 | Major | bug | `src/features/edit/ui/edit_properties_panel.py:1688-1696, 1538-1597` | medium | Solid Color section 的 Reset：信号被 _route_clip_param 静默丢弃，但控件显示被归位 → UI 与数据脱节且无撤销 |
| AUD-20008 | Major | bug | `src/features/edit/ui/edit_properties_panel.py:1678-1706（按钮无条件创建于 561-568）` | medium | Transition / Subtitle section 的 Reset 按钮完全静默无效（_on_section_reset 未处理这两种 kind） |
| AUD-20009 | Suggestion | process | `src/features/edit/ui/edit_properties_panel.py:1689-1696` | high | Reset 逐参数各发一条命令：crop/opacity/speed/raw 一次 Reset 产生多条撤销项，与 transform 单命令不一致 |
| AUD-2001 | Major | bug | `src/app/controllers/ai_edit_controller.py:1958（begin），2016（早退）` | medium | `_start_chat` 多模态校验失败早退路径遗留永不终结的 Run Drawer "running" 活动项 |
| AUD-20010 | Suggestion | process | `src/features/edit/ui/edit_properties_panel.py:706-709（另 2364-2367）` | high | 宽泛 except 吞错无日志：_refresh_effect_status_badge `except Exception: pass`（另 QFontDatabase 分支同型） |
| AUD-20011 | Suggestion | risk | `src/features/edit/ui/edit_properties_panel.py:4056-4083, 4210-4236, 4423-4436, 4463-4477` | medium | 效果参数控件回调缺少 _updating 防递归守卫（铁律要求程序化更新须状态锁） |
| AUD-20012 | Suggestion | process | `src/features/edit/ui/edit_properties_panel.py:1071-1077 对照 3653-3657、3663-3667、3678-3682` | medium | effect_id 解析不一致：drop 处理兼容含冒号 id（join parts[3:]），enable/delete/open 只取 parts[3] |
| AUD-20013 | Suggestion | process | `src/features/edit/ui/edit_properties_panel.py:2224-2226（` | medium | 模块级构建函数与若干方法缺类型注解 |
| AUD-20014 | Suggestion | process | `src/features/edit/ui/edit_properties_panel.py:1573-1585` | high | raw 参数重建 `except ValueError: return` 静默吞错，无日志无用户反馈 |
| AUD-20015 | Suggestion | process | `src/features/edit/ui/edit_properties_panel.py:975-980（对照 2306-2337）` | medium | 字幕文本 debounce：_clear_sections 停止并删除定时器但不 flush 未提交文本，特定路径丢失最后 <250ms 输入 |
| AUD-20016 | Suggestion | process | `src/features/edit/ui/edit_properties_panel.py:2363-2370` | medium | 每次重建字幕 Font section 都全量枚举 QFontDatabase.families() |
| AUD-2002 | Major | risk | `src/app/controllers/ai_edit_controller.py:893（暂存），947-961（媒体模式早退），1975-1976 + 2000（消费）` | medium | 非 chat 模式请求早退后 `_pending_chat_attachments` 跨请求残留，会把旧附件泄漏进后续 feature 发起的隔离 LLM 请求 |
| AUD-2003 | Major | bug | `src/app/controllers/ai_edit_controller.py:3956（cancel 路径），3913-3914（fail 路径对照）` | medium | `_on_chat_cancelled` 对 `teaching_html_generate` 未终结 V2 Motion Graphic run，与 `_on_chat_failed` 不对称，留下幽灵 running 行 |
| AUD-2004 | Major | bug | `src/app/controllers/ai_edit_controller.py:648-657（cancel+wait），740（service 回调置 None），3940-3946（迟到槽）` | medium | dispose() 不清 `_chat_worker`/`_plan_worker` 引用、不断开 worker 信号，排队信号在 dispose 后仍向共享 panel 发布旧会话状态 |
| AUD-2005 | Major | risk | `src/app/controllers/ai_edit_controller.py:2082-2112（chat），5016（plan），1276-1281（subtitle），3825（MG worker 有 deleteLater 的对照）` | medium | `_ChatWorker`/`_PlanWorker`/`_SubtitlePlanWorker` 以 `parent=self` 创建且从不 deleteLater——worker 对象及其持有的完整消息数组（含图片 data URL）在 |
| AUD-2006 | Major | bug | `src/app/controllers/ai_edit_controller.py:6112-6137；对照 src/features/ai_harness/runtime.py:352-362` | medium | Run Drawer 取消按钮对 V2 chat run 完全无效（`_on_run_cancel_requested` 只调 HarnessRuntime.cancel，未知 id 静默返回 False） |
| AUD-2007 | Major | risk | `src/app/controllers/ai_edit_controller.py:1243（取 state），1276-1286（跨线程传入）；src/app/controllers/ai_edit_host_bridge_impl.py:294-306（已核验仅浅拷贝顶层 dict）` | medium | `_SubtitlePlanWorker` 在后台线程读取 live `ProjectSequences`（bridge 不做快照），与主线程时间线编辑并发 |
| AUD-2008 | Major | perf-opportunity | `src/app/controllers/ai_edit_controller.py:648-657` | medium | dispose() 对每个 Motion Graphic postprocess worker 串行 `wait(2000)`，Chromium 后处理不可中断，工程切换 UI 冻结可达 2s×N |
| AUD-2009 | Suggestion | process | `src/app/controllers/ai_edit_controller.py:4966-4977；对照 1416-1424（` | medium | `start_chat_run` 用默认 `RunPolicy()` 而非 `_current_permission`，违反 footer 权限“只作用于后续 invocation”的声明语义 |
| AUD-2010 | Suggestion | process | `src/app/controllers/ai_edit_controller.py:3719-3728（占位 + 条件 return），3791-3798（穿透追加）` | medium | `teaching_scene_edit` 响应 handler 返回 False 时穿透普通路径，追加占位消息后又把原始 JSON 追加为 assistant 消息 |
| AUD-20100 | Suggestion | process | `src/features/edit/ui/edit_style.py:53` | medium | EDIT_COLORS 混入非颜色值（int 宽度/alpha），`get_color() -> str` 类型契约对部分键不成立 |
| AUD-20101 | Suggestion | process | `src/features/edit/ui/edit_style.py:508` | medium | 七个样式生成函数全库零调用者（死代码），且 `clip_item_style` 使用普通 QWidget 不会命中的 `:selected` 伪状态 |
| AUD-20102 | Suggestion | process | `src/features/edit/ui/edit_style.py:263` | high | tree_style 选中色硬编码 `rgba(226, 146, 39, …)` 与 accent Token `#E29227` 数值耦合 |
| AUD-20103 | Major | bug | `src/features/edit/ui/interchange_dialog.py:97` | medium | 诊断消息未做 HTML 转义即拼入富文本 QLabel，外部数据中的特殊字符导致显示残缺或被吞 |
| AUD-20104 | Suggestion | process | `src/features/edit/ui/interchange_dialog.py:36` | medium | `_CATEGORY_LABELS` 在模块导入期固化 I18N.tr 翻译，运行时切换语言后类别标签与对话框其余文本语言不一致 |
| AUD-2011 | Suggestion | process | `src/app/controllers/ai_edit_controller.py:3553-3563` | medium | `_invoke_teaching_request_handler` 用捕获 TypeError 探测旧签名，会掩盖 handler 内部真实的 TypeError |
| AUD-2012 | Suggestion | process | `src/app/controllers/ai_edit_controller.py:4327-4330` | high | 以魔法字符串比较宿主错误消息决定降级路径 |
| AUD-2013 | Suggestion | process | `src/app/controllers/ai_edit_controller.py:3670-3671（唯一赋非空值处），3691/3756/3912/3968（清空）` | high | `_teaching_html_generate_system_prompt` 是只写不读的死状态属性 |
| AUD-2014 | Suggestion | risk | `src/app/controllers/ai_edit_controller.py:815-822（` | medium | 每次 service 状态变化都在 UI 线程全量重建聊天历史并估算 token |
| AUD-2015 | Suggestion | risk | `src/app/controllers/ai_edit_controller.py:2678-2687` | high | `persist_v2_state` 非原子写盘，崩溃时可损坏 v2_state.json |
| AUD-2016 | Suggestion | process | `src/app/controllers/ai_edit_controller.py:1758；6259-6313；133-137` | medium | 类型注解缺失/不精确（少量） |
| AUD-20200 | Major | risk | `src/features/edit/ui/media_pool_panel.py:9157` | medium | Quick Look 的 Space 切换永远找不到已打开的对话框，重复弹窗并泄漏 |
| AUD-20201 | Major | risk | `src/features/edit/ui/media_pool_panel.py:3412` | medium | hover-scrub 帧缓存 `_hover_frames` 无界增长（内存泄漏） |
| AUD-20202 | Major | risk | `src/features/edit/ui/media_pool_panel.py:4579` | medium | 5 秒定时器在 UI 线程对全部素材同步执行 `os.path.exists`（网络盘卡顿） |
| AUD-20203 | Major | risk | `src/features/edit/ui/media_pool_panel.py:10358` | medium | `_update_disk_proxy_services` 直接锁其它服务的私有锁并改写私有缓存 |
| AUD-20204 | Major | bug | `src/features/edit/ui/media_pool_panel.py:9090` | high | 批量标记把评级 0（未评级）当作"无操作"丢弃 |
| AUD-20205 | Major | bug | `src/features/edit/ui/media_pool_panel.py:10171` | medium | 代理生成无重复触发防护，pending 状态可并发启动多个 ffmpeg 转码写同一文件 |
| AUD-20206 | Major | perf-opportunity | `src/features/edit/ui/media_pool_panel.py:6446` | medium | 拖拽事件中同步递归遍历目录树，拖入大目录时 UI 冻结 |
| AUD-20207 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:6304` | medium | 大块死代码：同步导入链与未使用的绘制/树函数（约 300+ 行） |
| AUD-20208 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:6694` | medium | 文件夹导入结构镜像逐条 execute 命令，产生 N 个 undo 步骤 |
| AUD-20209 | Suggestion | perf-opportunity | `src/features/edit/ui/media_pool_panel.py:6027` | medium | `_on_thumbnail_ready` 每张缩略图到达即全树扫描，整体 O(n²) |
| AUD-20210 | Suggestion | risk | `src/features/edit/ui/media_pool_panel.py:10396` | high | `_get_proxy_resolution` 在 UI 线程同步 av.open + ffprobe 子进程（超时 5s） |
| AUD-20211 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:5900` | high | 多处宽泛 except 吞错（含静默 pass） |
| AUD-20212 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:7828` | medium | 列表视图双击未跳过 Generator 素材，与网格视图/Enter 键行为不一致 |
| AUD-20213 | Suggestion | perf-opportunity | `src/features/edit/ui/media_pool_panel.py:5408` | high | `_update_timeline_highlight_list` 查找的 "timelines" 顶级节点已不存在，函数永远空转 |
| AUD-20214 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:9839` | high | `_on_replace_media` 使用位置计算重复且控制流混乱 |
| AUD-20215 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:10003` | high | 批量重链接统计用哨兵 `MediaAsset` 伪对象兜底 |
| AUD-20216 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:534` | high | 箭头图标以固定文件名写入共享系统临时目录 |
| AUD-20217 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:1456` | medium | `_pending_thumbnails`/`_pending_subclip_frames` 无失败清理，解码失败后卡片永久停在占位符 |
| AUD-20218 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:802` | medium | 类型注解缺失或过宽（Any/无注解） |
| AUD-20219 | Suggestion | process | `src/features/edit/ui/media_pool_panel.py:7989` | high | 树/网格两套上下文菜单约 300 行复制，且功能项已出现不对称 |
| AUD-20300 | Suggestion | process | `src/features/edit/ui/multicam_panel.py:223-227, 347-351` | medium | 宽泛 `except Exception` 静默吞掉缩略图信号连接失败 |
| AUD-20301 | Suggestion | risk | `src/features/edit/ui/multicam_panel.py:331-337, 339-345` | medium | `_pending_by_key` 在播放期间发起的请求永不清理（缓慢泄漏），迟到结果会短暂显示过期帧 |
| AUD-20302 | Suggestion | process | `src/features/edit/ui/multicam_panel.py:328-345` | high | 两个角度指向同一文件同帧号时 pending 登记互相覆盖，先登记者收不到缩略图 |
| AUD-20303 | Suggestion | process | `src/features/edit/ui/multicam_panel.py:131, 152-154, 233-238` | medium | 类型注解缺失：`mousePressEvent(event)`、若干 dict 泛型与 `set_group` 的 group 参数 |
| AUD-20304 | Suggestion | process | `src/features/edit/ui/multicam_sync_result_dialog.py:63` | high | `ok_rows` 计算后从未使用（死代码） |
| AUD-20305 | Suggestion | process | `src/features/edit/ui/multicam_sync_result_dialog.py:29, 53-55` | high | `parent` 类型注解过窄（`Optional[QDialog]`）且模块级 `logger` 未使用 |
| AUD-20306 | Major | bug | `src/features/edit/ui/new_project_dialog.py:301-315；src/features/edit/core/project.py:2373-2377` | medium | 用户选择的音频采样率被静默忽略（`create_timeline` 不接收 sample_rate，硬编码 48000） |
| AUD-20307 | Suggestion | process | `src/features/edit/ui/new_project_dialog.py:286-318` | high | 项目名未做文件系统字符清洗直接拼接保存路径 |
| AUD-20308 | Suggestion | process | `src/features/edit/ui/new_project_dialog.py:47, 69-70` | high | `RESOLUTION_PRESETS` 在类体（import 时）求值 I18N；构造参数缺类型注解 |
| AUD-20309 | Major | risk | `src/features/edit/ui/new_timeline_dialog.py:573-582, 597-611` | medium | 宽高比锁定状态下选择分辨率预设，最终 W/H 按锁定比例漂移，与所选预设不符且无提示 |
| AUD-20310 | Major | bug | `src/features/edit/ui/new_timeline_dialog.py:520-530` | medium | 编辑模式下序列 fps 不在 8 个预设内时无回退处理，保存会静默把时间线改写成 combo 当前值 |
| AUD-20311 | Suggestion | process | `src/features/edit/ui/new_timeline_dialog.py:630-645` | high | `_on_accept` 校验失败仅 setFocus，无任何错误提示 |
| AUD-20312 | Suggestion | process | `src/features/edit/ui/new_timeline_dialog.py:507-512, 639-644` | medium | 起始时间码校验过松：接受越界值与一位数值，且 config 默认值绕过 validator |
| AUD-20313 | Suggestion | process | `src/features/edit/ui/new_timeline_dialog.py:129-136` | high | `existing_names` 参数缺类型注解 |
| AUD-20314 | Suggestion | process | `src/features/edit/ui/placeholder_dialog.py:25-26` | high | `parent: Optional[object]` 类型过宽 |
| AUD-20315 | Suggestion | process | `src/features/edit/ui/playback_hud.py:169-179, 181-196` | medium | 诊断数值的格式化/比较位于 try 块之外，getter 返回非数值会让 `_refresh` 每 tick 抛异常 |
| AUD-20316 | Suggestion | perf-opportunity | `src/features/edit/ui/playback_hud.py:116-124` | high | 反向播放时实测 fps 冻结在旧值，不更新也不清零 |
| AUD-20400 | Critical | bug | `src/features/edit/ui/program_viewer.py:1050` | medium | CPU 上传路径忽略 QImage 行跨距（bytesPerLine 4 字节对齐），特定宽度下预览帧整体错位 |
| AUD-20401 | Major | bug | `src/features/edit/ui/program_viewer.py:2458（另 2612、2654；触发链 1128–1191）` | medium | 错误状态/时间码 overlay 绘制方法从未被调用（死代码），"Media Offline"、渲染/解码错误、R-8 ghost 等错误反馈 UI 完全失效 |
| AUD-20402 | Major | bug | `src/features/edit/ui/program_viewer.py:1113` | medium | clear_texture 在 GL 回调之外直接调用 glDeleteTextures，未确保上下文 current、也未走 deferred-release |
| AUD-20403 | Major | bug | `src/features/edit/ui/program_viewer.py:1729` | medium | present-frame ack 只在 blit 成功路径触发，源纹理无效或 blit 异常时 renderer slot 悬挂 |
| AUD-20404 | Major | doc-gap | `src/features/edit/ui/program_viewer.py:1265` | medium | 字幕 overlay 的 QPainter 绘制调用被包在 beginNativePainting/endNativePainting 之间，属 Qt 文档定义的未定义行为 |
| AUD-20405 | Suggestion | perf-opportunity | `src/features/edit/ui/program_viewer.py:1348` | high | 每帧 glGetUniformLocation + glBufferData(GL_STATIC_DRAW) 重建顶点缓冲 |
| AUD-20406 | Suggestion | perf-opportunity | `src/features/edit/ui/program_viewer.py:1039` | medium | CPU 上传路径每帧最多 3 次全帧级拷贝（convert + concatenate + ascontiguousarray） |
| AUD-20407 | Suggestion | process | `src/features/edit/ui/program_viewer.py:3283` | high | set_frame 递归保护用 print 输出警告，深度计数用动态属性 |
| AUD-20408 | Suggestion | process | `src/features/edit/ui/program_viewer.py:790` | high | 类型注解缺失/误用：`Optional[callable]`（内建函数当类型）、bare `list`、多处方法无注解 |
| AUD-20409 | Suggestion | process | `src/features/edit/ui/program_viewer.py:3253` | high | 跨对象私有属性访问：widget 直写 viewer 内部状态、viewer 直读控制器 `_active_handle` |
| AUD-20410 | Suggestion | process | `src/features/edit/ui/program_viewer.py:1685` | medium | `except Exception: pass` 宽泛吞掉 GL 异常 |
| AUD-20411 | Suggestion | perf-opportunity | `src/features/edit/ui/program_viewer.py:2940` | high | 每帧每字幕的循环体内执行 import |
| AUD-20412 | Suggestion | process | `src/features/edit/ui/program_viewer.py:2243` | medium | exit_fullscreen 无原父级分支 deleteLater 全屏窗口时可能连 viewer 一起销毁 |
| AUD-20500 | Major | bug | `src/features/edit/ui/quick_project_dialog.py:166-172（交叉证据 src/shared/io/file_io.py:511-516）` | medium | 图片素材的 Auto 分辨率恒为 1920x1080：FileIO.get_metadata 从不返回 "width"/"height" 键 |
| AUD-20501 | Major | perf-opportunity | `src/features/edit/ui/quick_project_dialog.py:61-83, 390-397, 527-536` | medium | 类级预设文本在 import 期冻结，运行时切换语言后 Auto 预设失配，默认分辨率落到 4K DCI |
| AUD-20502 | Suggestion | risk | `src/features/edit/ui/quick_project_dialog.py:105, 146` | high | 构造函数在 UI 线程同步探测媒体元数据，未命中缓存时可阻塞 |
| AUD-20503 | Suggestion | process | `src/features/edit/ui/quick_project_dialog.py:160-161, 178-179` | high | 探测异常被宽泛 except Exception 吞掉后静默采用默认元数据 |
| AUD-20504 | Suggestion | process | `src/features/edit/ui/quick_project_dialog.py:573-601` | high | 项目名未清洗非法字符与路径分隔符 |
| AUD-20505 | Suggestion | process | `src/features/edit/ui/quick_project_dialog.py:58, 625-635` | high | quick_project_created 信号声明后从未发射（死信号） |
| AUD-20506 | Suggestion | process | `src/features/edit/ui/quick_project_dialog.py:138-163` | medium | 非 VIDEO 扩展名一律归类为 image，音频素材会被误判 |
| AUD-20507 | Suggestion | process | `src/features/edit/ui/quick_project_dialog.py:117-124, 649` | high | 裸 dict 返回注解，未参数化 |
| AUD-20508 | Critical | bug | `src/features/edit/ui/relink_media_dialog.py:347, 381, 416, 499（对照正确用法 :288）` | medium | item.data(Qt.ItemDataRole) 传入枚举类型而非 .UserRole 成员，四处调用全部抛 TypeError，重链接功能整体不可用 |
| AUD-20509 | Suggestion | risk | `src/features/edit/ui/relink_media_dialog.py:367-409, 411-463, 465-495` | high | 目录/自动搜索在 UI 线程同步扫描，且对同一目录反复 listdir 无缓存 |
| AUD-20510 | Suggestion | process | `src/features/edit/ui/relink_media_dialog.py:357-365, 497-518` | high | 重链接不校验所选文件与素材媒体类型/扩展名是否匹配 |
| AUD-20511 | Suggestion | process | `src/features/edit/ui/relink_media_dialog.py:209-212, 219-221, 246, 251-253, 258-260, 301, 341-343, 401-409, 455-462, 536-541` | high | 大量界面文案硬编码英文，绕过 I18N |
| AUD-20512 | Suggestion | process | `src/features/edit/ui/relink_media_dialog.py:36-44, 145, 240` | high | 未使用导入与重复导入 |
| AUD-20513 | Suggestion | process | `src/features/edit/ui/relink_media_dialog.py:83, 104-141` | medium | 死状态与绕过 __init__ 的工厂实现 |
| AUD-20600 | Critical | risk | `src/features/edit/ui/scene_cut_dialog.py:2663-2667（配合 1984-1995）` | medium | closeEvent 对 wait 超时后仍在运行的预览线程调用 deleteLater，可销毁运行中的 QThread 导致崩溃 |
| AUD-20601 | Major | bug | `src/features/edit/ui/scene_cut_dialog.py:1792-1796 与 2397-2403` | medium | 持久缓存命中快路径被 sender 守卫整体吞掉：缓存分析永不应用且探测不再启动 |
| AUD-20602 | Major | bug | `src/features/edit/ui/scene_cut_dialog.py:2284-2288、2294-2298` | medium | 批量接受/拒绝结果弹窗显示字面 `{0}` 占位符，计数未格式化 |
| AUD-20603 | Major | risk | `src/features/edit/ui/scene_cut_dialog.py:1782-1784` | medium | UI 线程无超时 `wait()` 等待被取消的探测线程，初始化阶段无法取消可致界面无限冻结 |
| AUD-20604 | Suggestion | risk | `src/features/edit/ui/scene_cut_dialog.py:1641-1648、1660-1688` | medium | `_init_frame_provider` 忽略 wait 超时返回值，切换源可在 provider 锁上阻塞 UI 且旧源迟到帧可穿过陈旧守卫 |
| AUD-20605 | Suggestion | doc-gap | `src/features/edit/ui/scene_cut_dialog.py:1419、2629（承诺）；2596-2604（实现）` | high | Backspace 被文档承诺为拒绝快捷键但未实现 |
| AUD-20606 | Suggestion | process | `src/features/edit/ui/scene_cut_dialog.py:2454-2458` | medium | `_get_total_frames` 无 time_index 时返回硬编码 1000，图表时间轴比例失真 |
| AUD-20607 | Suggestion | process | `src/features/edit/ui/scene_cut_dialog.py:172、398-401` | high | 预览 LRU 缓存容量按张数（300 张 512×288×3）折合约 130MB 常驻内存 |
| AUD-20608 | Suggestion | process | `src/features/edit/ui/scene_cut_dialog.py:1858-1876` | high | `_try_load_cache` 宽泛 `except Exception` 且无任何日志，掩盖缓存领域错误 |
| AUD-20609 | Suggestion | process | `src/features/edit/ui/scene_cut_dialog.py:2251-2260` | medium | 手动剪切候选的 seconds 使用 `_playback_fps`，provider 打开失败时回落 24.0 产生错误时间码 |
| AUD-20610 | Suggestion | process | `src/features/edit/ui/scene_cut_dialog.py:194、1054-1057、2612、2640` | medium | 多处弱类型注解：`Optional[Any]` 与事件对象用 `Any` 而非具体 Qt 类型 |
| AUD-20700 | Major | risk | `src/features/edit/ui/scene_cut_graph_widget.py:743-751（触发点另见 887-888、935-939）` | medium | MiniScrubber.set_total_frames 无状态锁，范围缩小时 Qt 钳制 value 泄漏 frame_changed，引发意外跳帧 |
| AUD-20701 | Suggestion | process | `src/features/edit/ui/scene_cut_graph_widget.py:320-321、750` | high | total_frames 强制下限 100，短于 100 帧的媒体时间轴失真 |
| AUD-20702 | Suggestion | risk | `src/features/edit/ui/scene_cut_graph_widget.py:760-765、896-918、957-974` | medium | `_updating` 状态锁未用 try/finally，异常路径会使锁永久悬挂 |
| AUD-20703 | Suggestion | process | `src/features/edit/ui/scene_cut_graph_widget.py:897-903、1089-1097（对照 332）` | high | 阈值入口未 clamp，各层 get_threshold 可能返回越界值 |
| AUD-20704 | Suggestion | process | `src/features/edit/ui/scene_cut_graph_widget.py:818；40-76` | high | 死代码：sensitivity_changed 信号从不发射；9 个导入未使用 |
| AUD-20705 | Suggestion | process | `src/features/edit/ui/scene_cut_graph_widget.py:929-933` | high | 父控件直接读取子控件私有成员 _current_frame |
| AUD-20706 | Suggestion | process | `src/features/edit/ui/scene_cut_graph_widget.py:427、434、1111` | high | Qt 事件参数用 Any 注解而非具体事件类型 |
| AUD-20710 | Major | bug | `src/features/edit/ui/scene_cut_preview_widget.py:565、570、576；715-717；691` | medium | set_candidate 时间码路径硬编码 24fps，非 24fps 媒体 TC 与 before/after 偏移错误 |
| AUD-20711 | Major | bug | `src/features/edit/ui/scene_cut_preview_widget.py:705-711` | medium | _format_timecode 混用 float fps 与 int(fps) 截断，分数帧率时间码出现分钟级偏差 |
| AUD-20712 | Major | bug | `src/features/edit/ui/scene_cut_preview_widget.py:719-753（分派点 579-587）` | medium | warning/accepted/rejected/normal 四个样式方法内容完全相同，状态高亮功能为空操作 |
| AUD-20713 | Major | doc-gap | `src/features/edit/ui/scene_cut_preview_widget.py:458（文档 433-435、449-450）` | high | frame_clicked 信号声明并文档化"面板点击跳转"，但全文无任何鼠标事件处理，信号永不发射 |
| AUD-20714 | Suggestion | doc-gap | `src/features/edit/ui/scene_cut_preview_widget.py:788-822` | high | ThumbnailLoader.request_thumbnail 在调用线程同步执行 loader_func，与"异步加载"文档相悖；_pending_requests 无完成清理 |
| AUD-20715 | Suggestion | process | `src/features/edit/ui/scene_cut_preview_widget.py:618-655（对照 666-668）` | high | set_frame_preview 不更新 _current_candidate 与高亮状态，遗留陈旧状态 |
| AUD-20716 | Suggestion | process | `src/features/edit/ui/scene_cut_preview_widget.py:286-293（调用点 572）` | high | SinglePreviewPanel.set_score 只存储不显示 |
| AUD-20717 | Suggestion | risk | `src/features/edit/ui/scene_cut_preview_widget.py:554-589、631-655、659-664` | medium | set_candidate/set_frame_preview/clear 的 _updating 锁无 try/finally |
| AUD-20718 | Suggestion | process | `src/features/edit/ui/scene_cut_preview_widget.py:47-68、358` | high | 未使用导入（QSize/QTimer/QBrush/SceneCutKind）与 paintEvent 的 Any 注解 |
| AUD-20800 | Major | bug | `src/features/edit/ui/scene_cut_result_model.py:675、:800` | medium | Start TC 时间码永远按硬编码 24fps 格式化，非 24fps 项目显示错误且随时长漂移 |
| AUD-20801 | Suggestion | process | `src/features/edit/ui/scene_cut_result_model.py:397-401` | medium | set_decision 的 dataChanged 只覆盖 DECISION 单列与 [Display, Background] 角色，Scene 列与 ForegroundRole 不会刷新 |
| AUD-20802 | Suggestion | process | `src/features/edit/ui/scene_cut_result_model.py:253-255` | high | set_candidates 直接持有调用方列表，未做防御性拷贝，也未维持 source_frame 唯一不变量 |
| AUD-20803 | Suggestion | risk | `src/features/edit/ui/scene_cut_result_model.py:460-472（accept_high_confidence；474-555 其余 4 个批量方法同构）` | medium | 批量操作循环期间不持锁，decision_changed 处理器可重入修改列表 |
| AUD-20804 | Suggestion | process | `src/features/edit/ui/scene_cut_result_model.py:315-323` | high | to_updated_analysis 的 decisions 字典是自映射 no-op，且按 source_frame 键在重复帧时会静默合并 |
| AUD-20805 | Suggestion | process | `src/features/edit/ui/scene_cut_result_model.py:898-907` | high | _string_to_decision 对未知字符串静默返回 PENDING，把无效编辑变成状态变更 |
| AUD-20806 | Suggestion | process | `src/features/edit/ui/scene_cut_result_model.py:575-584、:559-569` | high | move_selection_up 在空模型时把 _current_index 置为 0，留下不一致状态 |
| AUD-20807 | Suggestion | doc-gap | `src/features/edit/ui/scene_cut_result_model.py:5-6、:110-112` | high | 文档宣称支持排序，但未实现 sort() |
| AUD-20808 | Suggestion | process | `src/features/edit/ui/scene_cut_shortcuts.py:38、:88` | high | register_scene_cut_shortcuts 参数与 _manager 返回值缺类型注解（noqa 压制） |
| AUD-20809 | Suggestion | process | `src/features/edit/ui/scene_cut_shortcuts.py:71-79、:82-85` | medium | 小键盘 Enter（带 KeypadModifier）无法匹配 Enter 快捷键 |
| AUD-20810 | Major | bug | `src/features/edit/ui/scene_cut_timeline_overlay.py:322` | medium | `timeline_frame or source_frame` 把合法的第 0 帧当作假值回退，标记错位 |
| AUD-20811 | Major | bug | `src/features/edit/ui/scene_cut_timeline_overlay.py:344-347（boundingRect 依赖见 :288）` | medium | update_num_tracks 改变 boundingRect 高度但未调用 prepareGeometryChange，违反 Qt 几何契约 |
| AUD-20812 | Major | bug | `src/features/edit/ui/scene_cut_timeline_overlay.py:288、:401` | medium | 竖线长度按 `num_tracks × 视频轨高(82px)` 估算，含音频/字幕轨或轨道调高时线长错误 |
| AUD-20813 | Major | risk | `src/features/edit/ui/scene_cut_timeline_overlay.py:724-741` | medium | select_candidate 持有 `_updating` 锁时发射 candidate_selected，处理器内的同步回调被静默丢弃 |
| AUD-20814 | Major | bug | `src/features/edit/ui/scene_cut_timeline_overlay.py:296-301 与 :464-468` | medium | hover tooltip 绘制在 boundingRect 之外，违反 Qt 绘制契约，产生残影/不刷新 |
| AUD-20815 | Suggestion | risk | `src/features/edit/ui/scene_cut_timeline_overlay.py:766-783` | medium | show_overlay/hide_overlay/toggle_overlay 不使用 `_updating` 锁，overlay_visibility_changed 存在回环隐患 |
| AUD-20816 | Suggestion | process | `src/features/edit/ui/scene_cut_timeline_overlay.py:303-317（对照 :276-301）` | high | shape() 只含 ruler 底部倒三角，竖线与 handle margin 区域不可点击/悬停 |
| AUD-20817 | Suggestion | process | `src/features/edit/ui/scene_cut_timeline_overlay.py:33、:57、:62-66` | medium | 未使用导入与双重常量导入 |
| AUD-20818 | Suggestion | process | `src/features/edit/ui/scene_cut_timeline_overlay.py:368-372、:824-828、:171/:334、:174/:350` | medium | paint 的 widget 参数类型注解错误；`_pixels_per_frame`/`_fps` 为存而不用的死状态 |
| AUD-20900 | Critical | risk | `src/features/edit/ui/source_viewer.py:701（别名建立）；982-991（删除）；1387（触发点）` | medium | paintGL 将渲染线程池纹理 ID 写入 `_current_tex_id`，close_source→clear_texture 会删除外部（OutputSlotPool）拥有的纹理 |
| AUD-20901 | Critical | risk | `src/features/edit/ui/source_viewer.py:929-943（复用写入）；872-885（重定义路径）；974（保留别名并切断共享引用）` | medium | CPU 回退上传路径按尺寸匹配复用 `_current_tex_id`，会 glTexSubImage2D 写入（或 glTexImage2D 重定义）渲染线程池纹理 |
| AUD-20902 | Major | bug | `src/features/edit/ui/source_viewer.py:852-890（上传）；982-995（删除）；调用点 2013、2029、2190、2208、2273、2285；死字段 532 / 705-709` | medium | 纹理上传/删除在 paintGL/initializeGL/resizeGL 之外执行且全程无 makeCurrent；`_pending_qimage` 延迟上传机制为死字段 |
| AUD-20903 | Major | risk | `src/features/edit/ui/source_viewer.py:2161-2169（同步 read_frame）；2220-2233（watchdog 触发）；2255-2261（request_cpu_fallback 路径）` | medium | CPU 回退为 UI 线程同步解码（关键帧 seek + 前向解码），watchdog 超时与播放 tick 直接阻塞界面 |
| AUD-20904 | Suggestion | perf-opportunity | `src/features/edit/ui/source_viewer.py:773-785` | high | `_render_texture` 每帧 glGetUniformLocation 并以 GL_STATIC_DRAW 全量重传顶点 |
| AUD-20905 | Suggestion | process | `src/features/edit/ui/source_viewer.py:2011-2014` | high | 帧缓存命中路径使用全量 glTexImage2D 的旧接口，比未命中路径更贵且不更新 last-good 状态 |
| AUD-20906 | Suggestion | process | `src/features/edit/ui/source_viewer.py:1374-1379（无日志）；1796-1799、1806-1809、1827-1828、1851-1852 等全部 logger.debug` | high | 宽泛 except 静默吞错（close_source 内 `except Exception: pass` 无任何日志） |
| AUD-20907 | Suggestion | process | `src/features/edit/ui/source_viewer.py:1428-1433` | high | close_source 以 `in_point_changed.emit(0)`/`out_point_changed.emit(0)` 表达"已清空"，与合法帧号 0 语义二义 |
| AUD-20908 | Suggestion | process | `src/features/edit/ui/source_viewer.py:1958-1986（三个 handler）；415（` | medium | 死代码：`_on_insert/_on_overwrite/_on_append` 无任何连接；`_can_edit` 只写不读；`_pending_qimage` 死字段 |
| AUD-20909 | Suggestion | process | `src/features/edit/ui/source_viewer.py:188、442、508、1137（` | medium | 类型注解缺失（类构造器与若干方法无参数/返回注解） |
| AUD-20910 | Major | risk | `src/features/edit/ui/stem_export_dialog.py:392-408（join 与覆盖）；9-12（取消粒度文档）` | medium | reject/close 的 3 秒 join 超时后导出线程仍在后台运行，对话框销毁时存在 QThread destroyed-while-running 崩溃风险 |
| AUD-20911 | Suggestion | process | `src/features/edit/ui/stem_export_dialog.py:88（清洗）；137-143（拼接）` | high | 文件名前缀未做路径安全清洗，`/`、`..` 等字符可使命名结果越出输出目录 |
| AUD-20912 | Suggestion | process | `src/features/edit/ui/stem_export_dialog.py:54-59（定义）；185-189（经` | high | `_STEM_TYPE_LABELS` 硬编码中文标签绕过 I18N 体系 |
| AUD-20913 | Suggestion | process | `src/features/edit/ui/stem_export_dialog.py:74-77、165、354、405` | high | 类型注解泛化为 Any（sequence/project/result 均有真实类型却不引用） |
| AUD-20914 | Suggestion | process | `src/features/edit/ui/speech_to_text_dialog.py:24-27` | high | 跨模块导入 shared/export 面板的下划线私有助手，形成对无关特性的私有依赖 |
| AUD-2100 | Major | bug | `src/app/controllers/ai_edit_host_bridge.py:107（协议声明）、:151（协议声明）、:290-445（Null 实现全文无这两个方法）` | medium | NullAiEditHostBridge 缺失协议方法 `detect_autonomous_replay` 与 `rollback_ai_command` |
| AUD-21001 | Suggestion | process | `src/features/edit/ui/subtitle_clip_item.py:529-531` | high | mouseDoubleClickEvent 以宽泛 `except Exception: pass` 吞掉对话框全部异常 |
| AUD-21002 | Suggestion | process | `src/features/edit/ui/subtitle_clip_item.py:287-291` | high | paint 中 label_color 的 try/except 为死代码且缺少回退分支 |
| AUD-21003 | Suggestion | risk | `src/features/edit/ui/subtitle_clip_item.py:534-540` | medium | keyPressEvent 的 Delete 未检查轨道锁定/编辑锁，与右键菜单路径行为不一致 |
| AUD-21004 | Suggestion | process | `src/features/edit/ui/subtitle_clip_item.py:101,421` | medium | 构造函数未验证 pixels_per_frame>0，mouseMoveEvent 存在除零路径 |
| AUD-21005 | Suggestion | process | `src/features/edit/ui/subtitle_clip_item.py:175` | high | set_drag_clamp_callback 的回调参数缺少类型注解 |
| AUD-21006 | Suggestion | process | `src/features/edit/ui/subtitle_delivery_dialog.py:24-29,55` | high | _FORMAT_LABELS 硬编码中文文案，未走 I18N |
| AUD-21007 | Suggestion | process | `src/features/edit/ui/subtitle_delivery_dialog.py:39-43` | high | `parent` 参数缺少类型注解 |
| AUD-21008 | Major | bug | `src/features/edit/ui/subtitle_handle_controller.py:333-342,421-435` | medium | end_drag 的 final（扁平 cue 字段）与 original（resolved style）取值空间不一致，带轨道样式覆盖时可提交伪更新 |
| AUD-21009 | Suggestion | process | `src/features/edit/ui/subtitle_handle_controller.py:151-152,158-160,309-310,587-588` | medium | 多处宽泛 except 静默吞错（无日志） |
| AUD-2101 | Suggestion | process | `src/app/controllers/ai_edit_host_bridge.py:25、:32、:34` | high | 未使用的导入：SemanticSegment / TimelineDiff / ActionRequest |
| AUD-21010 | Suggestion | process | `src/features/edit/ui/subtitle_handle_controller.py:407,463-476` | medium | update_drag 中 _apply_preview_delta 位于 try 块外，数据类校验异常可逃逸进 Qt 事件派发 |
| AUD-21011 | Suggestion | process | `src/features/edit/ui/subtitle_handle_controller.py:34,104-106,323` | high | 死变量 is_body、未使用的 import math、回调属性缺类型注解 |
| AUD-21012 | Suggestion | process | `src/features/edit/ui/subtitle_handle_controller.py:149` | high | 跨模块访问 viewer 私有方法 `_calculate_display_rect` |
| AUD-21013 | Suggestion | process | `src/features/edit/ui/timeline_interaction.py:201-220` | medium | finish() 与 cancel() 状态重置不对称，残留 original_* 脏值 |
| AUD-21014 | Suggestion | process | `src/features/edit/ui/timeline_interaction.py:178-188` | high | begin() 对未知 kwargs 静默忽略，拼写错误无告警 |
| AUD-2102 | Suggestion | process | `src/app/controllers/ai_edit_host_bridge.py:17（声明）、:434-445（Null 的 composite 实现）` | medium | 模块声明"协议外方法一律不再被调用"，但 composite 方法游离于本协议之外 |
| AUD-2103 | Major | bug | `src/app/controllers/ai_edit_host_bridge_impl.py:1075-1081（按 scope.timeline_id 取 sequence 并测量质量合同）、:1228-1299（委托 perform_video_export）` | medium | AI 导出未绑定已验证的 timeline_id，宿主实际导出的是 Edit 页当前时间线 |
| AUD-2104 | Major | bug | `src/app/controllers/ai_edit_host_bridge_impl.py:1290-1303` | medium | AI 导出 Job 在宿主"静默取消"路径下永久停留在 running |
| AUD-2105 | Major | bug | `src/app/controllers/ai_edit_host_bridge_impl.py:1613-1622` | medium | `check_ai_action` 在宿主缺 `feature_flags` 时策略门 fail-open |
| AUD-2106 | Suggestion | process | `src/app/controllers/ai_edit_host_bridge_impl.py:1033-1039（字幕路径）、:1737-1749（composite 路径）` | medium | 一次性 ApprovalGrant 在提交/应用之前被消耗，失败路径烧掉授权 |
| AUD-2107 | Suggestion | process | `src/app/controllers/ai_edit_host_bridge_impl.py:176-182、:206-211；调用方异常捕获 :1180-1181` | medium | `_prepare_brand_quality_contract` 对外部输入做 `tuple()` 未防字符串/标量，TypeError 可逃逸 (bool, str) 契约 |
| AUD-2108 | Suggestion | risk | `src/app/controllers/ai_edit_host_bridge_impl.py:939-953` | medium | ASR worker 信号连接的普通 Python 回调在 ASR 线程执行 |
| AUD-2109 | Suggestion | process | `src/app/controllers/ai_edit_host_bridge_impl.py:1553-1577` | high | `_GeneratedAssetImporter.import_asset` 吞掉具体失败原因，返回空诊断 |
| AUD-2110 | Suggestion | risk | `src/app/controllers/ai_edit_host_bridge_impl.py:1150-1157（preflight）、:1166-1179（质量 cues/品牌元素）` | medium | `request_ai_export` 在 UI 线程同步执行字幕 bbox 逐条测量与导出预检 |
| AUD-21100 | Critical | risk | `src/features/edit/ui/timeline_items.py:5234（方法 5205–5248，调用点 4469）` | medium | Retime 段速度菜单引用未导入的 `I18N`，点击即抛 NameError（PyQt6 默认 qFatal 可致应用崩溃） |
| AUD-21101 | Major | bug | `src/features/edit/ui/timeline_items.py:5064（exec 位于 5024）` | medium | Audio Gain 菜单项在 `menu.exec()` 之后才 addAction，入口永远不可达 |
| AUD-21102 | Major | risk | `src/features/edit/ui/timeline_items.py:6315（对照 ClipItem:4456；view 侧 handler timeline_view.py:10762 起也无锁守卫）` | medium | TransitionItem 无轨道锁状态，锁定轨道上的转场仍可修剪/移动/删除 |
| AUD-21103 | Major | bug | `src/features/edit/ui/timeline_items.py:4410（各分支 4310/4321/4330/4348/4367/4380/4391 同样先 update 再 return）` | medium | ClipItem.hoverMoveEvent 每次鼠标移动无条件 update()，导致 DeviceCoordinateCache 整卡重建 |
| AUD-21104 | Major | bug | `src/features/edit/ui/timeline_items.py:8210–8217` | medium | 轨道头上下文菜单 target/sync 动作先改本地状态再发信号，违反 P4 提交一致性约定 |
| AUD-21105 | Major | bug | `src/features/edit/ui/timeline_items.py:9962–9965` | medium | DragPreviewItem._draw_thumbnail 对零尺寸 QPixmap 无防护，paint 内除零 |
| AUD-21106 | Suggestion | process | `src/features/edit/ui/timeline_items.py:1360、1374、1428、2738、578、329–384` | medium | 多处死代码与从未写入的缓存：`_cache_key`、`_static_cache_key`、`_waveform_start_offset_samples`、`_draw_loading_thumbnails`、`ClipItem._th |
| AUD-21107 | Suggestion | process | `src/features/edit/ui/timeline_items.py:984–985、1044–1045、1290–1291、4664–4668、4679–4683` | high | 多处宽泛 `except Exception` 吞错，违反 .clinerules 异常铁律 |
| AUD-21108 | Suggestion | process | `src/features/edit/ui/timeline_items.py:35、62、59、3441` | high | 未使用/重复导入：`QGraphicsLineItem`、`WaveformCache`、`get_size` 及函数内重复导入 `is_waveform_pending` |
| AUD-21109 | Suggestion | process | `src/features/edit/ui/timeline_items.py:10636、10661、10680、10699` | high | MarkerItem 事件方法注解引用未导入的 `QMouseEvent` |
| AUD-2111 | Suggestion | risk | `src/app/controllers/ai_edit_host_bridge_impl.py:311-323` | medium | `get_capability_manifest` 在 UI 线程访问系统 keyring（`get_secret`） |
| AUD-21110 | Suggestion | risk | `src/features/edit/ui/timeline_items.py:1668–1670（置 0.5 在 1582–1583）` | medium | 禁用/锁定片段的透明度被提前重置，名称条与波形未呈现半透明 |
| AUD-21111 | Suggestion | process | `src/features/edit/ui/timeline_items.py:10257（Gap）、6103（Transition hatch）` | high | GapIndicatorItem/TransitionItem 纹理绘制按整宽迭代，未按 exposedRect 裁剪 |
| AUD-21112 | Suggestion | process | `src/features/edit/ui/timeline_items.py:7936–7948` | medium | 音量拖拽提交失败无回滚，header 视觉与模型可能脱节 |
| AUD-21113 | Suggestion | process | `src/features/edit/ui/timeline_items.py:1734–1735、3365` | high | paint 路径内的函数级导入与不稳定 hash 占位波形 |
| AUD-2112 | Suggestion | process | `src/app/controllers/ai_edit_host_bridge_impl.py:784-789` | medium | `_resolve_timeline_view` 忽略 `timeline_id` 参数，恒返回当前 view |
| AUD-2113 | Suggestion | process | `src/app/controllers/ai_edit_host_bridge_impl.py:807-832` | medium | `get_selected_asset_ids` 语义重载（时间线音频选中覆盖媒体池选中）且异常路径零日志 |
| AUD-2114 | Suggestion | process | `src/app/controllers/ai_edit_host_bridge_impl.py:33（未使用导入）、:249（无协议基类）、:1599（` | medium | 实现类未声明实现 `AiEditHostBridge`，导入未使用；`check_ai_action` 返回注解弱化为 Any |
| AUD-21200 | Major | bug | `src/features/edit/ui/timeline_layout.py:338-348` | medium | 存在字幕轨道时 video_scroll_offset 被完全忽略，溢出场景下视频滚动条变死条、轨道绘制进标尺区 |
| AUD-21201 | Major | risk | `src/features/edit/ui/timeline_layout.py:113-124` | medium | TimelineLayoutState.from_dict 不校验外部持久化数据，损坏的工程文件可在加载时崩溃、越界比率直接破坏布局 |
| AUD-21202 | Suggestion | process | `src/features/edit/ui/timeline_layout.py:611-615` | medium | subtitle_area_ratio 为死状态：被写入、持久化、参与重配置签名，但布局计算从不读取 |
| AUD-21203 | Suggestion | doc-gap | `src/features/edit/ui/timeline_layout.py:705-721` | medium | resolve_timeline_hit 未检查 S/V 分隔条，且实现顺序与文档化优先级矛盾 |
| AUD-21204 | Suggestion | risk | `src/features/edit/ui/timeline_layout.py:212-217, 568-570` | medium | set_pixels_per_frame/configure 不拒绝 pixels_per_frame ≤ 0，x_to_frame 将除零崩溃 |
| AUD-21205 | Suggestion | process | `src/features/edit/ui/timeline_layout.py:471-478` | medium | track_at_y 在矩形循环内反复重建 track_id 集合，鼠标移动热路径 O(N×M) |
| AUD-21206 | Suggestion | process | `src/features/edit/ui/timeline_layout.py:194-207` | medium | configure() 就地修改调用方 TrackInfo 并持有外部可变引用，绕过 _dirty 缓存的风险由调用方承担 |
| AUD-21207 | Suggestion | process | `src/features/edit/ui/timeline_layout.py:693, 102, 114` | high | clip_id_at 注解 "callable \| None" 不是合法类型；多处 dict 注解未参数化 |
| AUD-21208 | Suggestion | process | `src/features/edit/ui/timeline_layout.py:773` | medium | resolve_drop_target 将一切未知 media_type 静默映射为 audio |
| AUD-21301 | Major | bug | `src/features/edit/ui/timeline_panel.py:1545` | medium | 键盘切换工具时 Rate Stretch 按钮的互斥选中状态不同步 |
| AUD-21302 | Major | bug | `src/features/edit/ui/timeline_panel.py:1376` | medium | `_on_timer_frame_ready` 缺少隐藏视图守卫，跨页（Color 页）播放时仍驱动隐藏的 Edit 时间线视图与背压 reanchor |
| AUD-21303 | Major | risk | `src/features/edit/ui/timeline_panel.py:2609` | medium | `TimecodeInputDialog` 在 fps∈(0, 0.5) 时整除零导致崩溃 |
| AUD-21304 | Suggestion | risk | `src/features/edit/ui/timeline_panel.py:1650` | medium | `_zoom_updating` 防递归锁多处未用 try/finally，异常后滑块缩放被永久禁用 |
| AUD-21305 | Suggestion | process | `src/features/edit/ui/timeline_panel.py:1402` | medium | 多处裸 `except Exception: pass` 静默吞错且无日志 |
| AUD-21306 | Suggestion | risk | `src/features/edit/ui/timeline_panel.py:2152` | medium | HUD 刷新时在 UI 线程对最多 200 个素材同步做磁盘代理状态校验 |
| AUD-21307 | Suggestion | process | `src/features/edit/ui/timeline_panel.py:898` | high | 个别公开/私有方法参数与容器类型注解缺失 |
| AUD-21308 | Suggestion | process | `src/features/edit/ui/timeline_shortcuts.py:114` | medium | `matches_key_event` 两处裸 `except Exception` 静默吞掉绑定读取/解析错误 |
| AUD-21309 | Suggestion | process | `src/features/edit/ui/timeline_shortcuts.py:92` | high | `_normalize_modifiers` 参数无类型注解 |
| AUD-21400 | Major | risk | `src/features/edit/ui/timeline_view.py:2020、6064、15556` | medium | 间隙指示器（Phase 3.3）在全部生产调用点被 `_updating` 锁短路，功能完全失效 |
| AUD-21401 | Major | bug | `src/features/edit/ui/timeline_view.py:6033-6037` | medium | 增量更新中 `set_sync_locked` 被嵌套在 `is_locked` 分支内，sync-lock 单独变化时表头仍失步 |
| AUD-21402 | Major | bug | `src/features/edit/ui/timeline_view.py:6795-6797、7367-7368、7948-7949、8128-8129、8181-8182、8360-8361、9036-9037、9101-9103、10974-10981` | medium | 常规片段拖拽/修剪的"排除自身"吸附从未传给 SnapManager，旧版排除点集合被整体旁路 |
| AUD-21403 | Major | risk | `src/features/edit/ui/timeline_view.py:2733-2742` | medium | `_debounce_thumbnail_schedule` 每次调用新建 QTimer 且旧对象不释放，滚动/缩放期间持续累积泄漏 |
| AUD-21404 | Major | bug | `src/features/edit/ui/timeline_view.py:7815-7857、7913-7924` | medium | Alt 拖拽复制 solid 素材时，命令失败路径已在项目里留下孤儿 asset 且不广播 project_changed |
| AUD-21405 | Suggestion | process | `src/features/edit/ui/timeline_view.py:9887-9954、9956-10005` | high | `add_transition_at_cut_point`/`add_transition_at_clip_edge` 返回注解 `-> None` 与实际返回 str(错误) 矛盾 |
| AUD-21406 | Suggestion | process | `src/features/edit/ui/timeline_view.py:3064、3619-3620、3629-3633、11875-11879、11900-11906` | medium | 多处用户可见字符串硬编码英文，绕过 I18N 工具 |
| AUD-21407 | Suggestion | process | `src/features/edit/ui/timeline_view.py:3948-3949、8031-8032、8052-8053、8078-8079` | medium | 多处宽泛 `except Exception` 静默吞掉领域错误，违反 .clinerules 红线 |
| AUD-21408 | Suggestion | risk | `src/features/edit/ui/timeline_view.py:3966-3970` | high | 锁轨数值 trim 的用户反馈走 `getattr(self,'status_bar')` 死路径，提示永不显示 |
| AUD-21409 | Suggestion | process | `src/features/edit/ui/timeline_view.py:147、154` | high | `TimelineChanges` 的 `track_changes`/`subtitle_track_changes` 仍是可变 dict 默认值，与 BUG-07 修复声明不一致 |
| AUD-21410 | Suggestion | perf-opportunity | `src/features/edit/ui/timeline_view.py:1184、1305、1506-1526` | medium | `_layout_track_items` 在每轨道循环内重复调用 `all_ordered_tracks()`（O(n²)），且三个分区滚动条每个 tick 都触发全量重排 |
| AUD-21411 | Suggestion | process | `src/features/edit/ui/timeline_view.py:11947（` | medium | 若干方法缺失或错误的类型注解 |
| AUD-21412 | Suggestion | risk | `src/features/edit/ui/timeline_view.py:13783-13792` | medium | 外部文件拖放在 UI 线程逐个同步探测媒体元数据，多文件时阻塞交互 |
| AUD-21500 | Major | risk | `src/features/edit/ui/trim_numeric_dialog.py:87-89, 109-115` | medium | `_refresh` 程序化修改 SpinBox 上下限可经 `valueChanged` 重入自身，无状态锁 |
| AUD-21501 | Suggestion | process | `src/features/edit/ui/trim_numeric_dialog.py:113-118` | medium | 合法范围标签显示未排序的 low/high，与 SpinBox 实际范围（sorted）可能不一致 |
| AUD-21502 | Suggestion | process | `src/features/edit/ui/trim_numeric_dialog.py:124-128` | medium | 结果时长用 `max(1, new_duration)` 掩盖非法值而非暴露 |
| AUD-21503 | Major | bug | `src/features/edit/ui/tts_settings_dialog.py:150, 180；src/shared/export/export_panel.py:260-279` | medium | 对 `QTextEdit` 调用 `QLineEdit` 专用样式函数，样式完全不生效（静默失效） |
| AUD-21504 | Major | bug | `src/features/edit/ui/tts_settings_dialog.py:276-287` | medium | 参考音频路径仅做非空校验，不验证文件存在性（外部数据未验证） |
| AUD-21505 | Suggestion | process | `src/features/edit/ui/tts_settings_dialog.py:280-286` | high | 校验失败静默 return，用户无任何提示 |
| AUD-21506 | Suggestion | process | `src/features/edit/ui/tts_settings_dialog.py:41-45` | high | 跨模块导入 export_panel 的私有下划线符号 |
| AUD-21507 | Suggestion | process | `src/features/edit/ui/tts_settings_dialog.py:133-140, 191-198, 295-298` | medium | 两个语言下拉（内置音色 tab / 音色克隆 tab）互不同步，另一 tab 的选择被静默丢弃 |
| AUD-21508 | Suggestion | process | `src/features/edit/ui/tts_settings_dialog.py:56-58` | high | 构造参数 `parent` 类型注解为 `Any` |
| AUD-21600 | Suggestion | process | `src/products/__init__.py:160-165` | medium | `TOPOS_DISABLE_FEATURES` 中的未知特性名被静默忽略，与 `get_current_product()` 的告警行为不一致 |
| AUD-21601 | Major | bug | `src/products/__init__.py:146-167（` | medium | 与 `src/shared/feature_flags.py` 双轨解析同一环境变量，语义已实际分叉 |
| AUD-21602 | Suggestion | process | `src/products/__init__.py:18` | high | `from typing import FrozenSet` 未使用 |
| AUD-21603 | Suggestion | process | `src/products/__init__.py:52-53` | high | dataclass 字段用未参数化的裸 `frozenset` 标注，与本文件 `dict[str, ProductPreset]` 风格不一致 |
| AUD-21604 | Suggestion | process | `src/products/packaging.py:95` | high | `_PLUGIN_MAP.get(product_name, [])` 静默兜底，新增产品漏配映射时不报错 |
| AUD-21605 | Suggestion | doc-gap | `src/products/packaging.py:66（docstring）与 91-93（实现）` | medium | `include_locales` 实际返回特性 ID 而非文档所述的“locale 文件前缀” |
| AUD-21801 | Suggestion | process | `src/features/__init__.py:1-3` | medium | 包入口 eager import ColorFacade，任何 `src.features.*` 导入都会拉起 color 特性链 |
| AUD-21900 | Suggestion | doc-gap | `src/features/ai_harness/__init__.py:6-17` | medium | 模块 docstring 仍停留在 P0 阶段描述，与实际导出内容严重脱节 |
| AUD-21901 | Major | bug | `src/features/ai_harness/analysis_skills.py:301-339（配合 342-352）` | medium | 转录路由追踪把"最终"质量错误归因到中间步骤，Qwen/Demucs 步的 quality 与 note 失真 |
| AUD-21902 | Suggestion | process | `src/features/ai_harness/analysis_skills.py:38-39` | high | 未使用的导入 `field`、`Callable` |
| AUD-21903 | Suggestion | process | `src/features/ai_harness/editorial_skills.py:378-391` | medium | build_rough_cut_plan 用 `replace()` 修改已入库 plan，同 plan_id 出现两个版本 |
| AUD-21904 | Suggestion | process | `src/features/ai_harness/editorial_skills.py:27-30` | high | 未使用的导入 `field`、`Iterable`、`FrameRange` |
| AUD-21905 | Major | bug | `src/features/ai_harness/generation_adapter.py:259-278（唯一传播点）、282-300、363-369` | medium | run 取消在 invoke 返回后不再传播到生成 job；用户取消后 run 仍可能终态 SUCCEEDED |
| AUD-21906 | Major | bug | `src/features/ai_harness/generation_adapter.py:318-321、363-369` | medium | job 的 cancelled 终态被一律映射为 FAILED/GENERATION_JOB_FAILED |
| AUD-21907 | Major | bug | `src/features/ai_harness/generation_adapter.py:97` | medium | reference_asset_ids 未验证类型，字符串输入被 `tuple()` 拆成单字符 id |
| AUD-21908 | Suggestion | process | `src/features/ai_harness/generation_adapter.py:71-75` | high | 无效 modality 静默回退 IMAGE，不产生任何诊断 |
| AUD-21909 | Suggestion | process | `src/features/ai_harness/generation_adapter.py:372-383` | high | _missing_consent_fields 与 GenerationConsent.is_complete 口径不一致 |
| AUD-21910 | Suggestion | process | `src/features/ai_harness/generation_adapter.py:85-86、313-333` | medium | retry 复用同一 invocation_id 作 request_id，历史候选被重复记入新 run |
| AUD-21911 | Suggestion | risk | `src/features/ai_harness/generation_adapter.py:271-278、358` | medium | worker 线程回调调用无锁的 JobRunBridge.sync，依赖 GIL 单操作原子性 |
| AUD-21912 | Suggestion | process | `src/features/ai_harness/help_assistant.py:177-181` | high | build_help_assistant 宽泛 except 且完全不记日志 |
| AUD-21913 | Suggestion | process | `src/features/ai_harness/help_assistant.py:129-133` | high | entry_ids_for_query 不拦空查询，search 空查询返回全部条目 |
| AUD-21914 | Suggestion | process | `src/features/ai_harness/invocation_models.py:683-691` | high | sanitize_for_storage 的 secret 替换 lambda 含死代码分支 |
| AUD-21915 | Suggestion | process | `src/features/ai_harness/invocation_models.py:405` | high | Invocation 默认 scope 使用魔法 project_id="default"，绕过跨工程校验 |
| AUD-21916 | Suggestion | process | `src/features/ai_harness/invocation_models.py:282` | medium | from_dict 对字符串型列表字段逐字符拆分（selected_asset_ids） |
| AUD-21917 | Suggestion | process | `src/features/ai_harness/invocation_models.py:32` | high | 未使用的导入 `Callable` |
| AUD-2200 | Major | bug | `src/app/controllers/ai_editing_mixin.py:1059（MCP host 赋值点 676-679）` | medium | 装配失败的外层 except 不停用已启动的 MCP host / tool facade / providers |
| AUD-22000 | Suggestion | process | `src/features/ai_harness/invocation_router.py:329-339` | medium | skill_resolver 抛异常时静默放行（fail-open），未注册 Skill 被当作有效 |
| AUD-22001 | Suggestion | process | `src/features/ai_harness/invocation_router.py:347-371（另见 168-173 的 _CLARIFY_QUESTIONS）` | medium | 澄清槽位检查只实现 target_language/target_duration，platform/voice_style 模板不可达；language 二次提取是死代码 |
| AUD-22002 | Suggestion | process | `src/features/ai_harness/invocation_router.py:223（_DURATION_RE）与 237-244` | high | 时长正则不识别一位数秒、五位以上秒被静默截断错解 |
| AUD-22003 | Suggestion | process | `src/features/ai_harness/invocation_router.py:304、344` | high | prefilled_inputs 的值未做类型/形状验证即并入 invocation inputs |
| AUD-22004 | Suggestion | process | `src/features/ai_harness/invocation_router.py:479-489` | high | _extract_placement 函数内重复 import re；英文 "start" 子串匹配会误命中 |
| AUD-22005 | Major | bug | `src/features/ai_harness/legacy_adapters.py:368-379（同型问题 249-261）` | medium | DRAFT 态调用 finish(success=False) 是非法迁移，SKILL_INPUT_FAILED 路径的 run 永久停在 draft |
| AUD-22006 | Major | doc-gap | `src/features/ai_harness/legacy_adapters.py:239-247（文档在 101-105）` | medium | _should_route_workflow 未实现文档承诺的 "source 不是 developer" 条件 |
| AUD-22007 | Major | bug | `src/features/ai_harness/legacy_adapters.py:330-339、636-639` | medium | 结果归档路径（repo.get_plan / repo.store）不在异常保护内，抛错会让 run 卡死在 RUNNING |
| AUD-22008 | Major | risk | `src/features/ai_harness/legacy_adapters.py:567-579、581-583` | medium | _editorial_analyses 实例属性跨 run 不清理，转录追踪会污染后续 run；亦非线程安全 |
| AUD-22009 | Suggestion | process | `src/features/ai_harness/legacy_adapters.py:26-32` | high | 未使用的导入 InvocationSource / PermissionLevel / RunStatus |
| AUD-2201 | Suggestion | process | `src/app/controllers/ai_editing_mixin.py:331-335` | high | KillSwitch（自主编辑急停）构造失败被完全静默吞掉，无任何日志 |
| AUD-22010 | Suggestion | process | `src/features/ai_harness/llm_role_executor.py:390-402` | medium | 错误/超时响应在计量前返回，token/成本台账系统性低估 |
| AUD-22011 | Suggestion | process | `src/features/ai_harness/llm_role_executor.py:189（同型 227、254、281）` | high | typed artifact ID 用秒级时间戳生成，同秒内可碰撞 |
| AUD-22012 | Suggestion | process | `src/features/ai_harness/llm_role_executor.py:442-452` | medium | execute_with_budget 临时改写共享 role_budgets 字典，不可重入 |
| AUD-22013 | Suggestion | process | `src/features/ai_harness/llm_role_executor.py:248` | medium | 模型输出的数值字段未做范围校验（confidence 可为任意 float） |
| AUD-22014 | Major | bug | `src/features/ai_harness/llm_role_model_client.py:40-51` | medium | timeout_ms 参数被静默丢弃，角色超时预算完全不生效 |
| AUD-22015 | Suggestion | process | `src/features/ai_harness/llm_role_model_client.py:58-61` | medium | finish_reason 硬编码 "stop"，截断对上层不可见 |
| AUD-22016 | Suggestion | process | `src/features/ai_harness/llm_role_model_client.py:55-57` | medium | 读取共享 last_usage 属性取成本，客户端并发复用时归属错乱 |
| AUD-22017 | Major | bug | `src/features/ai_harness/mcp_http_transport.py:242-249（约束定义在 106-109）` | medium | urllib 默认跟随重定向且保留 Authorization 头，Bearer 凭据可被引到任意主机（含 https→http 降级，绕过 localhost-only 约束） |
| AUD-22018 | Suggestion | process | `src/features/ai_harness/mcp_http_transport.py:248、251` | high | HTTP 响应体读取无大小上限，恶意服务器可造成内存耗尽 |
| AUD-22019 | Suggestion | process | `src/features/ai_harness/mcp_http_transport.py:164` | medium | 服务器返回 "error": null 时 404 探测分支抛 AttributeError |
| AUD-2202 | Suggestion | process | `src/app/controllers/ai_editing_mixin.py:1583-1589` | high | teardown 中 `EditSession.unbind()` 用裸 `except Exception: pass` 且无日志 |
| AUD-22020 | Critical | bug | `src/features/ai_harness/mcp_outbound.py:369-396` | medium | 写审批闸门使用晚发现 manifest 前计算的 is_write，注解标记的写工具可在无审批下执行 |
| AUD-22021 | Major | bug | `src/features/ai_harness/mcp_outbound.py:378-396（结合 353-360 的前置校验）` | medium | discovery 失败后放行未校验的工具调用（fail-open） |
| AUD-22022 | Suggestion | risk | `src/features/ai_harness/mcp_outbound.py:129-137、277-279、551-560、417-421` | medium | registry / auth log / transport 缓存 / 请求 id 均无锁保护，多线程调用会竞态 |
| AUD-2203 | Suggestion | process | `src/app/controllers/ai_editing_mixin.py:690-694、828-836（adapter 构造在 build_p1_dependencies 内 line 135）` | medium | shared_runtime 解析失败为 None 时仍构造 `LegacyHarnessAdapter(service, None)`，故障被推迟到首次运行 |
| AUD-2204 | Suggestion | process | `src/app/controllers/ai_editing_mixin.py:1760-1766` | high | `_all_analysis_fresh` 中 `isinstance(aid, tuple)` 分支两臂完全相同（死代码） |
| AUD-2205 | Suggestion | perf-opportunity | `src/app/controllers/ai_editing_mixin.py:1681-1693、1706-1710` | medium | `bind_comfy_workflow`/`test_comfy_connection` 在宿主方法内同步阻塞 HTTP（默认 30s 超时） |
| AUD-2206 | Suggestion | process | `src/app/controllers/ai_editing_mixin.py:786-787、1279、1306-1309、1560-1561、1680、1704、382` | medium | 大量跨类/跨模块私有属性访问（`_empty_capabilities`、`_server`、`_svc`、`_catalog`、`_refresh_history_menu`、`_timeline`） |
| AUD-2207 | Suggestion | process | `src/app/controllers/ai_editing_mixin.py:172、1813-1835` | high | 类型注解缺失/退化：`_SessionHostShim` 多方法无返回注解，闭包返回裸 `list` |
| AUD-2208 | Suggestion | process | `src/app/controllers/ai_editing_mixin.py:178-184` | medium | `make_editorial_provider` 的闭包吞掉 AnalysisService 所有异常并返回空证据，无日志 |
| AUD-2209 | Critical | bug | `src/app/controllers/ai_workflow_composite_render.py:263-269（调用点 238-239、quality 选择 254-260）` | medium | `_write_png` 把 scene-linear 帧直接量化为 8bit PNG，缺少 sRGB/显示变换，产出的合成渲染图系统性偏暗 |
| AUD-2210 | Major | bug | `src/app/controllers/ai_workflow_composite_render.py:250-260` | medium | `_render_single_frame` 每次新建 `CompositeFrameRenderer` 但从不调用 `cleanup()`，GL context/纹理池依赖 GC 释放 |
| AUD-22101 | Major | risk | `src/features/ai_harness/mcp_protocol.py:159-174` | medium | 不可信 JSON Schema 可导致校验线程资源耗尽（组合爆炸 + 正则回溯） |
| AUD-22102 | Major | risk | `src/features/ai_harness/mcp_stdio_transport.py:183-196, 126` | medium | close() 后立即 start() 时，旧 stdout 泄放线程可能向新队列投毒 None 哨兵 |
| AUD-22103 | Major | perf-opportunity | `src/features/ai_harness/mcp_stdio_transport.py:202-220, 255-276` | medium | send_request 全程持有 RLock，close() 最长阻塞 request_timeout（默认 180s） |
| AUD-22104 | Major | risk | `src/features/ai_harness/mcp_stdio_transport.py:189-192, 270` | medium | `_stdout_queue` 无界，空闲期间 server 通知可无限累积 |
| AUD-22105 | Major | risk | `src/features/ai_harness/plugin_isolation.py:515-549` | medium | context.deadline 为 None（默认策略）且无取消源时，invoke 等待循环无任何看门狗，可永久占锁 |
| AUD-22106 | Suggestion | process | `src/features/ai_harness/mcp_protocol.py:269-274` | high | SlidingWindowRateLimiter.allow 为非原子的 check-then-append，并发下限流可被轻微突破 |
| AUD-22107 | Suggestion | process | `src/features/ai_harness/mcp_protocol.py:146-149` | high | enum 校验中 bool 与 int 相等（True == 1）偏离 JSON Schema 语义 |
| AUD-22108 | Suggestion | process | `src/features/ai_harness/mcp_stdio_transport.py:281-283` | medium | server 发起的 JSON-RPC 请求被静默丢弃，未回 MethodNotFound |
| AUD-22109 | Suggestion | process | `src/features/ai_harness/mcp_stdio_transport.py:73, 126-134` | high | 重启后 `_stderr_lines` 不清空，诊断信息混入上一进程内容 |
| AUD-2211 | Major | risk | `src/app/controllers/ai_workflow_composite_render.py:156-162（模块契约声明见 5-13 行 docstring）` | medium | worker 线程 provider 活读 `main_window._current_edit_project/_current_timeline_sequences`，违反本模块自述的"worker 只消费 dict 快照"红线 |
| AUD-22110 | Suggestion | process | `src/features/ai_harness/plugin_broker.py:387-389, 447-448` | high | `PathGrant.resolved_root` 每次检查都重新 expanduser+resolve（每次 I/O 多次系统调用） |
| AUD-22111 | Suggestion | process | `src/features/ai_harness/plugin_broker.py:295-296` | medium | HTTPError 响应体未关闭，连接/FD 滞留至 GC |
| AUD-22112 | Suggestion | process | `src/features/ai_harness/plugin_discovery.py:133-145, 174-186` | medium | VERIFIED_THIRD_PARTY 分支中的二次哈希校验为不可达死代码 |
| AUD-22113 | Suggestion | process | `src/features/ai_harness/plugin_isolation.py:77-99` | medium | 子进程 `_ProcessRPC.request` 不使用 remaining_seconds 本地超时，host 停滞时子进程无限等待 |
| AUD-22114 | Suggestion | process | `src/features/ai_harness/plugin_isolation.py:569-575, 323-331` | medium | 普通插件异常被统一映射为 PluginInvocationProcessCrashed，误导诊断分类 |
| AUD-22115 | Suggestion | process | `src/features/ai_harness/plugin_persistence.py:191-197` | medium | `restore` 仅精确匹配 "disabled" 才禁用，与 `restore_project_state` 的规范化处理不一致 |
| AUD-22116 | Suggestion | process | `src/features/ai_harness/plugin_persistence.py:409-415, 472-477` | medium | journal 快照的 to_dict/rollback 为浅拷贝，调用方改动嵌套结构会污染检查点 |
| AUD-2212 | Major | risk | `src/app/controllers/ai_workflow_composite_render.py:314（bin 实现 generation_bin.py:154-159，读侧 generation_bin.py:126-146）` | medium | `gen_bin.add_candidates` 在 worker 线程无锁写入，GUI 线程同时迭代 GenerationBin 可触发 RuntimeError |
| AUD-2213 | Major | bug | `src/app/controllers/ai_workflow_composite_render.py:135-181、341-351` | medium | provider 整体无异常防护：`_resolve_output_resolution` 的 int() 与渲染异常会逃出"失败返回 None"契约 |
| AUD-2214 | Suggestion | risk | `src/app/controllers/ai_workflow_composite_render.py:35-36、216、237` | high | `composite_renders` 缓存目录无任何清理/淘汰策略，产物无限累积 |
| AUD-2215 | Suggestion | process | `src/app/controllers/ai_workflow_composite_render.py:318-321` | high | `_generation_bin` 穿透 `bridge._generation_service` 私有属性取 bin |
| AUD-2216 | Suggestion | process | `src/app/controllers/ai_workflow_composite_render.py:64-68（对比 74-78）、243-248` | medium | 显式 composite id 路径的 `to_dict()` 未包异常（与活动图路径的降级处理不一致）；`_render_single_frame` 的 `render_output` 参数未使用 |
| AUD-22200 | Suggestion | risk | `src/features/ai_harness/plugin_rollout.py:239-243（另见 245-246、260-272）` | medium | PluginRolloutRouter 完全无锁，register/remove 与 target_for_new_run/to_dict 存在 check-then-act 与迭代竞争 |
| AUD-22201 | Major | bug | `src/features/ai_harness/plugin_runtime.py:241（metadata）、193-195（input_schema/output_schema/metadata）、265-271 与 277-284` | medium | PluginManifest/PluginCapability 持有的 Mapping 字段未做防御性拷贝，frozen 契约与 content_hash 完整性可被注册后突变破坏 |
| AUD-22202 | Major | risk | `src/features/ai_harness/plugin_runtime.py:511-519、529-540、545-552` | medium | activate() 与 register(replace_existing=True) 竞态：激活窗口内条目可被替换，新条目被标记 ACTIVE 但从未执行其 activate，且 host_io/policy 绑定来自旧条目 |
| AUD-22203 | Suggestion | process | `src/features/ai_harness/plugin_runtime.py:955（对照 745-749、961-965）` | medium | invoke_tool 成功路径原样返回插件 Mapping、未统一注入 ok=True，与错误路径恒含 ok=False 的契约不对称 |
| AUD-22204 | Suggestion | process | `src/features/ai_harness/plugin_runtime.py:840-849、966-967（对照 716-725）` | medium | in_process 旧式（不接受 context 的）handler 无超时看门狗，卡死调用永久占用 BoundedSemaphore，插件持续 PLUGIN_BUSY 直至重启 |
| AUD-22205 | Major | bug | `src/features/ai_harness/plugin_sandbox.py:216-223` | medium | 沙箱 worker 继承宿主完整 os.environ，凭据对第三方插件代码可读，违背"插件不得隐式获得凭据"边界 |
| AUD-22206 | Suggestion | process | `src/features/ai_harness/plugin_sandbox.py:117-126` | medium | 沙箱子进程 stdout/stderr 均 DEVNULL，worker 启动失败（退出码 2/3/4 或异常 traceback）只表现为连接 EOF，无法诊断 |
| AUD-22207 | Suggestion | process | `src/features/ai_harness/plugin_status.py:354（对照 src/features/ai_harness/plugin_runtime.py:984-993）` | medium | can_enable 仅对 DISABLED 为真，而 registry.enable() 允许 FAILED/DEGRADED 状态回 ENABLED，UI 将不提供失败插件的启用入口 |
| AUD-22208 | Suggestion | process | `src/features/ai_harness/plugin_trace.py:56-59` | medium | 会话解析器抛错被宽泛 except 静默降级为 "default" 会话，trace 事件可能被错误归属且无任何诊断 |
| AUD-22209 | Suggestion | process | `src/features/ai_harness/plugin_trust.py:59-62` | high | HmacPluginSignatureVerifier 构造对非 str/bytes 键执行 bytes(value)，整数会静默生成全零密钥 |
| AUD-22210 | Major | bug | `src/features/ai_harness/result_card.py:190-200（对照 8-9、139 行红线声明）` | medium | generated_media 未审批即启用 import_to_pool/place_on_timeline，违反模块自述红线"审批前所有应用/导出类按钮禁用" |
| AUD-22211 | Suggestion | process | `src/features/ai_harness/result_card.py:164-169` | high | preview_diff 分支仅在 not applied 时追加，其 enabled=not applied 与 disabled_reason 的 applied 分支均为死代码 |
| AUD-22300 | Suggestion | process | `src/features/ai_harness/result_card_builder.py:137-144` | medium | resolver 失败的兜底结果卡固定 trust="trusted"，与"trust 只降级不升级"红线存在张力 |
| AUD-22301 | Suggestion | process | `src/features/ai_harness/result_card_builder.py:247-253` | medium | _evidence_refs_and_trust 对非数值 untrusted_count 直接比较，类型异常会让成功 run 渲染成错误卡 |
| AUD-22302 | Suggestion | process | `src/features/ai_harness/rollout_metrics.py:128` | high | quality_gate_failures 用 "gate" 子串启发式匹配，investigate/propagate 等诊断码会被误计 |
| AUD-22303 | Suggestion | process | `src/features/ai_harness/run_trace.py:351-364` | medium | project() 把会话级 dropped 计数当作单 run 的截断数，误报 trace_truncated |
| AUD-22304 | Suggestion | process | `src/features/ai_harness/run_trace.py:411-416` | high | fork_session 不继承源会话 dropped 计数，截断历史被当作完整历史分发 |
| AUD-22305 | Suggestion | process | `src/features/ai_harness/run_trace.py:617-636` | medium | project_trace_events 的 tool call key 在 started/finished 两侧推导不对称，无标识事件会永久挂起 pending_tool_calls |
| AUD-22306 | Suggestion | process | `src/features/ai_harness/run_trace.py:366-372` | medium | project_session 对每个 run 各自全量扫描并折叠整个会话事件，O(runs×events) |
| AUD-22307 | Suggestion | process | `src/features/ai_harness/run_trace.py:529-539` | medium | clear() 不清理 _next_seq，清理语义自相矛盾 |
| AUD-22308 | Suggestion | process | `src/features/ai_harness/run_trace.py:304-311` | high | append() 分发 subscriber 时异常被静默吞掉且无任何日志 |
| AUD-22309 | Major | bug | `src/features/ai_harness/runtime.py:538-562` | medium | finish() 在迁移校验通过前就写入 finished_at / error 字段，失败路径留下脏状态并可能发出伪状态事件 |
| AUD-22310 | Major | risk | `src/features/ai_harness/runtime.py:332-343` | medium | start() 在锁外读取 _detached_sessions，与 detach_session 存在竞态，可产生漏标 detached 的 run |
| AUD-22311 | Major | bug | `src/features/ai_harness/runtime.py:361-377` | medium | cancel() 不校验 run 状态，允许对已终态 run 置 cancel token 并发布 run.cancel_requested |
| AUD-22312 | Suggestion | doc-gap | `src/features/ai_harness/runtime.py:735-743` | medium | _emit/_emit_node 在锁外向 state.events 追加，事件流不受类文档声明的 RLock 保护 |
| AUD-22313 | Suggestion | risk | `src/features/ai_harness/runtime.py:425-428` | high | retry() 中的空锁块为死代码 |
| AUD-22314 | Suggestion | process | `src/features/ai_harness/runtime.py:701-724` | medium | detach 后无任何 re-attach 路径，_detached_sessions 只增不减 |
| AUD-22315 | Major | bug | `src/features/ai_harness/runtime_invariants.py:95-96（配合 agent_orchestrator.py:684-689）` | medium | strict 模式抛出的 RuntimeError 会被 orchestrator 的订阅分发吞掉，strict 经 install() 永不生效 |
| AUD-22316 | Suggestion | process | `src/features/ai_harness/runtime_invariants.py:120-135` | medium | check_run 全量回放时 `events[:-1]` 语义错位，终态之后还有事件的 run 会对早期合法 retry 误报 RETRY_AFTER_TERMINAL |
| AUD-22317 | Suggestion | risk | `src/features/ai_harness/runtime_invariants.py:90-96` | medium | _on_event 的 _seen/_violations 无锁，订阅回调跨线程时同一违例可被重复记录 |
| AUD-22318 | Suggestion | perf-opportunity | `src/features/ai_harness/runtime_invariants.py:84-96` | high | 每个事件回放全量历史做 any() 扫描，整 run 累计 O(n²) |
| AUD-22319 | Suggestion | risk | `src/features/ai_harness/scheduler_adapter.py:136-183` | medium | JobRunBridge 的 _bindings/_job_to_runs 无并发保护，Job 回调线程与 UI 线程并发时可丢绑定或迭代崩溃 |
| AUD-22320 | Suggestion | process | `src/features/ai_harness/scheduler_adapter.py:37-39, 136-145` | medium | 每个 run 仅存单一 _registry，与模块自述"一个 run 可能挂多个 Job（分析 + 生成 + 渲染）"矛盾，跨 registry 的 Job 取消被静默跳过 |
| AUD-22321 | Suggestion | risk | `src/features/ai_harness/session_fork.py:74-94` | medium | SessionEventLog 无任何锁保护，append 的 seq 计算存在 check-then-act 竞态 |
| AUD-22322 | Major | bug | `src/features/ai_harness/skill_catalog.py:74-76` | medium | brief_template / input_schema 数值未经类型校验直接 int()/float()，畸形 manifest 会炸掉整个目录渲染 |
| AUD-22323 | Suggestion | process | `src/features/ai_harness/skill_catalog.py:360-370` | medium | _merge_p2_meta 宽泛 except + pass 且无日志，静默掩盖 editorial_skills 导入/合并错误 |
| AUD-22324 | Suggestion | process | `src/features/ai_harness/skill_catalog.py:66, 120, 138` | medium | ui_card.form_fields / labels / enum 对非预期形状无防御，字符串被拆成字符级垃圾字段 |
| AUD-22400 | Suggestion | process | `src/features/ai_harness/skill_compiler.py:184` | high | taskgraph 输入映射用宽泛 `except Exception`，掩盖非领域异常 |
| AUD-22401 | Suggestion | process | `src/features/ai_harness/skill_compiler.py:248` | high | 必填输入校验不覆盖空容器（`[]`/`{}`） |
| AUD-22402 | Major | doc-gap | `src/features/ai_harness/skill_dry_run.py:320（赋值）；476 / 526-540（实际分派绕开它）` | medium | 注入的 `fake_executor` 从未被调用，文档宣称的可注入点失效 |
| AUD-22403 | Major | bug | `src/features/ai_harness/skill_dry_run.py:379-387` | medium | STALE_REVISION 检查只判 `revision >= 0` 哨兵，从不比对草稿 base_revision，真实 stale 场景必然通过 |
| AUD-22404 | Suggestion | process | `src/features/ai_harness/skill_dry_run.py:467-474` | medium | 取消路径不调 `runtime.finish`：终态 CANCELLED 但 `finished_at` 恒为 None，且"partial artifact"承诺未落地 |
| AUD-22405 | Suggestion | process | `src/features/ai_harness/skill_dry_run.py:664-688` | medium | `_check_dag_acyclic` 对重复 node_id 误报环 |
| AUD-22406 | Suggestion | process | `src/features/ai_harness/skill_dry_run.py:160-163（对照 385-387、445-447）` | high | `DryRunCheckCode.ALL` 与实际检查集不一致（缺 REVISION_CURRENT / EVIDENCE_SUFFICIENT） |
| AUD-22407 | Suggestion | process | `src/features/ai_harness/skill_eval_suite.py:337-341` | medium | UPLOAD_REJECTED 判定引用的事件名不在 RunEventKind 词表，检查恒真 |
| AUD-22408 | Suggestion | process | `src/features/ai_harness/skill_eval_suite.py:90-96` | high | `_KIND_EXPECTED_ERROR` 为死代码 |
| AUD-22409 | Major | bug | `src/features/ai_harness/skill_release.py:285-297` | medium | `register_into` 未强制"draft.* 前缀、绝不覆盖 official skill"红线，SkillRegistry 允许同 id 高版本覆盖官方条目 |
| AUD-22410 | Major | bug | `src/features/ai_harness/skill_studio_drafts.py:529` | medium | `new_draft_from_workflow` 的 `produces_plan` 逻辑缺陷：`"timeline_write" in policy` 只查键存在，"forbidden" 也判 True，第二子句恒被短路 |
| AUD-22411 | Major | perf-opportunity | `src/features/ai_harness/skill_studio_drafts.py:548-549（对照 508、674-679）` | medium | 浅拷贝破坏草稿冻结不可变约定：嵌套结构与调用方共享，workflow_hash 可与内容静默脱钩 |
| AUD-22412 | Suggestion | process | `src/features/ai_harness/skill_studio_drafts.py:637-655` | medium | `_input_schema_from_workflow` 的 `required` 永远为空，且给每个输入节点加幻影属性 `_{node_id}` |
| AUD-22413 | Suggestion | risk | `src/features/ai_harness/skill_studio_drafts.py:572-601` | medium | `SkillStudioStore` 无锁、无容量上限，`_registry` 为死字段 |
| AUD-22414 | Major | risk | `src/features/ai_harness/subagent_runtime.py:131-134, 176-178, 221-223` | medium | Supervisor 记账字典只增不删，子代理完成后无清理，长会话无界内存增长 |
| AUD-22415 | Major | risk | `src/features/ai_harness/subagent_runtime.py:208-223` | medium | `continue_child` 轮次上限为跨锁 TOCTOU：并发 follow-up 可同轮次双双通过并并发执行 runner |
| AUD-22416 | Suggestion | process | `src/features/ai_harness/subagent_runtime.py:253-265` | medium | `_emit` 直接调用宿主 event_sink，sink 异常会吞掉已完成的子结果 |
| AUD-22500 | Major | test-gap | `src/features/ai_harness/tests/test_generation_adapter.py:238` | medium | `test_safety_blocked_recorded` 为空转测试，SAFETY_BLOCKED 安全路径零验证 |
| AUD-22501 | Suggestion | process | `src/features/ai_harness/tests/test_analysis_skills.py:149` | medium | `pytest.raises(Exception)` 宽泛断言掩盖领域错误类型 |
| AUD-22502 | Suggestion | process | `src/features/ai_harness/tests/test_analysis_skills.py:162` | high | 模块级辅助函数与测试方法缺类型注解 |
| AUD-22503 | Suggestion | process | `src/features/ai_harness/tests/test_composite_render_skill.py:14` | medium | 缺 `pytestmark = pytest.mark.edit_core`，与同目录兄弟文件不一致 |
| AUD-22504 | Suggestion | process | `src/features/ai_harness/tests/test_composite_render_skill.py:133` | high | 四处直接调用私有方法 `_build_editorial_outcome` |
| AUD-22505 | Suggestion | test-gap | `src/features/ai_harness/tests/test_editorial_skills.py:6` | high | 模块文档声称覆盖「reading_speed 诊断」但无对应测试 |
| AUD-22506 | Suggestion | perf-opportunity | `src/features/ai_harness/tests/test_editorial_skills.py:85` | high | `test_subtitle_op_carries_evidence_ref` 断言在 for 循环体内，可空转通过 |
| AUD-22507 | Suggestion | process | `src/features/ai_harness/tests/test_editorial_skills.py:43` | high | 辅助函数与测试方法缺类型注解 |
| AUD-22508 | Suggestion | process | `src/features/ai_harness/tests/test_generation_adapter.py:97` | medium | 将非法 modality 静默回退 IMAGE 固化为预期行为 |
| AUD-22509 | Suggestion | process | `src/features/ai_harness/tests/test_generation_adapter.py:48` | high | 模块级辅助函数缺类型注解 |
| AUD-22510 | Suggestion | test-gap | `src/features/ai_harness/tests/test_help_assistant.py:38` | high | 通过私有属性 `_entries` 直接装配测试索引 |
| AUD-22600 | Major | test-gap | `src/features/ai_harness/tests/test_legacy_adapters.py:285-361` | medium | 取消 token 透传验收测试空转：adapter 从未被调用，约 50 行死代码 |
| AUD-22601 | Suggestion | process | `src/features/ai_harness/tests/test_legacy_adapters.py:220-229` | high | test_workflow_succeeds_with_variants 未断言任何产出/variants |
| AUD-22602 | Suggestion | test-gap | `src/features/ai_harness/tests/test_legacy_adapters.py:161-173, 191-204` | medium | fixture 构造期异常时 EditSession 全局绑定泄漏到后续测试 |
| AUD-22603 | Suggestion | process | `src/features/ai_harness/tests/test_legacy_adapters.py:50-401（如 :105` | high | 测试与辅助函数普遍缺类型注解 |
| AUD-22604 | Suggestion | process | `src/features/ai_harness/tests/test_llm_role_executor.py:132` | high | 意外的海象表达式 `ContextScope := ContextSlice()` 产生无用变量 |
| AUD-22605 | Suggestion | process | `src/features/ai_harness/tests/test_llm_role_executor.py:122, 140` | high | `list(degradation_counts.keys())[0]` 在空字典时抛 IndexError 而非断言失败 |
| AUD-22606 | Suggestion | process | `src/features/ai_harness/tests/test_llm_role_executor.py:249-255` | medium | secret 透传红线测试只查字段命名，不验证实际数据流 |
| AUD-22607 | Suggestion | process | `src/features/ai_harness/tests/test_llm_role_executor.py:47-109, 118-273` | high | 测试与 fake client 方法缺类型注解 |
| AUD-22608 | Suggestion | test-gap | `src/features/ai_harness/tests/test_llm_role_model_client.py:20-26` | medium | fake client 内部用 `assert` 校验调用契约，失败时被 adapter 的宽异常捕获吞掉，测试失败信息失真 |
| AUD-22609 | Suggestion | process | `src/features/ai_harness/tests/test_mcp_outbound.py:31` | high | `StdioTransport` 导入未使用 |
| AUD-22610 | Suggestion | process | `src/features/ai_harness/tests/test_mcp_outbound.py:255-260` | high | test_discover_caches 未验证"第二次不再调 transport" |
| AUD-22611 | Suggestion | process | `src/features/ai_harness/tests/test_mcp_outbound.py:337-345` | high | test_max_events_cap 未验证保留的是"最近"事件 |
| AUD-22612 | Suggestion | process | `src/features/ai_harness/tests/test_mcp_outbound.py:40-47, 50-69 等` | high | 测试与 `_profile` 辅助缺类型注解 |
| AUD-22613 | Suggestion | process | `src/features/ai_harness/tests/test_p1_editorial_provider.py:26-28` | high | 返回类型注解为字符串前向引用，指向函数内部导入的名称（模块级不可解析） |
| AUD-22614 | Suggestion | process | `src/features/ai_harness/tests/test_p1_editorial_provider.py:70-199` | high | provider fake 与测试函数缺类型注解 |
| AUD-22615 | Suggestion | test-gap | `src/features/ai_harness/tests/test_p1_host_wiring.py:73-84` | medium | skill_resolver 绑定契约测试实际未验证绑定（默认 router 也能通过） |
| AUD-22616 | Suggestion | process | `src/features/ai_harness/tests/test_p1_host_wiring.py:95-114` | medium | "only accepts imported" 命名与覆盖不符：只测正向路径 |
| AUD-22617 | Suggestion | process | `src/features/ai_harness/tests/test_p1_host_wiring.py:32-126, 134-172` | high | 测试方法与 stub 类方法缺类型注解 |
| AUD-22618 | Suggestion | process | `src/features/ai_harness/tests/test_p2_acceptance.py:23` | high | `from dataclasses import replace` 导入未使用 |
| AUD-22619 | Suggestion | process | `src/features/ai_harness/tests/test_p2_acceptance.py:50-117 等` | high | 测试与 fake 构造函数缺类型注解 |
| AUD-22620 | Suggestion | doc-gap | `src/features/ai_harness/tests/test_p2_wiring.py:10（对照 :147-162）` | high | 模块 docstring 与实际断言矛盾（target=workflow vs target=editorial） |
| AUD-22621 | Suggestion | process | `src/features/ai_harness/tests/test_p2_wiring.py:173-323` | high | 测试方法与局部 provider 缺类型注解；方法内重复导入 |
| AUD-22700 | Suggestion | process | `src/features/ai_harness/tests/test_p3_red_lines.py:156（及 48、62、91 等）` | high | 测试方法与辅助函数缺类型注解 |
| AUD-22701 | Suggestion | perf-opportunity | `src/features/ai_harness/tests/test_p4_acceptance.py:273-275` | high | PREVIEW 红线断言被 `if` 包裹，存在空转风险 |
| AUD-22702 | Suggestion | process | `src/features/ai_harness/tests/test_p4_acceptance.py:238-241（另 80、257、272）` | medium | `__import__` 动态导入 ToolPolicy 且函数内重复局部导入 |
| AUD-22703 | Suggestion | process | `src/features/ai_harness/tests/test_p4_acceptance.py:30-56` | medium | 缺 `pytestmark = pytest.mark.edit_core` 标记 |
| AUD-22704 | Major | test-gap | `src/features/ai_harness/tests/test_p4_security.py:1（适用于本批 8 个文件及整个` | medium | 本批全部测试未被任何 CI / 默认 pytest 收集，安全红线测试实际不进 CI |
| AUD-22705 | Major | test-gap | `src/features/ai_harness/tests/test_p4_security.py:164-172（对照文件头 11 行声明）` | medium | "层 4 发布门禁拒绝任意代码"实未测试，且断言守卫可空转 |
| AUD-22706 | Suggestion | doc-gap | `src/features/ai_harness/tests/test_p4_security.py:174-181（对照 skill_release.py:117-126）` | medium | docstring 声称"对未知值视为危险"但未覆盖该回退路径 |
| AUD-22707 | Suggestion | process | `src/features/ai_harness/tests/test_p4_security.py:59-70（对照 test_p4_acceptance.py:350-356）` | high | `_is_error_diag` 与 test_p4_acceptance.py `_is_error` 重复实现 |
| AUD-22708 | Suggestion | process | `src/features/ai_harness/tests/test_p4_security.py:21-42` | medium | 缺 `pytestmark = pytest.mark.edit_core` 标记 |
| AUD-22709 | Suggestion | process | `src/features/ai_harness/tests/test_result_card.py:38-39` | high | `_action` 辅助函数为死代码 |
| AUD-22710 | Suggestion | process | `src/features/ai_harness/tests/test_result_card.py:34-39（及 48、55 等全部 test 方法）` | high | 辅助函数与测试方法缺类型注解 |
| AUD-22711 | Suggestion | process | `src/features/ai_harness/tests/test_result_card_builder.py:341-342` | high | skill_id 可追溯性断言被 `or "r1" in text` 短路，几乎恒真 |
| AUD-22712 | Suggestion | process | `src/features/ai_harness/tests/test_result_card_builder.py:181（另 203、216、231、260；方法如 109）` | high | lambda 赋值（E731 noqa ×5）与 test 方法缺 `-> None` |
| AUD-22713 | Suggestion | process | `src/features/ai_harness/tests/test_run_trace.py:126（对照 src/features/ai_harness/run_trace.py:473）` | medium | skipped_entries 精确匹配内部异常类名，与实现细节耦合 |
| AUD-22714 | Suggestion | test-gap | `src/features/ai_harness/tests/test_runtime.py:61（及 74、86、93 等全部 test 方法）` | high | 测试方法缺 `-> None` 返回注解 |
| AUD-22800 | Suggestion | process | `src/features/ai_harness/tests/test_runtime_node_events.py:83-87` | high | no-op 用例无任何断言，仅靠"不抛"通过 |
| AUD-22801 | Suggestion | process | `src/features/ai_harness/tests/test_scheduler_adapter.py:37,45-46,63,72,86,93,101,108,116,125,137,155,165` | high | 全部测试方法与助手缺少返回类型注解 |
| AUD-22802 | Suggestion | process | `src/features/ai_harness/tests/test_scheduler_adapter.py:19` | high | edit_core 标记在同批次 8 文件中仅 3 个使用，标记口径不一致 |
| AUD-22803 | Major | bug | `src/features/ai_harness/tests/test_skill_catalog.py:50-58` | medium | test_subtitle_draft_has_target_duration 名不副实：注释与 manifest 事实相反，且未断言 target_duration 的任何行为 |
| AUD-22804 | Suggestion | process | `src/features/ai_harness/tests/test_skill_catalog.py:196-198,50,108,202 等` | high | 测试方法与 _registry() 助手缺少类型注解 |
| AUD-22805 | Suggestion | process | `src/features/ai_harness/tests/test_skill_compiler.py:47,157,179,191,207,222,235,255,283,305,327,346,354,367` | high | 测试方法与 _build_service_with_analysis 缺少类型注解 |
| AUD-22806 | Suggestion | process | `src/features/ai_harness/tests/test_skill_compiler.py:276-281（同模式 302）` | medium | "三入口同结构"仅比较 to_public_dict 的键集合，一致性验证鉴别力弱 |
| AUD-22807 | Major | bug | `src/features/ai_harness/tests/test_skill_dry_run.py:199-202,239-242,249-252` | medium | 红线"无副作用"事件断言使用不存在/拼写错误的 RunEventKind 字符串，全部恒真 |
| AUD-22808 | Major | doc-gap | `src/features/ai_harness/tests/test_skill_dry_run.py:15-16（docstring）与 187-202（唯一权限用例）` | medium | 文件 docstring 声称覆盖"权限不足 → PERMISSION_DENIED"，但无任何用例断言该错误码 |
| AUD-22809 | Suggestion | process | `src/features/ai_harness/tests/test_skill_dry_run.py:190` | high | 未使用的导入 dataclasses.replace |
| AUD-22810 | Suggestion | process | `src/features/ai_harness/tests/test_skill_eval_suite.py:128-139` | medium | upload_rejected 用例 fixture 与草稿错配，且判定断言对词汇表恒真，鉴别力弱 |
| AUD-22811 | Suggestion | process | `src/features/ai_harness/tests/test_skill_eval_suite.py:304-305（对照模块顶部 30-33），及 245-246,252-253,260 的词汇表外事件名` | medium | 函数内重复导入 TimelineSnapshot；建议事件断言改用 RunEventKind 常量 |
| AUD-22812 | Major | bug | `src/features/ai_harness/tests/test_skill_release.py:179-190` | medium | test_publish_arbitrary_code_executor_blocked 恒真，门禁 ARBITRARY_CODE_EXECUTOR 拒绝路径零覆盖 |
| AUD-22813 | Suggestion | process | `src/features/ai_harness/tests/test_skill_studio_drafts.py:56-59` | high | kind_for_workflow 用例断言过弱：仅断言值属于 3 值集合 |
| AUD-22814 | Suggestion | process | `src/features/ai_harness/tests/test_skill_studio_drafts.py:303-309` | medium | 草稿隔离红线用例探测私有属性，实现重命名后退化为恒真 |
| AUD-23001 | Major | bug | `src/features/ai_workflow/adapters/comfyui_client.py:244-249` | medium | capabilities() 对 /object_info 的解析结构与 ComfyUI 真实格式不符，available_models 恒为空集，Real 路径 preflight 必然 MISSING_MODEL 拒绝 |
| AUD-23002 | Major | bug | `src/features/ai_workflow/adapters/comfyui_client.py:217-223（配合 435-481）` | medium | job_outputs 吞掉下载异常并永久置 outputs_fetched=True：瞬时网络错误被固化为"无产物"或携带占位 hash 的候选 |
| AUD-23003 | Major | bug | `src/features/ai_workflow/adapters/comfyui_client.py:450-452（写入点 456-469）` | medium | 下载落盘路径直接拼接 server 返回的 filename，无路径穿越校验（外部数据未验证） |
| AUD-23004 | Major | bug | `src/features/ai_workflow/adapters/comfyui_client.py:191-203` | medium | cancel() 对 QUEUED 任务也乐观置 CANCELLED，但 POST /interrupt 只打断当前执行、不会出队，server 侧仍会执行该任务 |
| AUD-23005 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_client.py:56-57, 322-327` | medium | /object_info 响应 2MB 上限对装较多 custom node 的真实 ComfyUI 偏小，capabilities() 会 RESPONSE_TOO_LARGE 失败 |
| AUD-23006 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_client.py:387-400` | high | _refresh_state 用 json.dumps 子串匹配判定任务在 running/pending 队列中的归属，脆弱且每轮全量序列化队列 |
| AUD-23007 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_client.py:433, 440, 478-481` | medium | 以 object.__setattr__ 绕过 frozen 契约改写 ComfyOutputArtifact，并以动态属性 _subfolders 扩展 _JobRecord |
| AUD-23008 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_client.py:292-300` | high | _headers 对凭据读取使用宽泛 except Exception，掩盖 CredentialStore 领域错误 |
| AUD-23009 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_converter.py:293-299` | high | 构造器中赋值的 _known_targets 从未使用（死代码），注册校验实际走 registry.get() |
| AUD-2301 | Critical | risk | `src/app/controllers/color_controller.py:1174-1177（on_brush_toggled）、src/app/controllers/color_controller.py:1246-1249（on_brush_stroke_added）` | medium | brush 工具遍历根级遮罩时对 MaskGroup 访问 `.type` 抛 AttributeError，槽内未捕获致进程崩溃 |
| AUD-23010 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_converter.py:182-187` | medium | 参数提取用 `or` 提供默认值，把合法的 0 与缺失/link 引用混淆 |
| AUD-23011 | Major | bug | `src/features/ai_workflow/adapters/comfyui_executor.py:387, 509, 526-531（宿主证据 src/app/controllers/ai_editing_mixin.py:784-787）` | medium | 默认 poll_timeout_s=30s 对真实生成任务过短且宿主未覆盖：超过 30 秒的生成会被主动取消并报 TIMEOUT |
| AUD-23012 | Major | perf-opportunity | `src/features/ai_workflow/adapters/comfyui_executor.py:432-442, 673-681, 688-691` | medium | generate 协议路径无法携带输入绑定，且构造的 ComfyImportReport 不含未绑 slot → preflight 的 MISSING_BINDING 检查恒为空转 |
| AUD-23013 | Major | bug | `src/features/ai_workflow/adapters/comfyui_executor.py:583-585` | high | fetch_outputs 哈希校验在"实际 hash 缺失"时回退为期望值，校验恒等通过——弱化 §9.4 #5 红线 |
| AUD-23014 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_executor.py:188-200` | medium | submitted_payload 的类型转换只捕 ValueError，且空 resolved_value 会覆写快照字面量 |
| AUD-23015 | Suggestion | risk | `src/features/ai_workflow/adapters/comfyui_executor.py:405, 481` | high | _submitted_jobs 只增不减，长生命周期 executor 内存无界增长 |
| AUD-23016 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_executor.py:419-424` | high | supported_modalities 在无 AUDIO 枚举时返回重复的 IMAGE |
| AUD-23017 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_executor.py:381-385（配合 629 行 getattr 探测 output_url）` | high | server 参数注解为 FakeComfyServer，但生产传入 RealComfyClient（靠 duck-typing）；应提取 Protocol |
| AUD-23018 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_executor.py:388, 532-533` | high | poll_interval_s 默认 0.05s 对真实 HTTP server 轮询过密 |
| AUD-23019 | Suggestion | risk | `src/features/ai_workflow/adapters/comfyui_executor.py:428-442, 658-663` | medium | bind_workflow / set_server_capabilities 与 worker 线程中的 generate 无同步保护 |
| AUD-2302 | Major | bug | `src/app/controllers/color_controller.py:35-76（on_mask_shape_added）、78-92（on_mask_shape_removed）、183-225（on_all_masks_cleared）、1057-1114（on_mask_hierarchy_updated）、468-489（_on_tracker_pattern_committed）` | medium | 遮罩增/删/清空/层级重组均不写 undo 栈，"清空全部遮罩"将永久销毁所有图层的 AI matte 且无确认 |
| AUD-23020 | Major | bug | `src/features/ai_workflow/adapters/comfyui_export.py:238-246（冲突证据 comfyui_models.py:488-492）` | medium | 导出 payload 顶层注入无 class_type 的 `_topos_meta` 键：纯 Comfy 导出无法被 ComfyUI /prompt 或本项目 importer 加载，与 equivalence_guaranteed=Tr |
| AUD-23021 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_export.py:287-298` | medium | _apply_binding 与 executor 同源的转换缺陷：只捕 ValueError、空 resolved_value 覆写字面量 |
| AUD-23022 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_export.py:175-178, 300-310` | medium | _scrub_credentials 只递归 dict 不递归 list，且子串匹配可能误删含 "token"/"secret" 字样的合法输入 |
| AUD-23023 | Major | bug | `src/features/ai_workflow/adapters/comfyui_importer.py:354-389（数据源证据 comfyui_client.py:250-254、comfyui_models.py:543-550）` | medium | UNPINNED_CUSTOM_NODE 门禁不可达：两个数据源在生产路径恒为空，§13.7"未 pin 默认拒绝"红线形同虚设 |
| AUD-2303 | Major | bug | `src/app/controllers/color_controller.py:1441-1445（on_video_denoise_changed）、1457-1461（on_video_denoise_history_reset）、1470-1475（on_video_denoise_debug_overlay_toggled）` | medium | 三处视频降噪参数下发用 `except Exception: pass` 静默吞错，且越层直连 renderer 绕过 engine 统一入口 |
| AUD-2304 | Major | risk | `src/app/controllers/color_controller.py:1508-1527（on_denoise_finished）、1611-1626（on_deblur_finished）、1562-1568 / 1662-1667（两个 reset）` | medium | denoise/deblur 完成回调与重置在 UI 线程同步执行全图拷贝、双次 resize 与 EXR 磁盘写 |
| AUD-2305 | Major | bug | `src/app/controllers/color_controller.py:1504-1539（on_denoise_finished）、1608-1638（on_deblur_finished）` | medium | denoise/deblur 异步完成回调不校验任务上下文，结果可能落到切换后的文件/图层 |
| AUD-2306 | Major | bug | `src/app/controllers/color_controller.py:1697-1704（on_crop_toggled）` | medium | 激活 crop 时静默把工作图像替换为未 bake 的原图，退出（含取消）无恢复路径 |
| AUD-2307 | Major | bug | `src/app/controllers/color_controller.py:1415-1428（on_decompile_lut_requested）` | medium | LUT 反编译同步执行且用 `QApplication.processEvents()` 刷 UI，异常路径状态栏消息永久残留 |
| AUD-2308 | Suggestion | process | `src/app/controllers/color_controller.py:1205-1208` | high | on_brush_params_changed 硬下标访问 params，缺键即 KeyError |
| AUD-2309 | Suggestion | process | `src/app/controllers/color_controller.py:1007（` | medium | 控制器直接调用 FileIO / Layer / TrackerPanel 的私有成员 |
| AUD-2310 | Suggestion | process | `src/app/controllers/color_controller.py:1482-1545（on_denoise_requested/finished/error）vs 1586-1644（on_deblur_requested/finished/error）；1547-1580（on_denoise_reset_requested）vs 1646-1679（on_deblur_reset_requested）` | high | denoise 与 deblur 的四组处理器近乎完全复制（约 200 行） |
| AUD-23100 | Major | bug | `src/features/ai_workflow/adapters/comfyui_models.py:380` | medium | 内置节点白名单条目 `" ConditioningConcat "` 带首尾空格，真实 class_type 永不匹配 |
| AUD-23101 | Major | bug | `src/features/ai_workflow/adapters/comfyui_models.py:505-506, 596-609` | medium | `_canonical_slot_id` 去重计数只递增最终 slot_id，第 3 个同名输入起产生重复 slot_id |
| AUD-23102 | Major | bug | `src/features/ai_workflow/adapters/comfyui_models.py:445, 565-579` | medium | `raw_payload` 仅浅拷贝且为 frozen dataclass 的可变字段，"只读快照 + content_hash 防篡改"红线可被绕过 |
| AUD-23103 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_models.py:509-519, 552-553` | medium | `from_api_format` 未把 node_id 归一为 str：summary 用 `str(node_id)` 而 slot 保留原值，int 型 node_id=0 时 slot 被静默丢弃 |
| AUD-23104 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_models.py:565-568` | medium | `content_hash` 计算对非 JSON 兼容 payload 抛未处理的 TypeError，外部数据校验不完整 |
| AUD-23105 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_models.py:145-157` | medium | `base_url` 校验不拒绝 userinfo，`https://user:key@host` 可把凭证内联进持久化配置 |
| AUD-23106 | Major | bug | `src/features/ai_workflow/adapters/comfyui_profiles.py:78-84` | medium | `load_from_config` 宽泛 `except Exception` 且零日志，坏 profile 条目被静默丢弃 |
| AUD-23107 | Suggestion | process | `src/features/ai_workflow/adapters/comfyui_profiles.py:65, 75` | high | `save_to_config` / `load_from_config` 的 `config` 参数为 `Any`，缺协议/接口注解 |
| AUD-23108 | Major | bug | `src/features/ai_workflow/adapters/composite_adapter.py:456-458, 496-498` | medium | `fetch_result` SUCCESS 路径访问 `self._svc._jobs` 私有属性，破坏自述的 duck-type 替换契约 |
| AUD-23109 | Suggestion | process | `src/features/ai_workflow/adapters/composite_adapter.py:260-269, 157-167` | medium | `quality` / `range_spec` / `import_strategy` 未按各自 `ALL` 枚举校验（Request 与 JobRef 均缺） |
| AUD-2311 | Suggestion | process | `src/app/controllers/color_controller.py:35（` | high | 大量处理器缺类型注解；on_mask_shape_selected 与 on_panel_shape_selected 逻辑重复 |
| AUD-23110 | Suggestion | process | `src/features/ai_workflow/adapters/composite_adapter.py:354-356, 471-472, 477, 510-511` | medium | `poll_render` / `fetch_result` / `cancel_render` 对未知 render_job_id 抛裸 KeyError，与 `submit_render` 的异常包装不一致 |
| AUD-23111 | Suggestion | process | `src/features/ai_workflow/adapters/composite_adapter.py:490-495` | medium | `fetch_result` 把 QUEUED/RUNNING 的 job 映射为 `error_code=RENDER_FAILED`，"未就绪"被误标为终态失败 |
| AUD-23112 | Suggestion | process | `src/features/ai_workflow/adapters/composite_adapter.py:255, 558-562` | low | 节点 spec 默认 `range_spec=WHOLE` 与 `CompositeSubgraphRequest` 默认 `SELECTION` 不一致 |
| AUD-23113 | Major | test-gap | `src/features/ai_workflow/adapters/generation_node_specs.py:353-356（定义）；缺失点 core/node_specs.py:457-485` | medium | `register_generation_node_specs` 未接入生产装配，6 个生成节点在应用内不可达（仅测试调用） |
| AUD-23114 | Major | bug | `src/features/ai_workflow/adapters/generation_node_specs.py:102-113, 128-139, 141-151` | medium | `inpaint` / `image_to_video` / `upscale` 三个 spec 完全没有 `params_schema`，违反本模块"每个字段必须有 JSON Schema"红线 |
| AUD-23115 | Suggestion | process | `src/features/ai_workflow/adapters/generation_node_specs.py:172, 184-186, 256-257, 266` | medium | `_FieldMeta.range` 声明 `Tuple[float, float]` 却填入 int 值，且 `(0.0, 0.0)` 哨兵与真实 0..0 范围冲突 |
| AUD-23116 | Suggestion | process | `src/features/ai_workflow/adapters/generation_node_specs.py:327-330` | medium | `resolution` 为自由字符串，无 pattern 校验 |
| AUD-23117 | Suggestion | process | `src/features/ai_workflow/adapters/generation_node_specs.py:83, 97, 111, 124, 137, 149；src/features/ai_workflow/adapters/comfyui_node_specs.py:59` | medium | 图像/视频生成节点复用 `executor_id=TTS_PROVIDER`，executor 语义与能力错配（comfyui_node_specs.py 同） |
| AUD-2312 | Suggestion | process | `src/app/controllers/color_workspace_controller.py:31-34（_color_workspace_panel_descriptors）、163-166（_refresh_color_workspace_menu）、189-192（_workspace_status）` | medium | 三处宽泛 `except Exception` 吞错且无日志，注册表读取失败时 workspace 功能静默失效 |
| AUD-2313 | Suggestion | process | `src/app/controllers/color_workspace_controller.py:65（get_color_workspace_presets）、152（color_workspace_menu_items）` | high | 公共方法缺返回类型注解 |
| AUD-23200 | Major | bug | `src/features/ai_workflow/core/node_specs.py:235-241` | medium | NodeSpecRegistry.register 无版本比较，旧版本 spec 可静默覆盖新版本 |
| AUD-23201 | Major | doc-gap | `src/features/ai_workflow/core/node_specs.py:471-485（docstring 466-467）` | medium | register_official_specs 宽泛 except Exception 吞掉适配器全部错误且无日志（与文档"只记日志"不符） |
| AUD-23202 | Suggestion | process | `src/features/ai_workflow/core/node_specs.py:169-185` | high | failure_strategy 字段完全无校验（retry_policy 有枚举校验） |
| AUD-23203 | Suggestion | process | `src/features/ai_workflow/core/node_specs.py:151-153, 187-193` | high | inputs/outputs 容器内端口 direction 与容器语义不一致时不拒绝 |
| AUD-23204 | Suggestion | process | `src/features/ai_workflow/core/publish_as_skill.py:218-229` | medium | _bump_to_version 存在死变量与失实注释，且不校验目标版本不低于 draft 当前版本 |
| AUD-23205 | Suggestion | process | `src/features/ai_workflow/core/publish_as_skill.py:169, 196, 205` | medium | 类型注解弱：_infer_evidence_kinds 返回裸 list、register_into 形参为 Any |
| AUD-23206 | Major | bug | `src/features/ai_workflow/core/template_registry.py:459-462, 481-516（注释 494-495）` | medium | 产品粗剪模板 audio_feat 节点输出无任何消费边，音频特征分析白跑 |
| AUD-23207 | Major | bug | `src/features/ai_workflow/core/template_registry.py:321-324` | high | register_official_templates 宽泛 except Exception: pass，吞掉 Composite 模板构造的真实错误且无日志 |
| AUD-23208 | Suggestion | process | `src/features/ai_workflow/core/template_registry.py:137-151` | medium | instantiate 不校验 param_overrides 的 node_id 与参数键，typo 被静默丢弃 |
| AUD-23209 | Suggestion | perf-opportunity | `src/features/ai_workflow/core/template_registry.py:145-160` | medium | instantiate 副本与冻结官方模板共享可变 dict 引用，"官方模板 frozen"红线仅靠约定维持 |
| AUD-23210 | Major | bug | `src/features/ai_workflow/core/workflow_compiler.py:294` | medium | invocation.inputs 的 target_duration 硬编码 720，模板节点声明的 target_duration（60/90）不流入运行时 |
| AUD-23211 | Suggestion | process | `src/features/ai_workflow/core/workflow_compiler.py:153-175（validator 签名见 workflow_validator.py:129-136, 172-180）` | medium | compile 持有 base_revision 却不接入 validator 的 stale revision 检查，STALE_REVISION 在编译路径永不触发 |
| AUD-23212 | Suggestion | process | `src/features/ai_workflow/core/workflow_graph_edit.py:35-46, 72-82` | medium | _is_json_safe 接受 NaN/Inf 浮点（严格 JSON 非法），move_node 同样不校验有限性 |
| AUD-23213 | Suggestion | process | `src/features/ai_workflow/core/workflow_graph_edit.py:49-66（仅有 add_node）` | medium | 变更层缺 remove_node/remove_nodes API，画布删除节点无模型层支撑 |
| AUD-23214 | Suggestion | process | `src/features/ai_workflow/core/workflow_models.py:188-199` | medium | WorkflowPolicy._NETWORK_VALUES/_TIMELINE_VALUES 为死字段且是实例字段（非 ClassVar），校验反而用内联字面量 |
| AUD-23215 | Suggestion | process | `src/features/ai_workflow/core/workflow_models.py:244-260, 380-384` | medium | from_dict 对畸形 notes/groups/exposed_inputs 条目静默丢弃（外部数据未显式验证） |
| AUD-23216 | Suggestion | process | `src/features/ai_workflow/core/workflow_models.py:314-319` | medium | exposed_inputs 只校验 node 存在，不校验 port 存在、方向为输入、exposed name 重复 |
| AUD-23217 | Major | bug | `src/features/ai_workflow/core/workflow_validator.py:266-275（connected_inputs 242-246）` | medium | edge 引用不存在的端口名被静默跳过，注释"端口不存在已在 spec 层处理"不实——全链路无端口存在性检查 |
| AUD-23218 | Major | bug | `src/features/ai_workflow/core/workflow_validator.py:248-264（spec 声明见 node_specs.py:77-79）` | medium | 不校验 allow_multi：多条边可连入声明为单值的输入端口 |
| AUD-23219 | Major | bug | `src/features/ai_workflow/core/workflow_validator.py:129-182（validate 全部检查项，无 params 相关）；node_specs.py:320-323（schema 声明）、workflow_compiler.py:308-310（原样透传）` | medium | 节点 params 全链路无 params_schema 校验（声明即死数据；官方模板自身也设 schema 外参数） |
| AUD-23220 | Suggestion | process | `src/features/ai_workflow/core/workflow_validator.py:344-347` | medium | approval token 检查硬编码官方 spec_id，未按 timeline_write_required 属性覆盖自定义节点 |
| AUD-23301 | Suggestion | process | `src/features/ai_workflow/module.py:82-88` | medium | `except TypeError` 兜底可掩盖 `set_callbacks` 内部真实 TypeError，且仅记 debug 级日志 |
| AUD-23302 | Suggestion | process | `src/features/ai_workflow/module.py:55、96` | high | 类型注解不完整：裸 `List` 返回注解与未注解参数 |
| AUD-23400 | Major | bug | `src/features/ai_workflow/runtime/fake_workflow_service.py:131-144` | medium | 取消后仍返回"成功"结果，run 终态被记为 SUCCEEDED 而非 CANCELLED |
| AUD-23401 | Suggestion | process | `src/features/ai_workflow/runtime/workflow_action_service.py:152-158` | medium | apply 提交不做 revision 复核，与 rollback 的可验证边界不对称 |
| AUD-23402 | Suggestion | process | `src/features/ai_workflow/runtime/workflow_action_service.py:50-52` | high | `_plans` / `_previews` / `_applied` 无淘汰，长会话内存持续增长 |
| AUD-23403 | Major | bug | `src/features/ai_workflow/runtime/workflow_context.py:186-189` | medium | 仅配置过 ComfyUI profile（未绑定/未探活）即广告 image_gen+video_gen 能力 |
| AUD-23404 | Major | bug | `src/features/ai_workflow/runtime/workflow_run_controller.py:282-285` | medium | `_current_run_id` 从不清空，第二次 run 起 pending-cancel 机制失效，窗口期取消被静默丢失 |
| AUD-23405 | Major | risk | `src/features/ai_workflow/runtime/workflow_run_controller.py:530-535` | medium | `run()`/`_start_worker` 无在跑防护，覆盖仍运行中的旧 worker 引用可致 QThread 销毁崩溃 |
| AUD-23406 | Suggestion | process | `src/features/ai_workflow/runtime/workflow_run_controller.py:702-705` | high | worker 失败仅记日志，失败信息不进 Run Drawer |
| AUD-23407 | Suggestion | risk | `src/features/ai_workflow/runtime/workflow_run_controller.py:457-458` | medium | `dispose` 等待超时后既不取消 run 也不处理仍在运行的线程 |
| AUD-23501 | Major | test-gap | `src/features/ai_workflow/tests/test_comfyui_client.py:136-144（辅证 :75、:302-311）` | medium | /queue→QUEUED 映射测试空转：注入态即期望态，且 fake 对未命中路由静默返回空 JSON |
| AUD-23502 | Suggestion | process | `src/features/ai_workflow/tests/test_comfyui_client.py:129` | high | `if False` 死表达式伪装成 submit 调用 |
| AUD-23503 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_client.py:131,143,152,170,180,189,205（导入于 :303）` | high | 7 处测试直接注入私有 `_jobs` 并导入私有 `_JobRecord` |
| AUD-23504 | Suggestion | process | `src/features/ai_workflow/tests/test_comfyui_client.py:215` | high | 下载落盘断言硬编码 "/" 分隔符 |
| AUD-23505 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_client.py:262-271` | medium | 协议一致性测试仅 getattr callable，与验收"签名 + 行为"不符 |
| AUD-23506 | Suggestion | process | `src/features/ai_workflow/tests/test_comfyui_client.py:232-247` | medium | cloud Bearer 头只测 happy path |
| AUD-23510 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_converter.py:284-299` | medium | "转换不执行 custom node"红线测试不可证伪 |
| AUD-23511 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_converter.py:92-94（同构重复于 :102-104、:114-116、:147-149 等 10+ 处）` | high | 每个测试重复 registry/converter 构建样板，缺 fixture |
| AUD-23512 | Suggestion | process | `src/features/ai_workflow/tests/test_comfyui_converter.py:108、:151` | high | 直接索引 findings[0] 前未断言数量 |
| AUD-23520 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_executor.py:249-269` | medium | hash-mismatch server 测试名与断言不符，outputs 从未被断言 |
| AUD-23521 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_executor.py:546、:567` | high | 两处测试穿透 `server._jobs` 私有结构，与本文件声明的协议约束相悖 |
| AUD-23530 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_export.py:135-143` | medium | "不含凭据"红线测试空转：输入 fixture 本身不含任何凭据 |
| AUD-23531 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_export.py:126-133` | medium | "只读不联网不执行"测试仅验证两次导出结果一致 |
| AUD-23540 | Suggestion | process | `src/features/ai_workflow/tests/test_comfyui_importer.py:261-269` | medium | 死变量 + hasattr 探测疑似不存在的方法 `_replace_if_possible` |
| AUD-23541 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_importer.py:363-382（辅证 :361）` | medium | "JobRef 不可绕过 preflight"测试为死分支 + 同义反复断言，验证力为零 |
| AUD-23542 | Suggestion | process | `src/features/ai_workflow/tests/test_comfyui_importer.py:243-250` | medium | 对诊断文案的子串断言脆弱（与英文措辞强耦合） |
| AUD-23550 | Suggestion | process | `src/features/ai_workflow/tests/test_comfyui_models.py:207-209` | high | `pytest.raises(Exception)` 捕获过宽 |
| AUD-23551 | Suggestion | process | `src/features/ai_workflow/tests/test_comfyui_models.py:189-191` | high | 绑定"类型兼容"正向用例是回显断言，未测任何类型校验 |
| AUD-23552 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_comfyui_models.py:359-370` | high | "快照不执行 custom node"红线测试不可证伪 |
| AUD-23560 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_composite_adapter.py:161-172` | high | "输入是 asset_ref 不是 texture"红线测试为同义反复 |
| AUD-23561 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_composite_adapter.py:400-405、:424-429、:438-442、:452-456（helper 重复 :211-215 与 :302-309）` | medium | 规格注册测试 4 处方法内重复局部 import + 两类重复 helper |
| AUD-23600 | Major | perf-opportunity | `src/features/ai_workflow/tests/test_fake_workflow_service.py:40` | medium | 红线源码扫描在 runtime 目录缺失时静默返回空串，全部红线断言空转通过 |
| AUD-23601 | Major | bug | `src/features/ai_workflow/tests/test_fake_workflow_service.py:52` | medium | GPU 红线子串检查可被 `from moderngl import ...` 等导入形式绕过 |
| AUD-23602 | Suggestion | process | `src/features/ai_workflow/tests/test_fake_workflow_service.py:161` | high | `cancel_after` 死变量 |
| AUD-23603 | Suggestion | process | `src/features/ai_workflow/tests/test_fake_workflow_service.py:95` | high | fixture 返回类型标注为 `Any`，丢失类型信息 |
| AUD-23610 | Suggestion | process | `src/features/ai_workflow/tests/test_generation_node_specs.py:156` | high | 注释声称校验字段标注，但断言仅检查 `"type"` 存在 |
| AUD-23611 | Suggestion | process | `src/features/ai_workflow/tests/test_generation_node_specs.py:134` | high | `test_no_full_access_or_skip_approval_node` 实际仅覆盖 generation specs |
| AUD-23612 | Suggestion | process | `src/features/ai_workflow/tests/test_generation_node_specs.py:214` | high | unit/range 断言范围与注释不一致 |
| AUD-23613 | Suggestion | process | `src/features/ai_workflow/tests/test_generation_node_specs.py:24` | high | `pytest` 导入未使用 |
| AUD-23620 | Major | bug | `src/features/ai_workflow/tests/test_node_canvas_protocol.py:29` | high | 红线子串检查可被其他导入形式绕过（同 AUD-23601 模式） |
| AUD-23621 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_node_canvas_protocol.py:125` | high | "无 Qt 环境可导入"测试实际未验证任何 Qt 无关性 |
| AUD-23622 | Suggestion | process | `src/features/ai_workflow/tests/test_node_canvas_protocol.py:18` | high | `sys` 导入未使用 |
| AUD-23630 | Suggestion | process | `src/features/ai_workflow/tests/test_node_specs.py:96` | high | executor 白名单硬编码字符串元组，与 `NodeExecutorId.ALL` 重复 |
| AUD-23640 | Suggestion | process | `src/features/ai_workflow/tests/test_p10_3_templates.py:140` | medium | 手动注册 Composite spec 冗余且注释过时 |
| AUD-23641 | Suggestion | process | `src/features/ai_workflow/tests/test_p10_3_templates.py:36` | high | fixtures 缺返回类型注解 |
| AUD-23650 | Suggestion | process | `src/features/ai_workflow/tests/test_p5_acceptance.py:252` | high | 死变量 `g` 与虚假注释（构造并不拒绝 <3 段 spec_id） |
| AUD-23660 | Suggestion | perf-opportunity | `src/features/ai_workflow/tests/test_p6_acceptance.py:354` | high | `if result.succeeded:` 守卫可使红线断言空转通过 |
| AUD-23661 | Suggestion | process | `src/features/ai_workflow/tests/test_p6_acceptance.py:41` | high | `ComfyJobState` 导入未使用 |
| AUD-23662 | Suggestion | process | `src/features/ai_workflow/tests/test_p6_acceptance.py:94` | high | 与 test_p7 重复复制的 fixture 助手已发生内容漂移 |
| AUD-23670 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_p7_acceptance.py:362` | medium | C5 红线测试的 for 循环在 findings 为空时空转通过 |
| AUD-23671 | Suggestion | process | `src/features/ai_workflow/tests/test_p7_acceptance.py:24` | high | `pytest` 与 `GOLDEN_FIXTURES` 导入未使用 |
| AUD-23700 | Major | bug | `src/features/ai_workflow/tests/test_publish_as_skill.py:157-158` | high | deny_code 断言被 `or result.deny_code` 弱化为"任意拒绝码即通过" |
| AUD-23701 | Major | bug | `src/features/ai_workflow/tests/test_publish_as_skill.py:160-168` | medium | executor_kind 安全红线断言包裹在 `if result.success:` 内，失败时静默跳过 |
| AUD-23702 | Suggestion | process | `src/features/ai_workflow/tests/test_publish_as_skill.py:23,25,42` | high | 未使用的 import：pytest、SkillReleaseGate、PublishFromWorkflowResult |
| AUD-23703 | Suggestion | process | `src/features/ai_workflow/tests/test_template_registry.py:20,27,33-34` | medium | 未使用的 import：pytest、WorkflowTemplateEntry、WorkflowGraph、WorkflowPolicy |
| AUD-23704 | Suggestion | process | `src/features/ai_workflow/tests/test_template_runnability.py:61-66,81-83,96-97` | high | `_compile_template` 与各测试方法缺返回类型注解 |
| AUD-23705 | Major | test-gap | `src/features/ai_workflow/tests/test_workflow_action_service.py:180-184` | medium | rollback 测试声称验证"宿主无回滚能力时 fail closed"，实际走的是 |
| AUD-23706 | Suggestion | process | `src/features/ai_workflow/tests/test_workflow_action_service.py:10,57-58,65-80` | high | 未使用 import `compile_plan`；伪造对象方法缺类型注解 |
| AUD-23707 | Suggestion | process | `src/features/ai_workflow/tests/test_workflow_compiler.py:25,27` | high | 未使用的 import：pytest、Invocation、RunPolicy |
| AUD-23708 | Suggestion | process | `src/features/ai_workflow/tests/test_workflow_compiler.py:128-137` | medium | policy 映射断言值与 WorkflowPolicy 默认值相同，测不出映射回归 |
| AUD-23709 | Suggestion | process | `src/features/ai_workflow/tests/test_workflow_context.py:21-26,43-45,52-53` | high | 伪造服务类方法缺返回类型注解 |
| AUD-23710 | Suggestion | process | `src/features/ai_workflow/tests/test_workflow_graph_edit.py:38-45` | medium | 红线源码扫描为朴素子串匹配，可被动态 import 绕过且可能误伤注释 |
| AUD-23711 | Suggestion | process | `src/features/ai_workflow/tests/test_workflow_graph_edit.py:54,74,84-105 等` | high | `_node` 的 `params` 参数与部分测试函数缺类型注解 |
| AUD-23801 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_workflow_run_bridge.py:45` | medium | bridge 声明的 payload 参数与"无效 run_id 静默 no-op"契约均无测试 |
| AUD-23802 | Suggestion | process | `src/features/ai_workflow/tests/test_workflow_run_bridge.py:64` | high | `received` 列表缺少类型注解 |
| AUD-23803 | Major | test-gap | `src/features/ai_workflow/tests/test_workflow_validator.py:249` | medium | approval_token 红线用 `or` 弱化断言，红线检查回归时测试仍通过 |
| AUD-23804 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_workflow_validator.py:228` | medium | approval_token 缺正向用例："token 来自 user.approval"的合法链路未测试 |
| AUD-23805 | Suggestion | process | `src/features/ai_workflow/tests/test_workflow_validator.py:26` | high | `NodeCategory` 导入未使用 |
| AUD-23806 | Suggestion | test-gap | `src/features/ai_workflow/tests/test_workflow_validator.py:5` | medium | validator 分支覆盖缺口：exposed_inputs 满足必填输入、docstring 声明的"锁定轨道"检查无测试 |
| AUD-23901 | Major | bug | `src/features/ai_workflow/ui/workflow_canvas_view.py:632-637（另见 368-374 存储侧）` | medium | 框选矩形 overlay 用视口坐标绘制，而 drawForeground 的 painter 处于场景坐标系 |
| AUD-23902 | Major | bug | `src/features/ai_workflow/ui/workflow_canvas_view.py:311-318、434-445` | medium | 空地点击与框选都不清除已有选中，用户无法取消选中 |
| AUD-23903 | Major | bug | `src/features/ai_workflow/ui/workflow_canvas_view.py:319-325、404-417` | medium | nodes_moved 快照在选中变化之前采集，首次点选未选中节点并拖动时移动不提交 |
| AUD-23904 | Major | bug | `src/features/ai_workflow/ui/workflow_canvas_view.py:208-213（同模式 183-187）` | medium | add_connection_item 重复 key 直接覆盖 dict，旧连线孤儿化残留 scene |
| AUD-23905 | Major | bug | `src/features/ai_workflow/ui/workflow_canvas_view.py:82、115` | medium | assets_dropped 信号 + setAcceptDrops(True) 无任何拖放事件实现，功能为死通道 |
| AUD-23906 | Major | bug | `src/features/ai_workflow/ui/workflow_canvas_view.py:208-213；交叉证据 workflow_panel.py:367-380、workflow_canvas_adapter.py:74-81` | medium | 程序化 add_connection_item 发用户意图信号 node_connected，造成 host 编辑层必然再入 |
| AUD-23907 | Suggestion | process | `src/features/ai_workflow/ui/workflow_canvas_view.py:194-201；交叉 workflow_node_item.py:201-206` | medium | remove_node_item 删除关联连线时不清理存活端节点的 _connections 注册 |
| AUD-23908 | Suggestion | process | `src/features/ai_workflow/ui/workflow_canvas_view.py:616-630` | medium | drawBackground 逐点 drawPoint 双重循环，缩小视图时单次重绘可达 10 万+ 绘点调用 |
| AUD-23909 | Suggestion | process | `src/features/ai_workflow/ui/workflow_canvas_view.py:584-612` | medium | _space_held 无 focusOut 兜底，Space 状态可卡死为平移模式 |
| AUD-23910 | Suggestion | process | `src/features/ai_workflow/ui/workflow_canvas_view.py:464-477、492-514` | medium | 命中检测直接访问节点项私有成员，且与锚点规则两处重复实现 |
| AUD-23911 | Suggestion | process | `src/features/ai_workflow/ui/workflow_canvas_view.py:584、606` | high | 事件处理器参数缺类型注解 |
| AUD-23912 | Major | risk | `src/features/ai_workflow/ui/workflow_canvas_adapter.py:69-72` | medium | build_from_graph 对节点坐标 float() 强转无 None/非法值防护，载入持久化图可崩溃 |
| AUD-23913 | Suggestion | process | `src/features/ai_workflow/ui/workflow_canvas_adapter.py:44-52` | high | registry 构造参数缺类型注解 |
| AUD-23914 | Suggestion | process | `src/features/ai_workflow/ui/workflow_node_item.py:303-312` | high | paint() 中 bar_rect 为死变量（计算后从未使用） |
| AUD-23915 | Suggestion | process | `src/features/ai_workflow/ui/workflow_node_item.py:330、344（绘制）对照 226、232（锚点）` | medium | 端口圆点绘制中心（x=0 / x=width）与命中检测、连线锚点（±0.5*port_radius）系统性不一致 |
| AUD-23916 | Suggestion | process | `src/features/ai_workflow/ui/workflow_node_item.py:150-155；调用方 workflow_canvas_view.py:504-509` | high | input/output_artifact_types 属性每次访问重建 dict，位于鼠标移动热路径 |
| AUD-23917 | Suggestion | process | `src/features/ai_workflow/ui/workflow_node_item.py:76-77、115、246-248` | high | 若干成员/参数缺泛型或类型注解 |
| AUD-23918 | Suggestion | process | `src/features/ai_workflow/ui/workflow_connection_item.py:153-158` | high | shape() 命中区宽度 12px 硬编码，违背视觉 token 单一来源 |
| AUD-23919 | Suggestion | process | `src/features/ai_workflow/ui/workflow_connection_item.py:46-51` | high | src_port/dst_port 构造参数无类型注解 |
| AUD-23920 | Major | risk | `src/features/ai_workflow/ui/workflow_inspector.py:146-168` | medium | set_node 对参数值 int()/float() 强转无异常防护，异常外部数据使选中/加载路径崩溃 |
| AUD-23921 | Suggestion | process | `src/features/ai_workflow/ui/workflow_inspector.py:89（对照 119、139、151、164 的回退值）` | low | JSON-Schema 的 default 从未被读取，缺键参数初值取 minimum/空串/False |
| AUD-23922 | Suggestion | process | `src/features/ai_workflow/ui/workflow_inspector.py:57、102` | high | "(no selection)" 占位文案硬编码英文，未走 tr() |
| AUD-23923 | Suggestion | process | `src/features/ai_workflow/ui/workflow_form_page.py:39-46、124、144` | high | _inspectors 列表只写不读（死状态）；registry 参数注解为 Any |
| AUD-23924 | Suggestion | doc-gap | `src/features/ai_workflow/ui/workflow_gallery_page.py:6-7（对照 84-108 全函数）` | high | 文档承诺「双击卡片」选择模板，实现缺失（仅按钮路径可用） |
| AUD-24001 | Suggestion | doc-gap | `src/features/ai_workflow/ui/workflow_node_search.py:79` | medium | 过滤未按文档匹配完整 description，仅匹配截断到 48 字符的描述 |
| AUD-24002 | Suggestion | process | `src/features/ai_workflow/ui/workflow_node_search.py:21` | high | 未使用的导入 `List` |
| AUD-24003 | Suggestion | process | `src/features/ai_workflow/ui/workflow_node_search.py:40` | high | 构造参数类型注解退化为 `Any` |
| AUD-24004 | Major | bug | `src/features/ai_workflow/ui/workflow_panel.py:362-365（唯一 push 点）、:374-377（add_edge 无 push）、:399-401（set_node_param 无 push）、:437-438（add_node 无 push）` | medium | 撤销栈覆盖不完整：加节点 / 加边 / 改参数不产生 undo 动作，Ctrl+Z 会错误回退更早的移动 |
| AUD-24005 | Major | bug | `src/features/ai_workflow/ui/workflow_panel.py:394-403（改参后不刷新任何视图）、:417-440（加节点后不重建表单）、:585（form_page.set_graph 仅在 _set_current_graph 调用）` | medium | 表单页与画布 Inspector 各持参数快照，跨视图编辑后互不刷新，显示值与 Run 执行值不一致 |
| AUD-24006 | Suggestion | risk | `src/features/ai_workflow/ui/workflow_panel.py:80、:642-646` | medium | `set_run_drawer` 的 `_updating` 锁从未被读取，是无效的装饰性防递归锁 |
| AUD-24007 | Suggestion | process | `src/features/ai_workflow/ui/workflow_panel.py:374-377（配合 workflow_canvas_view.py:208-213）` | medium | 程序化 `add_connection_visual` 会再入触发 `node_connected`，当前仅靠"重复边 ValueError 被吞"终止，且正确性依赖赋值顺序 |
| AUD-24008 | Suggestion | process | `src/features/ai_workflow/ui/workflow_panel.py:398-403` | medium | `_on_parameter_changed` 静默吞掉 ValueError/KeyError，参数被拒时 UI 与模型失同步且无任何反馈 |
| AUD-24009 | Suggestion | process | `src/features/ai_workflow/ui/workflow_panel.py:455-456、:462-470` | high | `set_context_snapshot` 对 `to_public_dict()` 返回值不校验，非 dict 返回会让槽内 AttributeError |
| AUD-2401 | Major | risk | `src/app/controllers/composite_controller.py:1215-1270（另见 1053、1206）` | medium | 视频/序列渲染回调闭包直接操作 Qt 控件，跨线程调用安全性完全依赖 exporter 实现 |
| AUD-24010 | Suggestion | process | `src/features/ai_workflow/ui/workflow_panel.py:121、:124、:159-163、:169、:176-184、:191、:502-504、:512、:514-520、:598` | medium | 大量 UI 字符串硬编码中文未走 tr()，与同文件 tr() 用法不一致 |
| AUD-24011 | Suggestion | process | `src/features/ai_workflow/ui/workflow_panel.py:158-163 与 :457-461` | medium | 上下文标签字典在 `_build_ui` 与 `set_context_snapshot` 两处重复定义，漂移即 KeyError |
| AUD-24012 | Suggestion | process | `src/features/ai_workflow/ui/workflow_panel.py:66-72（` | medium | 关键公共接口缺类型注解 |
| AUD-24013 | Suggestion | process | `src/features/ai_workflow/ui/workflow_panel.py:487` | high | `"timeline" + "_write_requires_approval"` 拼接常量，可读性与可检索性差 |
| AUD-2402 | Major | bug | `src/app/controllers/composite_controller.py:1209、1240、1262、1278-1297` | medium | 重复启动渲染会静默覆盖 `_composite_render_exporter`，旧任务回调会清掉新任务的取消入口 |
| AUD-2403 | Major | bug | `src/app/controllers/composite_controller.py:1182-1270（关键 1264-1270）；对照图像路径的 finally（1115-1116）` | medium | `exporter.export()` 同步抛异常时进度条、取消过滤器、光标与状态栏消息全部残留 |
| AUD-2404 | Major | bug | `src/app/controllers/composite_controller.py:178-181、203-207、415-435、1095-1098（另见 1283-1290 teardown）` | medium | 多处宽泛 except 吞错（含裸 pass 与 debug 级日志），可掩盖用户编辑丢失与旧帧缓存残留 |
| AUD-2405 | Major | bug | `src/app/controllers/composite_controller.py:924` | medium | `_last_metadata` 属性存在但为 None 时，点击渲染输出即 AttributeError |
| AUD-2406 | Major | risk | `src/app/controllers/composite_controller.py:610-611、657-658、1307-1310` | medium | 工程文件图数据的数值字段未校验即 int() 强转，损坏数据可致页面切换/渲染输出崩溃 |
| AUD-2407 | Major | bug | `src/app/controllers/composite_controller.py:1061-1068、1084-1089` | medium | `keep_linear=(ext == "exr")` 把 EXR 之外的一切格式都按显示编码处理，浮点 HDR 格式的线性语义被破坏 |
| AUD-2408 | Suggestion | process | `src/app/controllers/composite_controller.py:37-109、529-543（对照 961-1003 的` | medium | 图数据加载路径无节点类型/连线完整性校验（渲染路径有 preflight，加载路径没有） |
| AUD-2409 | Suggestion | process | `src/app/controllers/composite_controller.py:341-372` | medium | 图变更经直接 `replace()` 写入 EditProject，不经 command 入口，本层不可见 undo/redo 覆盖 |
| AUD-2410 | Suggestion | risk | `src/app/controllers/composite_controller.py:459-468` | medium | 属性面板接收节点 `parameters` 的可变引用，`set_node` 程序化更新在本层无状态锁 |
| AUD-24101 | Suggestion | process | `src/features/color/__init__.py:1-4` | medium | 包入口 eager import ColorModule，把 features→app 依赖链引入所有 `src.features.color.*` 导入 |
| AUD-2411 | Suggestion | process | `src/app/controllers/composite_controller.py:1085-1089` | high | 8/16bit 量化用截断而非四舍五入，且未处理 NaN |
| AUD-2412 | Suggestion | process | `src/app/controllers/composite_controller.py:401-437（触发链入口 372）` | medium | Edit 页时每次图变更全量 `set_project` + `load_timeline` + 请求预览帧，无节流 |
| AUD-2413 | Suggestion | process | `src/app/controllers/composite_controller.py:131-138、447、856-858、938` | medium | 控制器越层访问 Feature 页面私有成员（`_renderer`、`node_graph`、`node_editor`、`_current_composite_id`） |
| AUD-2414 | Suggestion | process | `src/app/controllers/composite_controller.py:366-372 对照 692-693、418-426` | medium | `replace()` 产生新 EditProject 后，`_timeline_clip_frame_provider` 持有的旧引用未刷新 |
| AUD-2415 | Suggestion | process | `src/app/controllers/composite_controller.py:851、861、1301` | high | 多个序列化/解析辅助方法缺少返回类型注解 |
| AUD-24200 | Major | bug | `src/features/color/adapters/color_stack_hydrator.py:74-85` | medium | 全量恢复对工程文件来源的 layers 快照零校验，单条损坏即中断整个水化并留下半应用状态 |
| AUD-24201 | Major | doc-gap | `src/features/color/adapters/color_stack_hydrator.py:265-280` | medium | collect_stack_from_engine_and_panels 不采集 video_denoise，与 apply 路径不对称，按其文档用途作保存时会丢 per-clip 降噪状态 |
| AUD-24202 | Major | bug | `src/features/color/adapters/color_stack_hydrator.py:203、78` | medium | 快照往返使用 to_dict(None)/from_dict 无 assets_dir，BITMAP 蒙版（AI 蒙版）与 AI matte 不进快照，切 clip 即丢失 |
| AUD-24203 | Suggestion | process | `src/features/color/adapters/color_stack_hydrator.py:265-268` | high | collect 构造结果硬编码 name="Default"、asset_id=None，丢弃原栈身份元数据（scope/timeline_id/clip_id/created_at 等） |
| AUD-24204 | Suggestion | process | `src/features/color/adapters/color_stack_hydrator.py:229-230、274-275` | medium | 采集的 layer.lut_intensity 未做 0.0–1.0 边界校验，越界外部数据会让 ColorStackData 构造抛 ValueError 打断保存 |
| AUD-24205 | Suggestion | process | `src/features/color/adapters/color_stack_hydrator.py:418-422` | high | _read_soft_clip_params 宽泛 except 返回部分结果，半截参数会被当作权威值保存 |
| AUD-24206 | Suggestion | process | `src/features/color/adapters/color_stack_hydrator.py:59-72` | medium | video_denoise 恢复用 except Exception 以 debug 级吞掉引擎/参数反序列化错误 |
| AUD-24207 | Suggestion | process | `src/features/color/adapters/color_stack_hydrator.py:154` | high | _apply_video_denoise_to_panel 缺少参数类型注解 |
| AUD-24301 | Major | bug | `src/features/color/ai/color_context.py:205（另见 85-86、150-151、368、479、514-515）` | medium | frozen dataclass 的 Mapping 字段持有可变 dict，破坏不可变与哈希绑定契约 |
| AUD-24302 | Major | bug | `src/features/color/ai/color_context.py:411-427` | medium | validate_hdr 未校验 LUT transfer_function 取值合法性，未知字符串静默绕过 HDR 防线 |
| AUD-24303 | Suggestion | process | `src/features/color/ai/color_context.py:365, 379-381` | high | HDRViolation.severity 无枚举校验，非 "error" 值一律视为非阻断 |
| AUD-24304 | Suggestion | process | `src/features/color/ai/color_context.py:139-151, 443` | medium | ColorMeasurement 无范围/NaN 校验，NaN 峰值可静默通过阈值判断 |
| AUD-24305 | Suggestion | process | `src/features/color/ai/color_context.py:407, 463（字段声明 377）` | high | validate_hdr 的 max_code_value 永远为 None（未完成功能/死变量） |
| AUD-24306 | Suggestion | process | `src/features/color/ai/color_context.py:259-260, 264-266` | high | ColorGradePlan.is_empty 恒为 False，与 __post_init__ 校验矛盾 |
| AUD-24307 | Suggestion | process | `src/features/color/ai/color_context.py:574, 588-604` | medium | compare_grade 用单一回归阈值套用不同量纲指标，自动回滚判定失真 |
| AUD-24308 | Suggestion | process | `src/features/color/ai/color_context.py:287` | medium | plan_hash 用 default=str 序列化任意参数，set 等无序 repr 导致跨进程哈希不稳定 |
| AUD-24309 | Suggestion | process | `src/features/color/ai/color_context.py:243-247, 256-262, 85` | medium | ColorGradePlan 未校验 project_id/timeline_id 非空与 base_revision 非负；快照 detected_input_spaces 值未校验 |
| AUD-24400 | Major | bug | `src/features/color/facade.py:197` | medium | 导出跟踪模式未做词表映射，affine/homography 绑定导入 Composite 后 bake 静默丢失 rotation/scale 通道 |
| AUD-24401 | Suggestion | perf-opportunity | `src/features/color/facade.py:212` | high | 帧循环内的 `TrackStatus` import 未被使用（死代码），且位于每帧迭代内执行 |
| AUD-24402 | Suggestion | process | `src/features/color/facade.py:129、134` | high | 日志使用无占位符的 f-string（F541） |
| AUD-24500 | Major | bug | `src/features/color/integrations/tangent_bridge.py:500-509（环路径）、:433-441（唯一守卫与重置）、:531-538（pending 清零）` | medium | 环形（brightness）硬件调节经 `grading_changed` 回灌会无条件清空在途 trackerball 批处理与平滑状态 |
| AUD-24501 | Major | bug | `src/features/color/integrations/tangent_bridge.py:808-810（` | medium | 多个 Hub 驱动 slot 内直接 `raise ValueError`（含一处未防护的 `float()` 索引取值），异常逃逸进 Qt 事件循环将按 PyQt6 默认行为 qFatal 终止整个应用 |
| AUD-24502 | Suggestion | process | `src/features/color/integrations/tangent_bridge.py:1018-1022` | high | `_is_default` 的第二个 `or` 子句被首条件完全吸收，属死逻辑 |
| AUD-24503 | Suggestion | process | `src/features/color/integrations/tangent_bridge.py:274（` | medium | 弱类型注解：`panels: object` 与 `main_window: Any` 削弱静态检查 |
| AUD-24601 | Major | bug | `src/features/color/module.py:221（另见 :255）` | medium | 默认快捷键 Ctrl+Shift+S / Ctrl+Shift+D 与其他命令冲突 |
| AUD-24602 | Suggestion | process | `src/features/color/module.py:113-125、552-560` | medium | get_facade/initialize_facade 为死代码，且 initialize_facade 无条件覆盖已发出的 facade |
| AUD-24603 | Suggestion | process | `src/features/color/module.py:16` | high | FeatureModule 导入未使用 |
| AUD-24604 | Suggestion | process | `src/features/color/module.py:134、194、203、260` | high | 注册类方法参数缺类型注解 |
| AUD-24611 | Major | bug | `src/features/color/preferences.py:646-660（load）、749-764（apply）、565-577（_set_combo_data）、378-386（_make_range_combo 首项 "limited"）` | medium | 打开偏好页并点确定会把 image 范围键静默写成 "limited"，覆盖运行时 FULL 默认 |
| AUD-24612 | Major | bug | `src/features/color/preferences.py:622-636` | medium | load() 对外部配置值做未保护的 float()/int() 转换，坏值导致整页静默半途加载中断 |
| AUD-24613 | Suggestion | process | `src/features/color/preferences.py:793、798、808-813` | medium | restore_defaults 混用直接索引与 .get，缺键即 KeyError |
| AUD-24614 | Suggestion | process | `src/features/color/preferences.py:80、86` | high | _dialog_ref 注释称弱引用，实为强引用，与对话框互持成环 |
| AUD-24615 | Suggestion | process | `src/features/color/preferences.py:352-361（定义）、402-405（调用）` | medium | _make_transfer_combo 的 default 参数从未使用 |
| AUD-24616 | Suggestion | process | `src/features/color/preferences.py:184、512、899-903` | high | 文件选择对话框标题硬编码英文、OCIO 选择器不过滤文件类型 |
| AUD-24617 | Suggestion | process | `src/features/color/preferences.py:27-30` | high | 跨模块导入 export_panel 的下划线私有样式函数 |
| AUD-24618 | Suggestion | process | `src/features/color/preferences.py:84（create_widget(self, parent)）、566（_set_combo_data 的 value）、690（apply(config_manager: Any)）` | high | 关键方法参数/返回缺类型注解 |
| AUD-24700 | Critical | bug | `src/features/color/shaders/shaders.py:481` | medium | RAW_VIEW 饱和度/色彩增强路径将颜色 clamp 到 [0,1] 后做 HSV 往返，HDR 高光与负值全部丢失 |
| AUD-24701 | Critical | bug | `src/features/color/shaders/shaders.py:434-437, 445-448（对照 405-423）` | medium | RAW_VIEW 锐化/中间细节的邻域采样未应用 BGR swizzle 与 sRGB→线性→ProPhoto 预处理，与中心像素色彩空间不一致 |
| AUD-24702 | Major | bug | `src/features/color/shaders/shaders.py:597-601（对照 507-511）` | medium | F-Gamut 色彩空间矩阵是 Rec.2020 矩阵的逐字节复制 |
| AUD-24703 | Major | bug | `src/features/color/shaders/shaders.py:664（对照 148-151）` | medium | RAW_VIEW 的 3D LUT 采样未做半 texel 中心修正，与本文件 LUT3D 已修复的实现不一致 |
| AUD-24704 | Suggestion | process | `src/features/color/shaders/shaders.py:114-198` | high | LUT3D_VERTEX/LUT3D_FRAGMENT 为无引用死代码，与 composite 特性的活副本形成双份维护 |
| AUD-24705 | Suggestion | process | `src/features/color/shaders/shaders.py:191-194` | medium | u_mix 混合基准使用未经输入域映射的原始纹理值 |
| AUD-24706 | Suggestion | process | `src/features/color/shaders/shaders.py:25` | high | 顶点着色器局部变量 radians 遮蔽 GLSL 内置函数名 |
| AUD-24707 | Suggestion | process | `src/features/color/shaders/shaders.py:107-110` | high | SPLIT_VIEW 分割线宽度为 UV 空间常量，非像素/宽高比自适应 |
| AUD-24801 | Suggestion | process | `src/features/color/tracking/coordinate_map.py:11-14（同款垫片模式同样适用于本批其余 6 个文件：corner_pin.py / feature_detector.py / frame_sampler.py / homography_stability.py / merge.py / point_manager.py，以及 batch-250 的 types.py）` | medium | 垫片 re-export 循环会把实现模块的 `__all__` 与第三方模块别名一并拷入垫片命名空间 |
| AUD-24900 | Suggestion | process | `src/features/color/tracking/tracking_evaluator.py:145-160, 199-204` | medium | 不支持点变换的形状类型静默原样返回，且 `evaluate_geometry` 仍返回非 None |
| AUD-24901 | Suggestion | process | `src/features/color/tracking/tracking_evaluator.py:175, 179-183` | medium | `_enabled` 字典只增不减，绑定删除/替换后条目永久残留 |
| AUD-24902 | Suggestion | perf-opportunity | `src/features/color/tracking/tracking_evaluator.py:123, 132, 150-151, 202` | medium | 每帧求值对基准几何整树 `copy.deepcopy`，播放热路径持续分配 |
| AUD-24903 | Suggestion | process | `src/features/color/tracking/tracking_evaluator.py:45` | high | `capture_base_geometry(shape)` 等参数缺少类型注解 |
| AUD-24904 | Critical | bug | `src/features/color/tracking/tracking_service.py:274-287（配合 tracking_worker.py:54-60 无 parent）` | medium | `_cancel_workers` 超时后丢弃仍在运行的 worker 的最后引用，GC 将析构运行中的 QThread 导致进程 Abort |
| AUD-24905 | Major | bug | `src/features/color/tracking/tracking_service.py:310-315` | medium | 重跟踪失败段时，经合并过滤的「部分结果」被写入全量缓存键，污染缓存 |
| AUD-24906 | Major | bug | `src/features/color/tracking/tracking_service.py:157-173` | medium | 缓存命中路径 `task_finished` 先于 `task_started` 发射，全部方向命中缓存时面板永久卡在 RUNNING |
| AUD-24907 | Major | bug | `src/features/color/tracking/tracking_service.py:143-158（实质缺陷位于 src/shared/tracking/tracking_cache.py:63-75` | medium | 缓存键未包含 `shape_geometry`，形状几何变化不会使缓存失效（根因在 shared 缓存键构造，经本文件使用生效） |
| AUD-24908 | Suggestion | process | `src/features/color/tracking/tracking_service.py:78, 249-254` | medium | `_fingerprint_cache` 无上限增长 |
| AUD-24909 | Suggestion | process | `src/features/color/tracking/tracking_service.py:233-238` | medium | `invalidate` 用 `key.startswith(f"{clip_id}:")` 前缀匹配，clip_id 含冒号时可误伤 |
| AUD-24910 | Suggestion | risk | `src/features/color/tracking/tracking_service.py:324-335` | medium | `_drop_worker` 的 `gen` 参数未使用，`isRunning()` 过滤存在竞态可致 worker 残留 |
| AUD-24911 | Suggestion | perf-opportunity | `src/features/color/tracking/tracking_store.py:50-54` | high | `get_binding` O(n) 线性扫描，位于渲染每帧每形状的求值热路径 |
| AUD-24912 | Suggestion | process | `src/features/color/tracking/tracking_store.py:91-92` | high | `from_dict` 对非数值 `schema_version` 抛出未捕获 ValueError，与单条损坏容忍策略不一致 |
| AUD-24913 | Suggestion | risk | `src/features/color/tracking/tracking_worker.py:26-35` | high | `_CancelToken` 用裸布尔属性实现跨线程取消，建议改用 `threading.Event` |
| AUD-2500 | Major | risk | `src/app/controllers/deliver_controller.py:530-548, 608-609` | medium | Composite 导出 fallback 在 UI 线程直接创建 GL context/纹理执行 GPU 操作，且 context 从不释放、makeCurrent 后无 doneCurrent |
| AUD-2501 | Major | bug | `src/app/controllers/deliver_controller.py:933-934` | medium | 视频导出无防重入：直接覆盖 `_video_exporter` 引用 |
| AUD-2502 | Major | bug | `src/app/controllers/deliver_controller.py:861-865；调用链 export_file_as:1071-1079` | medium | 未知 codec 在 UI 事件处理器中直接 `raise ValueError`，无用户可见反馈 |
| AUD-2503 | Major | risk | `src/app/controllers/deliver_controller.py:936-947, 949-977` | medium | VideoExporter 的进度/成功/失败回调直接操作 QWidget，若在 QThread 中被调用即跨线程 UI 操作 |
| AUD-2504 | Major | bug | `src/app/controllers/deliver_controller.py:1647-1656` | medium | RAW 批量导出白平衡提取失败时静默使用魔术值 [2.0, 1.0, 1.5, 1.0]，`except Exception: pass` 吞掉全部错误 |
| AUD-2505 | Major | risk | `src/app/controllers/deliver_controller.py:1590-1593（调度链 1329-1330 / 1565 的 QTimer.singleShot 均在 UI 线程）` | medium | 批量导出在 UI 线程同步执行 `FileIO.load_image`（含 rawpy RAW 解码），阻塞主线程 |
| AUD-2506 | Major | bug | `src/app/controllers/deliver_controller.py:1771-1790（快照来源 1279-1295）` | medium | 批量导出完成后的状态恢复用 `is not None` 判断，导出前为 None 的字段会残留最后一个批文件的状态 |
| AUD-2507 | Major | bug | `src/app/controllers/deliver_controller.py:151-160, 204-210, 1722-1731（ext 列表缺 '.webp'）；1736-1739 与 1526-1528` | medium | float32 图像导出 `.webp` 缺少 dtype 转换分支，保存失败被误报为渲染失败 |
| AUD-2508 | Major | bug | `src/app/controllers/deliver_controller.py:64-82` | medium | `_encode_composite_linear_to_srgb` 用宽泛 except 把色彩管理意外错误静默降级为 sRGB |
| AUD-2509 | Major | bug | `src/app/controllers/deliver_controller.py:1279-1306` | medium | 批量导出无防重入：二次启动会覆盖引擎状态快照并重置进度 |
| AUD-2510 | Suggestion | process | `src/app/controllers/deliver_controller.py:155, 157, 208, 210, 1726, 1728` | high | 8bit/16bit 量化用截断而非四舍五入，系统性偏暗约 0.5 LSB |
| AUD-25100 | Suggestion | doc-gap | `src/features/color/ui/__init__.py:3-11` | high | 模块 docstring 未列出导出的 ColorMappingPanel |
| AUD-25101 | Suggestion | process | `src/features/color/ui/adjustment_panel.py:75-77` | high | reset_single_param 中 row.reset() 未屏蔽信号，values_changed 双重发射 |
| AUD-25102 | Suggestion | process | `src/features/color/ui/adjustment_panel.py:4, 28-34` | high | I18N 导入未使用，五个参数标签硬编码英文 |
| AUD-25103 | Suggestion | process | `src/features/color/ui/adjustment_panel.py:13-103（` | high | 全部方法缺少类型注解 |
| AUD-25104 | Suggestion | process | `src/features/color/ui/auto_color_dialog.py:40-43, 52-57, 58` | medium | suggestion 参数无类型注解且 items() 结果直接按键访问，无键校验 |
| AUD-25105 | Major | bug | `src/features/color/ui/blur_sharpen_panel.py:469-470, 483-484（对照 317-331）` | medium | set_params_dict 屏蔽信号恢复 mode/blur_mode 时未同步 stack/blur_stack 页面索引，状态恢复后 UI 页面与参数不符 |
| AUD-25106 | Major | bug | `src/features/color/ui/blur_sharpen_panel.py:527, 555` | medium | reset_all/reset_blur 在屏蔽信号下 setCurrentIndex(0)，堆栈页面同样不归位 |
| AUD-25107 | Suggestion | process | `src/features/color/ui/blur_sharpen_panel.py:122-124, 142, 156` | medium | 基础/高级两个锐化滑条初始默认值不同步（config 默认非 0 时高级页显示 0） |
| AUD-25108 | Suggestion | process | `src/features/color/ui/blur_sharpen_panel.py:24-40, 318, 329` | high | self.params 为只写死状态，与实际读取路径脱节 |
| AUD-25109 | Suggestion | process | `src/features/color/ui/blur_sharpen_panel.py:565-568` | medium | resizeEvent 每次触发都 findChildren 全树遍历，与行内建自动显隐逻辑重复 |
| AUD-2511 | Suggestion | process | `src/app/controllers/deliver_controller.py:151-160, 204-210, 1722-1731` | high | dtype/格式转换逻辑在三个导出路径中重复三份 |
| AUD-25110 | Suggestion | process | `src/features/color/ui/blur_sharpen_panel.py:469-470, 483-484` | medium | set_params_dict 对 mode/blur_mode 索引无范围校验即 setCurrentIndex |
| AUD-25111 | Suggestion | process | `src/features/color/ui/blur_sharpen_panel.py:3, 22-48, 50-61, 274-285, 333-357, 359, 443, 511, 538, 565` | medium | 全部方法缺少类型注解；QCheckBox 导入未使用 |
| AUD-2512 | Suggestion | process | `src/app/controllers/deliver_controller.py:1315, 1879` | high | `_restored_queue_settings` 写后从未读取（死状态） |
| AUD-2513 | Suggestion | process | `src/app/controllers/deliver_controller.py:1736-1737（对比 100-102, 183-185）` | medium | 批量导出未注入 `preserve_metadata` 配置，与单文件导出行为不一致 |
| AUD-2514 | Suggestion | process | `src/app/controllers/deliver_controller.py:622-630, 526-528` | high | 两处宽泛 except 静默吞错：`_open_export_folder` 的 `except Exception: pass` 与 render-thread 分支日志缺 traceback |
| AUD-2515 | Suggestion | process | `src/app/controllers/deliver_controller.py:92, 174, 225, 252, 787, 1013, 1083, 1252, 1588` | high | 主要导出入口缺少参数/返回类型注解 |
| AUD-2516 | Suggestion | process | `src/app/controllers/deliver_controller.py:1465-1471` | high | 批量暂停用 `processEvents + sleep(0.01)` 忙等轮询 |
| AUD-2517 | Suggestion | process | `src/app/controllers/deliver_controller.py:1090, 1097, 1112-1117, 1133, 1138, 1142` | high | `export_srt` 全部对话框以 None 为 parent |
| AUD-2518 | Suggestion | process | `src/app/controllers/deliver_controller.py:130-149（panel）/ 190-202（quick）` | medium | composite 导出结果经过 sRGB 编码后再进入 `convert_for_export`，存在双重编码风险，需复核 |
| AUD-25200 | Major | bug | `src/features/color/ui/canvas.py:1701（同 1914）` | medium | 曲线逐点羽化手柄缩放基准 `scene_width` 属性不存在，恒回退 512，手柄距离与引擎实际羽化像素不符 |
| AUD-25201 | Major | bug | `src/features/color/ui/canvas.py:921` | medium | 裁切合法性检查使用被平移边界扩展过的 sceneRect，"限制在画内"约束失效 |
| AUD-25202 | Major | bug | `src/features/color/ui/canvas.py:4972-4973` | medium | RAW 取色把 "Linear" gamma 与 "Gamma 2.2" 同样施加 1/2.2 编码，与引擎 Linear=0（不做 OETF）语义冲突 |
| AUD-25203 | Major | bug | `src/features/color/ui/canvas.py:4984-4991` | medium | 颜色检查器不做人眼坐标→原图坐标缩放映射、不应用 RAW 色彩管理，与两个取色器路径不一致 |
| AUD-25204 | Major | bug | `src/features/color/ui/canvas.py:5768-5775` | medium | `set_crop_params` 使用裸 `except:` 吞掉一切异常并将宽高比静默置为 Free |
| AUD-25205 | Major | bug | `src/features/color/ui/canvas.py:4224-4227（对照 5893-5896、5925）` | medium | `set_tool_mode('eraser')` 只改光标不改 `_brush_params["is_eraser"]`，橡皮擦工具实际画出加色笔触 |
| AUD-25206 | Major | bug | `src/features/color/ui/canvas.py:1560-1567（作废）、1383-1422（重建）、1389（死增量）` | medium | 笔刷覆盖层缓存被每次 `update_from_data` 全量作废并在 paint() 内整幅重建，增量渲染分支为死码 |
| AUD-25207 | Major | bug | `src/features/color/ui/canvas.py:1371-1385` | medium | 笔刷缓存 QImage 在 paint() 内分配且上限 16384×16384（ARGB32 约 1GB），极端场景可致 UI 卡死/内存耗尽 |
| AUD-25208 | Major | risk | `src/features/color/ui/canvas.py:2367-2385（另 2340-2350）` | medium | `sync_menu_state` 程序化 setChecked 无状态锁，且把「覆盖层可见性」与「引擎 matte 预览」两个概念混接 |
| AUD-25209 | Suggestion | process | `src/features/color/ui/canvas.py:2-4, 6-7, 19` | medium | 一批未使用的导入残留 |
| AUD-25210 | Suggestion | process | `src/features/color/ui/canvas.py:3521, 3605, 5490-5505` | medium | 死代码：从未发射的 `stroke_finished` 信号、未用的 `is_panning` 属性、残缺的 `_update_curve_preview` |
| AUD-25211 | Suggestion | process | `src/features/color/ui/canvas.py:968, 139, 1551-1553, 2065` | medium | `MaskShapeItem.rotation` 实例属性遮蔽 QGraphicsItem.rotation() 方法 |
| AUD-25212 | Suggestion | process | `src/features/color/ui/canvas.py:2296-2300` | high | workspace 菜单重建时裸吞 provider 异常且无日志 |
| AUD-25213 | Suggestion | risk | `src/features/color/ui/canvas.py:5898-5909` | medium | `set_brush_params` 对外部驱动的更新也无条件回发 `brush_params_updated`（信号回声），自身无状态锁 |
| AUD-25214 | Suggestion | process | `src/features/color/ui/canvas.py:4702-4715` | medium | `_set_node_type` 未做 handle.index 越界检查（同类方法均有） |
| AUD-25215 | Suggestion | process | `src/features/color/ui/canvas.py:4372-4381` | medium | 快捷键处理不检查修饰键：Ctrl+C 会切换裁切模式 |
| AUD-25216 | Suggestion | process | `src/features/color/ui/canvas.py:5709-5714` | medium | `reset_crop_to_image` 用全新 dict 整体替换 engine.crop_params，可能丢弃引擎侧其他键 |
| AUD-25217 | Suggestion | process | `src/features/color/ui/canvas.py:4871-4907` | medium | `_apply_raw_color_management` 对引擎 RAW 状态与外部 WB 数据无 None/长度防护 |
| AUD-25218 | Suggestion | process | `src/features/color/ui/canvas.py:35（` | medium | 大量公开方法缺少类型注解 |
| AUD-25219 | Suggestion | process | `src/features/color/ui/canvas.py:4250-4253` | high | 检查器分支异常日志无上下文 |
| AUD-25220 | Suggestion | process | `src/features/color/ui/canvas.py:3977-3981` | low | `fit_to_window` 对零尺寸 logical_size 无防护 |
| AUD-25221 | Suggestion | process | `src/features/color/ui/canvas.py:230-245` | medium | 空点击 MaskHandle 也会强制 `_sync_back`，产生无变化的引擎往返 |
| AUD-25300 | Major | bug | `src/features/color/ui/color_status_bar.py:152-159` | high | 降噪档位轮询用宽泛 `except Exception` 静默吞掉 provider 错误 |
| AUD-25301 | Suggestion | perf-opportunity | `src/features/color/ui/color_status_bar.py:138-143` | high | 500ms 轮询定时器无条件常驻，无 provider / 不可见时仍在空转 |
| AUD-25302 | Suggestion | process | `src/features/color/ui/color_status_bar.py:47-47, 147-149` | high | 类型注解缺失：`format_provider_badge` 无返回类型、provider 回调无类型 |
| AUD-25303 | Major | bug | `src/features/color/ui/color_wheel.py:392-403` | medium | `_rgb_to_hsv` 用 clamp(0,1) 截断归一化色度，RGB 数值框回读会折叠饱和度 |
| AUD-25304 | Suggestion | process | `src/features/color/ui/color_wheel.py:95-97` | high | RGB 数值框上限 10.0 会在高亮度/高饱和时截断正向显示 |
| AUD-25305 | Suggestion | process | `src/features/color/ui/color_wheel.py:205-222` | medium | `set_hsv` 未清空/停止每轮 16ms 合并定时器的 pending 值（reset 有做） |
| AUD-25306 | Suggestion | process | `src/features/color/ui/color_wheel.py:284-287, 314-335` | medium | 防递归守卫错位：`_update_rgb_values` 只抬 `_updating_rgb`，但 `_bright_drag` 的处理器检查的是 `_updating_brightness` |
| AUD-25307 | Suggestion | process | `src/features/color/ui/color_wheel.py:499-512, 559-579, 851-851` | medium | 死代码：`_draw_bezel`、`_draw_polar_grid`、`_pending_grading_params` 无任何调用/读取 |
| AUD-25308 | Suggestion | process | `src/features/color/ui/color_wheel.py:200, 1090-1091` | high | 浮点信号用 int 字面量发射、float 参数默认值写 `0` |
| AUD-25309 | Suggestion | process | `src/features/color/ui/color_wheel.py:1230-1245` | medium | `get_current_params` 硬编码 `highlights/sharpness` 为 0，保存路径可能清零他处设置的值 |
| AUD-25310 | Suggestion | process | `src/features/color/ui/color_wheel.py:174-176, 178-203, 205-222, 86-92` | high | 类型注解缺失：`paintEvent/reset/set_hsv/_make_box` 等多处无参数/返回注解 |
| AUD-25311 | Major | bug | `src/features/color/ui/crop_panel.py:382-392, 429-448` | medium | Esc「取消」与 Enter「应用」行为完全相同——取消路径实际提交裁切 |
| AUD-25312 | Suggestion | process | `src/features/color/ui/crop_panel.py:360-363, 336-338` | high | 死处理器 `_on_rotation_changed` 引用不存在的 `self.rot_value_label`，一旦接线即 AttributeError |
| AUD-25313 | Suggestion | process | `src/features/color/ui/crop_panel.py:235-236, 353, 471` | medium | 宽高比预设项 "Free"/"Custom" 未走 i18n，且以英文字面量做相等比较 |
| AUD-25314 | Suggestion | process | `src/features/color/ui/crop_panel.py:344-344, 348-349, 124-124, 168-168` | high | 类型注解缺失：多数事件处理器无参数/返回注解 |
| AUD-25315 | Suggestion | process | `src/features/color/ui/deblur_panel.py:182` | high | 自拼相对路径 `models/Deblur` 与引擎的模型目录知识重复 |
| AUD-25316 | Suggestion | risk | `src/features/color/ui/deblur_panel.py:202-216` | high | `set_params` 用 setCurrentText 静默忽略非法持久化值；blockSignals 只锁了面板自身、未锁子控件 |
| AUD-25317 | Suggestion | process | `src/features/color/ui/deblur_panel.py:63, 84-84, 121-121, 227-228` | high | 混用未本地化文案与裸 list 类型注解 |
| AUD-25318 | Suggestion | process | `src/features/color/ui/color_mapping_panel.py:37-49` | high | 本地手写 QTabBar 样式绕过共享主题常量 TAB_STYLE |
| AUD-25400 | Major | bug | `src/features/color/ui/denoise_panel.py:398` | medium | reset_defaults 硬编码亮度默认 0.05，忽略 ConfigManager 配置默认值 |
| AUD-25401 | Major | bug | `src/features/color/ui/denoise_panel.py:333` | medium | 视频降噪滑杆参数无节流直发，每个 tick 触发渲染管线并重置时域历史 |
| AUD-25402 | Suggestion | process | `src/features/color/ui/denoise_panel.py:441` | high | on_apply_clicked 将 mode==2（视频模式）错误映射为 "AI" |
| AUD-25403 | Suggestion | risk | `src/features/color/ui/denoise_panel.py:471` | medium | set_params 的防递归锁挂在面板自身，无法阻挡子控件信号 |
| AUD-25404 | Suggestion | process | `src/features/color/ui/denoise_panel.py:10` | high | 未使用导入 IconColors |
| AUD-25405 | Suggestion | process | `src/features/color/ui/denoise_panel.py:277` | high | 多个方法缺少类型注解 |
| AUD-25406 | Suggestion | process | `src/features/color/ui/denoise_panel.py:507` | high | resizeEvent 每次全树 findChildren 收集滑杆行 |
| AUD-25407 | Suggestion | process | `src/features/color/ui/film_grain_panel.py:230` | medium | reset_all 未重置启用开关 enable_check |
| AUD-25408 | Suggestion | process | `src/features/color/ui/film_grain_panel.py:4` | high | 未使用导入 Qt、IconColors |
| AUD-25409 | Suggestion | process | `src/features/color/ui/film_grain_panel.py:140` | high | 方法普遍缺少类型注解 |
| AUD-25410 | Suggestion | process | `src/features/color/ui/film_grain_panel.py:257` | high | resizeEvent 每次全树 findChildren 收集滑杆行 |
| AUD-25411 | Major | bug | `src/features/color/ui/gl_viewport.py:183` | medium | show 后延迟渲染无取消/可见性检查，尺寸无效时无限重试 |
| AUD-25412 | Suggestion | process | `src/features/color/ui/gl_viewport.py:28` | high | 死状态变量 `_needs_context_restore` / `_pending_render_after_show` |
| AUD-25413 | Suggestion | perf-opportunity | `src/features/color/ui/gl_viewport.py:127` | high | paintGL 中的父级遍历结果从未使用（每帧热路径死代码） |
| AUD-25414 | Suggestion | process | `src/features/color/ui/gl_viewport.py:145` | medium | showEvent 中 makeCurrent 后未调用 doneCurrent |
| AUD-25415 | Suggestion | process | `src/features/color/ui/gl_viewport.py:40` | medium | initializeGL 的一次性守卫阻止 GL 上下文重建后的再初始化 |
| AUD-25416 | Suggestion | process | `src/features/color/ui/gl_viewport.py:21` | high | 多数方法缺少类型注解 |
| AUD-25417 | Suggestion | process | `src/features/color/ui/grade_paste_dialog.py:46` | low | 未注册类别在对话框中被静默丢弃 |
| AUD-25418 | Suggestion | doc-gap | `src/features/color/ui/hsl_curves_panel.py:78` | medium | `_on_hsl_curve_changed` 为死代码，docstring 宣称的转发信号不存在 |
| AUD-25419 | Suggestion | process | `src/features/color/ui/hsl_curves_panel.py:33` | high | 部分方法缺少类型注解 |
| AUD-25420 | Critical | risk | `src/features/color/ui/layer_panel.py:549` | medium | `_toggle_current_visibility` 访问不存在的属性 `widget._is_visible`，触发即 AttributeError 崩溃 |
| AUD-25421 | Critical | bug | `src/features/color/ui/layer_panel.py:372` | medium | 混合模式下拉以本地化显示名直发引擎，BLEND_MODES 反查失败后静默回退 Normal（渲染错误 + 持久化状态丢失） |
| AUD-25422 | Major | bug | `src/features/color/ui/layer_panel.py:769` | high | update_layers 用引擎侧英文 blend_mode 匹配本地化下拉条目，恢复/换层后下拉框显示旧值 |
| AUD-25423 | Major | bug | `src/features/color/ui/layer_panel.py:139` | medium | 子控件右键菜单坐标以错误的控件为原点映射，菜单位置偏移 |
| AUD-25424 | Major | bug | `src/features/color/ui/layer_panel.py:487` | medium | 底部删除按钮未守护无选择状态，layer_removed(-1) 负索引误删顶层图层并自动保存 |
| AUD-25425 | Suggestion | process | `src/features/color/ui/layer_panel.py:725` | medium | update_layers 的 blockSignals 无 try/finally，异常后面板信号永久失效 |
| AUD-25426 | Suggestion | process | `src/features/color/ui/layer_panel.py:6` | high | 未使用导入 QIcon、QPixmap、QKeySequence、QShortcut、QAction |
| AUD-25427 | Suggestion | process | `src/features/color/ui/layer_panel.py:24` | high | 多数方法缺少类型注解 |
| AUD-25428 | Suggestion | process | `src/features/color/ui/lut_panel.py:27` | high | 代理方法缺少类型注解 |
| AUD-25500 | Major | bug | `src/features/color/ui/mask_panel.py:579` | medium | DragValue 交互信号未转发，遮罩参数拖拽缺少 undo 快照边界 |
| AUD-25501 | Major | bug | `src/features/color/ui/mask_panel.py:662,796-807` | medium | 树重建不保留原选中项并强制选中最后一项，延迟执行还会覆盖控制器的显式选中 |
| AUD-25502 | Suggestion | process | `src/features/color/ui/mask_panel.py:591-602` | medium | 节流待发参数不随选中切换/删除取消，且 `_pending_shape_id` 发射后不清空 |
| AUD-25503 | Suggestion | process | `src/features/color/ui/mask_panel.py:621-640` | medium | 任一控件变化即全量打包提交所有参数（含未同步的变换值） |
| AUD-25504 | Suggestion | process | `src/features/color/ui/mask_panel.py:969,980` | high | reset_all 中的 no-op 样式语句与未配对的 interaction_ended |
| AUD-25505 | Suggestion | process | `src/features/color/ui/mask_panel.py:2-17,83,109,140 等` | medium | 死导入、硬编码英文文案与缺失类型注解 |
| AUD-25506 | Suggestion | process | `src/features/color/ui/mixer_panel.py:266,283（对比 165）` | medium | reset_group/reset_all 直接发射内部 self.params 引用，与节流路径的深拷贝语义不一致 |
| AUD-25507 | Suggestion | process | `src/features/color/ui/mixer_panel.py:54,113,155,229,248,268` | high | 多数方法缺失类型注解 |
| AUD-25508 | Major | risk | `src/features/color/ui/plugin_panel.py:425,585,740` | medium | `_create_param_widget` 污染共享插件元数据 `param['instance_id']`，多实例下 select 回调错路由且泄漏进新实例默认参数 |
| AUD-25509 | Major | bug | `src/features/color/ui/plugin_panel.py:615-643,645-677,605-613（对比 540-552）` | medium | reset/toggle/delete 等离散操作无 interaction 边界，缺少 undo 快照 |
| AUD-25510 | Suggestion | risk | `src/features/color/ui/plugin_panel.py:149-155` | medium | 右键菜单每次打开都在 UI 线程同步重扫插件目录 |
| AUD-25511 | Suggestion | process | `src/features/color/ui/plugin_panel.py:12-15,24-25,35,52,592-601,688-694` | medium | 死代码与未使用导入 |
| AUD-25512 | Suggestion | process | `src/features/color/ui/plugin_panel.py:665-686` | medium | `_delete_plugin` 通过类名嗅探向上访问 MainWindow 并直接刷新 layer_panel，破坏分层解耦 |
| AUD-25513 | Major | bug | `src/features/color/ui/presets_panel.py:553-576（及 583 的 zip 元数据读取）` | medium | PresetDelegate.paint 每次重绘都从磁盘解码缩略图 QPixmap，无任何缓存 |
| AUD-25514 | Major | bug | `src/features/color/ui/presets_panel.py:41-43,49-52` | medium | 用户预设根目录绑定 `os.getcwd()`，换启动目录后预设"丢失" |
| AUD-25515 | Major | bug | `src/features/color/ui/presets_panel.py:344-364,403-428` | medium | create_folder/_rename_preset 未验证用户输入的名称：路径注入与未捕获 OSError |
| AUD-25516 | Suggestion | process | `src/features/color/ui/presets_panel.py:271-279` | high | `_get_preset_meta` 用裸 `except Exception: pass` 掩盖读取错误 |
| AUD-25517 | Suggestion | risk | `src/features/color/ui/presets_panel.py:287-330,509-516` | medium | scan_presets 在 UI 线程同步做逐条磁盘/压缩包 I/O，_export_thumbnail 的 copy2 无异常保护 |
| AUD-25518 | Suggestion | process | `src/features/color/ui/presets_panel.py:1-17,526,666` | high | 未使用导入与缺失类型注解 |
| AUD-25600 | Critical | risk | `src/features/color/ui/qualifier_panel.py:454（关联 :167、:706-711、:742）` | medium | 程序化 set_params/reset 时 invert 复选框 toggled 绕过状态锁，强制把限定器置为 enabled 并提交引擎 |
| AUD-25601 | Suggestion | process | `src/features/color/ui/qualifier_panel.py:407-429` | medium | update_param 的 blockSignals 防护对象错误，实际是无效代码 |
| AUD-25602 | Suggestion | process | `src/features/color/ui/qualifier_panel.py:396、432、458、742` | medium | params_changed 直接 emit 内部可变字典引用 |
| AUD-25603 | Suggestion | process | `src/features/color/ui/qualifier_panel.py:454-458、662-683` | medium | invert 切换与 reset_all 缺少交互生命周期信号，撤销快照边界缺失 |
| AUD-25604 | Suggestion | process | `src/features/color/ui/qualifier_panel.py:29、33、38` | high | 死参数 hue_enabled/sat_enabled/lum_enabled 声明后无任何读写 |
| AUD-25605 | Suggestion | process | `src/features/color/ui/qualifier_panel.py:51、95、327、346、362、398、434-452、706、744、762 等方法无参数/返回注解` | high | 类型注解缺失 |
| AUD-25606 | Suggestion | process | `src/features/color/ui/raw_panel.py:403-407（对照 :217）` | medium | reset_all 用 findText("None") 查找 LUT 空项，但该项显示文本是 i18n 翻译，中文环境永远找不到 |
| AUD-25607 | Suggestion | process | `src/features/color/ui/raw_panel.py:295-312、344-373` | medium | set_params 对外部 params 未验证，combo setCurrentText 不匹配时静默保持旧值并会被后续 emit 反向覆盖 |
| AUD-25608 | Suggestion | process | `src/features/color/ui/raw_panel.py:33-42` | medium | 两个 QTimer 未指定 parent，与 soft_clip_panel 的 QTimer(self) 写法不一致 |
| AUD-25609 | Suggestion | process | `src/features/color/ui/raw_panel.py:47、183、194、213、228、232、239-272、344、375、441 等方法无注解` | high | 类型注解缺失 |
| AUD-25610 | Major | perf-opportunity | `src/features/color/ui/scope_panel.py:986-1017（重点 :1016-1017）` | medium | _present_gpu_frame 整体 except Exception: pass，GPU 呈现错误被静默吞掉，示波器冻结且无任何诊断 |
| AUD-25611 | Suggestion | process | `src/features/color/ui/scope_panel.py:579-582、723-725、769-772、982-984` | high | 其余多处 `except Exception: pass` 无日志 |
| AUD-25612 | Suggestion | process | `src/features/color/ui/scope_panel.py:859、864、874（对照 :830-835 的 ndim==3 防护）` | medium | update_scope 输入 ndarray 未校验形状，部分分支直接访问 image.shape[2]，2D 输入将 IndexError |
| AUD-25613 | Suggestion | process | `src/features/color/ui/scope_panel.py:747-753` | medium | resizeEvent 每个事件都 bump_demand_revision，交互式拖拽窗口期间无节流地强制重提交 GPU scope |
| AUD-25614 | Suggestion | process | `src/features/color/ui/scope_panel.py:894-906（对照 _on_mode_changed :741-745）` | medium | set_mode 程序化切换不回放 _last_image，新模式显示空白直到下一帧 |
| AUD-25615 | Suggestion | process | `src/features/color/ui/scope_panel.py:618-637、926-929` | high | 直接读写子控件私有属性（_heatmap_image/_density/_display_image/_mode/_zebra_threshold），hasattr 掩蔽重构后静默失效 |
| AUD-25616 | Suggestion | process | `src/features/color/ui/soft_clip_panel.py:233（对照 raw_panel.py:228-231 的正确做法）` | high | enable_check.toggled 直连 emit_params，缺交互生命周期信号，撤销快照边界缺失 |
| AUD-25617 | Suggestion | process | `src/features/color/ui/soft_clip_panel.py:262-292、147-158` | medium | 参数范围允许 white_point < high 与 black_point > low，肩/趾压缩被条件静默关闭，无钳制也无提示 |
| AUD-25618 | Suggestion | process | `src/features/color/ui/soft_clip_panel.py:417-436（对照 raw_panel.py:438-439）` | high | reset_all 经 16ms 定时器异步 emit，与 raw_panel 的同步冲刷时序不一致 |
| AUD-25619 | Suggestion | process | `src/features/color/ui/soft_clip_panel.py:195、207、374、385、417、439 等方法无注解` | high | 类型注解缺失 |
| AUD-25700 | Major | bug | `src/features/color/ui/timeline_clip_strip.py:1380` | medium | 播放期缩略图抑制机制从未接线：`set_playback_active` 全库无调用方 |
| AUD-25701 | Major | bug | `src/features/color/ui/timeline_clip_strip.py:954` | high | 卡片时间码硬编码 24 fps，无法适配项目帧率 |
| AUD-25702 | Major | bug | `src/features/color/ui/timeline_clip_strip.py:1148` | medium | `_fit_cards_to_viewport` 在 viewport 高度 ≤ 0 时以 0 ms 定时器无限重试，绕过重试计数 |
| AUD-25703 | Suggestion | process | `src/features/color/ui/timeline_clip_strip.py:1774` | high | `_prime_card_from_thumbnail_cache` 使用宽泛 `except Exception` 静默吞错 |
| AUD-25704 | Suggestion | process | `src/features/color/ui/timeline_clip_strip.py:1066` | high | 多处 UI 文案硬编码英文，未走 I18N |
| AUD-25705 | Suggestion | process | `src/features/color/ui/timeline_clip_strip.py:445` | high | 公共 API 与事件处理器参数缺类型注解 |
| AUD-25706 | Suggestion | process | `src/features/color/ui/timeline_clip_strip.py:314` | high | `_has_meaningful_value` 的 bool/None 分支不可达（bool 是 int 子类） |
| AUD-25707 | Suggestion | process | `src/features/color/ui/timeline_clip_strip.py:21` | high | 未使用的导入 `QPen` |
| AUD-25708 | Major | bug | `src/features/color/ui/tools_panel.py:161` | medium | `lut_panel` 创建代码被注释，但 `init_ui` 仍连接其信号——类一旦实例化立即 AttributeError |
| AUD-25709 | Suggestion | process | `src/features/color/ui/tracker_graph.py:123` | high | 类型注解引用了未导入的 `QPoint` |
| AUD-25710 | Suggestion | process | `src/features/color/ui/tracker_graph.py:273` | medium | 失败帧断线逻辑只断"入线"未断"出线"，断开意图只实现一半 |
| AUD-25711 | Suggestion | process | `src/features/color/ui/tracker_graph.py:458` | high | `TrackerGraph.set_markers` 与 `__init__` 的 parent 参数缺类型注解 |
| AUD-25801 | Major | bug | `src/features/color/ui/tracker_overlay.py:619` | medium | 手动修正拖动过程无任何视觉反馈：`mouseMoveEvent` 计算的新区域是死存储，overlay 状态从未更新 |
| AUD-25802 | Major | risk | `src/features/color/ui/tracker_overlay.py:665-670` | medium | 透视边中点拖动会移动已锁定的角点，锁定语义被绕过 |
| AUD-25803 | Suggestion | process | `src/features/color/ui/tracker_overlay.py:588-602, 356-361` | medium | 拖动被中断时 `_drag`/`_manual_correction_active` 不复位，MANUAL_COLOR 高亮残留 |
| AUD-25804 | Suggestion | process | `src/features/color/ui/tracker_overlay.py:140-144` | low | `transform_point` 对退化单应（w≈0）静默按 w=1 计算，产出无意义远点 |
| AUD-25805 | Suggestion | process | `src/features/color/ui/tracker_overlay.py:262-265, 217` | high | `set_tracking_mode(mode: object)` 弱类型收窄为字符串，`_drag` 无结构注解 |
| AUD-25806 | Major | risk | `src/features/color/ui/tracker_panel.py:1040-1058` | medium | `set_params` 程序化更新控件未加信号锁，违反"状态锁防递归信号"铁律，导致参数处理器双重执行与中间不一致状态外泄 |
| AUD-25807 | Suggestion | process | `src/features/color/ui/tracker_panel.py:989-992` | high | `set_tracking_result` 跨类读取私有属性 `graph.view._reference_frame` |
| AUD-25808 | Suggestion | process | `src/features/color/ui/tracker_panel.py:649-651` | medium | 置信度滑块拖动时每个 tick 都广播 `params_changed`，无合流/防抖 |
| AUD-25809 | Suggestion | perf-opportunity | `src/features/color/ui/tracker_panel.py:926-955` | medium | `set_tracking_binding` 每次全量重建 series 并三重排序，长片段高频刷新时 O(n) 重复开销 |
| AUD-25810 | Major | risk | `src/features/color/ui/tracker_toolbar.py:146-153` | medium | `set_target_names(None)` 必然 TypeError 崩溃：函数体对 falsy 输入自相矛盾 |
| AUD-25811 | Suggestion | process | `src/features/color/ui/tracker_toolbar.py:122-126` | medium | 点击已选中的工具按钮仍重复发射 `tool_changed` |
| AUD-25812 | Suggestion | process | `src/features/color/ui/viewer_guides.py:256-257` | high | 缩略图卡片 `mousePressEvent` 任意按键（右键/中键）都触发选择，且不检查事件内容 |
| AUD-25813 | Suggestion | process | `src/features/color/ui/viewer_guides.py:31, 124-129` | medium | `GuideOverlayItem.paint` 的 option 参数注解 `QStyle.QStyleOptionGraphicsItem` 不是有效类型 |
| AUD-25814 | Major | bug | `src/features/color/ui/warper_panel.py:99-105` | medium | `set_params` 绕过 ColorWarper 的状态复位防护：未停 `_changed_timer`/清 `_pending_deltas`，陈旧拖动样本可在程序化更新后发布并覆盖新状态 |
| AUD-25815 | Suggestion | process | `src/features/color/ui/warper_panel.py:96-110` | high | `set_params` 不校验网格尺寸合法性：非 6/8/12 时 combo 与实际网格脱节；`set_params(None)` 静默无操作 |
| AUD-25816 | Suggestion | process | `src/features/color/ui/warper_panel.py:41, 74-117` | high | 全文件无类型注解，且 reset 按钮提示硬编码英文未走 i18n |
| AUD-25900 | Critical | bug | `src/features/scripting/api.py:119` | medium | RGB 曲线 y 轴方向与 UI/持久化约定相反，曲线经 UI 回读后整体反相 |
| AUD-25901 | Critical | bug | `src/features/scripting/api.py:1136` | medium | batch() 在 output_dir=None 时原地覆盖原始文件，源图不可恢复 |
| AUD-25902 | Major | bug | `src/features/scripting/api.py:781` | medium | apply_lut 相对路径解析到 src/luts/（回溯 3 层），与全项目 luts 根目录不符 |
| AUD-25903 | Major | bug | `src/features/scripting/api.py:122` | medium | 曲线两点路径 clip(0,1) 与多点/PCHIP/Widget 的线性外推不裁切行为不一致 |
| AUD-25904 | Major | bug | `src/features/scripting/api.py:597` | medium | 脚本级调色不使视频缓存渲染参数失效，视频播放期间脚本修改可能被缓存覆盖 |
| AUD-25905 | Suggestion | process | `src/features/scripting/api.py:131` | medium | PchipInterpolator 对重复 x 直接抛 ValueError，无 widget 版的兜底，环境间行为不一致 |
| AUD-25906 | Suggestion | process | `src/features/scripting/api.py:444` | high | 多处 implicit-Optional 类型注解与缺失返回注解 |
| AUD-25907 | Suggestion | process | `src/features/scripting/api.py:1413` | high | capture_still/apply_preset 的名称参数未验证，可写出 Presets 目录之外 |
| AUD-25908 | Suggestion | process | `src/features/scripting/api.py:1374` | medium | _find_main_window 回退分支缓存任意可见顶层控件，后续引擎调用可能 AttributeError |
| AUD-25909 | Critical | risk | `src/features/scripting/engine.py:347` | medium | 脚本抛出 SystemExit/KeyboardInterrupt 逃出 run() 重写：script_finished 不发射，且可能触发 qFatal 整进程崩溃 |
| AUD-25910 | Major | doc-gap | `src/features/scripting/engine.py:62` | medium | call_sync 文档化的 timeout 实际不可达：BlockingQueuedConnection 的 emit 先阻塞至槽完成，等待/超时循环形同虚设 |
| AUD-25911 | Suggestion | process | `src/features/scripting/engine.py:98` | medium | 结果序号按"执行时"的 _seq 标记而非"发起时"，若等待路径被启用存在陈旧结果错配隐患 |
| AUD-25912 | Suggestion | risk | `src/features/scripting/engine.py:117` | high | reset_abort/is_aborted 无锁读写共享 _abort 标志 |
| AUD-25913 | Suggestion | process | `src/features/scripting/engine.py:51` | high | stop() 仅协作式中止，纯 Python 循环中的脚本无法停止 |
| AUD-25914 | Suggestion | process | `src/features/scripting/composite_api.py:170` | high | 兼容回退路径调用页面私有方法，且 _apply_plan 重复解析 page |
| AUD-2600 | Major | bug | `src/app/controllers/dock_layout_controller.py:2076-2082` | medium | 浮动 dock 尺寸/位置在“重新停靠瞬间”被停靠几何覆盖写入 floating_* 配置键 |
| AUD-26001 | Suggestion | process | `src/features/scripting/examples/advanced_workflow.py:3、35` | high | 示例无任何输入验证与错误处理，占位路径必然失败 |
| AUD-26002 | Suggestion | process | `src/features/scripting/examples/basic_grade.py:23 与 src/features/scripting/examples/composite_batch_params.py:24、src/features/scripting/examples/composite_build_graph.py:27-32（对照）` | medium | 示例间控制台输出 API 混用：`topos.print` 与内建 `print` 不一致 |
| AUD-2601 | Major | bug | `src/app/controllers/dock_layout_controller.py:967-1020（成员表）、2183-2185（标题栏更新遍历）、3076-3092（_sync_floating_docks 同样只遍历 all_docks）` | medium | dock_properties 等 5 个 Edit 右侧 dock 未注册进 all_docks，浮动后无法获得可拖拽标题栏 |
| AUD-2602 | Major | bug | `src/app/controllers/dock_layout_controller.py:2064、2074、3047` | medium | 三处裸 `except: pass` 吞掉包括 KeyboardInterrupt/SystemExit 在内的一切异常 |
| AUD-2603 | Major | risk | `src/app/controllers/dock_layout_controller.py:2880-2956（方法整体无 dock_deliver 分支）` | medium | `_enforce_page_dock_visibility` 未覆盖 Deliver dock，Deliver 页专属 dock 可泄漏到其他页面 |
| AUD-2604 | Suggestion | process | `src/app/controllers/dock_layout_controller.py:1613-1617、1762-1766、1860-1864、1948-1952、2002-2009` | medium | Edit/Color 布局迁移写回 page_layouts 只处理扁平形态，mode-scoped 结构被顶层混入页面 hex |
| AUD-2605 | Suggestion | process | `src/app/controllers/dock_layout_controller.py:2020-2034` | medium | reset_product_layout 未清除 legacy 配置桶，且当前无任何调用点 |
| AUD-2606 | Suggestion | process | `src/app/controllers/dock_layout_controller.py:2247-2253（对照 262-266 行）` | medium | apply_default_layout 无条件 setVisible(True)，覆盖 tracker 等面板的“默认隐藏”契约 |
| AUD-2607 | Suggestion | process | `src/app/controllers/dock_layout_controller.py:2119-2124、1084-1087、1477-1480、1697-1701` | medium | 静默异常处理无日志（except Exception 后直接 pass / 赋默认值） |
| AUD-2608 | Suggestion | process | `src/app/controllers/dock_layout_controller.py:1421-1425` | high | resizeDocks 传入未停靠的裸 QWidget 作占位 |
| AUD-2609 | Suggestion | process | `src/app/controllers/dock_layout_controller.py:179（setup_docks）、1069（initialize_page_layouts）、143（_ensure_window_on_screen 的 window 参数）、2047（_on_dock_floating_changed）、2132、2169、2238、2339、2354、2370、2387、2405、2426、2439、2454、2591、2804、2880、3007、3022、3067 等；2488 行` | medium | 大量方法缺类型注解 |
| AUD-2610 | Suggestion | perf-opportunity | `src/app/controllers/dock_layout_controller.py:2183、2081-2082` | medium | 性能小项：getattr 默认值无条件执行 findChildren；每次停靠触发两次全量写盘 |
| AUD-26101 | Suggestion | process | `src/features/scripting/module.py:9、13` | high | 工厂方法与模块级函数缺参数/返回类型注解 |
| AUD-2611 | Suggestion | process | `src/app/controllers/dock_layout_controller.py:1111-1114、1155-1158、1192-1194、1434-1436（另 1421-1452 的 composite 段）` | medium | 四段近乎相同的“隐藏页面专属 dock”列表重复且彼此漂移 |
| AUD-2612 | Suggestion | process | `src/app/controllers/dock_layout_controller.py:2827、2834（另 1182、2365 同型）` | high | restoreState 返回值被忽略，失败无日志无回退 |
| AUD-26300 | Suggestion | process | `src/features/shared/effects/__init__.py:15-30, 55-95` | medium | `EffectExecutionCapability` 未随 `resolve_execution_capability` 一并 re-export |
| AUD-26301 | Suggestion | process | `src/features/shared/effects/dctl_wrapper.py:90-93, 118-124` | medium | uniform 名 `u_{var_name}` 未做 GLSL 标识符/保留名冲突校验 |
| AUD-26302 | Suggestion | test-gap | `src/features/shared/effects/dctl_wrapper.py:136-141` | medium | `default_uniform_values` 文档声称由 EffectCatalog 使用，实际仅测试引用 |
| AUD-26303 | Suggestion | process | `src/features/shared/effects/effect_capability.py:1-2` | high | 文件头 DORMANT 横幅"暂无生产入口"与实际生产接线矛盾 |
| AUD-26304 | Suggestion | process | `src/features/shared/effects/effect_capability.py:520-530` | medium | `resolve_execution_capability` 内的宽泛 `except Exception` 无日志地吞掉能力求证错误 |
| AUD-26305 | Suggestion | process | `src/features/shared/effects/effect_capability.py:161, 168, 200-205` | medium | `resolve_capability` 在 `has_gpu=False` 时丢弃显式声明的后端集合，CPU-only 效果会被 `check_backend_support` 误判 UNSUPPORTED |
| AUD-26306 | Suggestion | process | `src/features/shared/effects/effect_capability.py:426-427, 536` | high | `EffectExecutionCapability` 的容器字段用裸 `tuple`/`set` 注解，丢失元素类型契约 |
| AUD-26307 | Major | bug | `src/features/shared/effects/effect_catalog.py:552-558` | medium | `@export_support` 声明值未校验：任意非空/非 none 令牌都会被授予 `export_opengl` 能力并进入 GL 导出链 |
| AUD-26308 | Major | bug | `src/features/shared/effects/effect_catalog.py:482-499` | medium | 插件目录扫描失败被宽泛 except 静默吞掉，`exc` 捕获后未使用且无日志 |
| AUD-26309 | Suggestion | process | `src/features/shared/effects/effect_catalog.py:518; src/features/shared/effects/effect_spec.py:127` | medium | `preview_cost` 存入 `PreviewQualityLevel` 枚举，违反 `EffectSpec.preview_cost: str` 契约 |
| AUD-26310 | Suggestion | process | `src/features/shared/effects/effect_catalog.py:655-666` | medium | 同 `effect_type` 重新注册时类别变更会留下陈旧的 `_by_category` 索引 |
| AUD-26311 | Suggestion | process | `src/features/shared/effects/effect_catalog.py:416` | high | `_dctl_param_to_pdef` 导入私有名 `_PARAM_GLTYPE` 且从未使用（死导入） |
| AUD-26312 | Suggestion | doc-gap | `src/features/shared/effects/effect_catalog.py:631-653, 707-712` | medium | `_ensure_built` 持锁期间执行插件目录磁盘扫描；`is_known` 文档称"cheap"但首次调用触发全量构建 |
| AUD-26401 | Critical | bug | `src/features/shared/effects/effect_pass_runner.py:1049-1058（V pass）；同缺陷亦见 H pass 1037-1040` | medium | `_run_separable` 的 H/V pass 写入目标固定为 scratch_b/scratch_a 且 V pass 将输入绑到 unit 1，链式调用时读写同一纹理形成 GL 反馈回路 |
| AUD-26402 | Critical | bug | `src/features/shared/effects/effect_pass_runner.py:1232-1237（对照防护代码 1227-1230）` | medium | `_run_glow` pass 1 固定写 scratch_b，输入为 scratch_b 时读写同一纹理形成反馈回路（FX-M02 只修了 pass 2） |
| AUD-26403 | Suggestion | process | `src/features/shared/effects/effect_pass_runner.py:1013-1014（触发入口 493-494）` | medium | 非 blur 的 SEPARABLE_PASSES spec 直接进 `_run_separable` 会在 `packed.pop("u_radius_x")` 处 KeyError，被顶层 catch-all 吞成静默 no-op |
| AUD-26404 | Suggestion | process | `src/features/shared/effects/effect_pass_runner.py:785-792` | medium | `_bind_external_texture` 每次调用都新建 `QOpenGLFunctions_4_1_Core` 并重跑 `initializeOpenGLFunctions` |
| AUD-26405 | Suggestion | risk | `src/features/shared/effects/effect_pass_runner.py:1359-1367（combine 同型 1387-1395）` | medium | `_ensure_blit_program` / `_ensure_combine_program` 中途失败时已创建的 program/vbo 泄漏 |
| AUD-26406 | Suggestion | process | `src/features/shared/effects/effect_pass_runner.py:496-501` | medium | `run()` 顶层 `except Exception` 将参数打包等领域错误与着色器错误一并降级为 no-op |
| AUD-26407 | Suggestion | process | `src/features/shared/effects/effect_spec.py:121-123（类装饰器 65）` | medium | `EffectSpec` 为 frozen dataclass 但持有三个可变 dict 字段，且 `hash(spec)` 会因 dict 字段抛 TypeError |
| AUD-26408 | Major | bug | `src/features/shared/effects/plugin_identity.py:138-147（调用点 340）` | medium | `PluginIdentity.from_dict` 不验证反序列化数据类型，损坏的项目文件可使 `resolve_plugin_status` 抛异常，违反其 "never raises" 契约 |
| AUD-26409 | Suggestion | process | `src/features/shared/effects/plugin_identity.py:415-420（重建 451-465）` | medium | `validate_effect_instances` 的 `catalog_lookup: Any` 类型过宽，且用重建 report 的方式回填 effect_id |
| AUD-26410 | Major | bug | `src/features/shared/effects/preview_policy.py:343-351` | medium | `_pick_degradation` 的 PARAMETER_CAP 分支在参数已达标时提前 return FULL，永远不会尝试该效果声明的 LOWER_RESOLUTION / BYPASS |
| AUD-26411 | Suggestion | process | `src/features/shared/effects/preview_policy.py:214-216` | medium | `register_degradation_decl` 无保护地改写模块级 `_BUILTIN_DECLS`，可覆盖内置声明且无并发防护 |
| AUD-26600 | Major | bug | `src/features/teaching_animation/ai/__init__.py:491-504（另见 446、133-135）` | medium | `__all__` 含 40 个未定义名称：已删除的 job_dag / lane_scheduler 模块导出残留 + 未导入的 `TeachingAIContextProvider` |
| AUD-26601 | Suggestion | process | `src/features/teaching_animation/ai/action_matrix.py:534-554` | medium | `evaluate_plan` 对 `Iterable` 二次消费，生成器输入时返回占位元数据 |
| AUD-26602 | Suggestion | process | `src/features/teaching_animation/ai/action_matrix.py:345-347, 374-379` | medium | `default_action_matrix()` 声称 immutable，但共享实例的 `profiles` 字典可被就地注入/篡改 |
| AUD-26603 | Major | bug | `src/features/teaching_animation/ai/advanced_capabilities.py:225-228` | medium | `compatibility_issues` 的 codec 条件逻辑近似失效，`export_backends` 白名单对编码从不生效 |
| AUD-26604 | Suggestion | process | `src/features/teaching_animation/ai/approval_scope.py:316-327` | medium | `validate()` 用宽泛 `except Exception` 吞掉编程错误，与 .clinerules"不得宽泛 except"铁律冲突 |
| AUD-26605 | Suggestion | process | `src/features/teaching_animation/ai/approval_scope.py:231` | high | `time_fn: Any = None` 类型注解过宽，丢失可调用签名契约 |
| AUD-26606 | Suggestion | process | `src/features/teaching_animation/ai/artifact_links.py:64-76, 207-215` | medium | `label` 参与 `ArtifactRef` 相等性/哈希，同一对象的索引集合会出现重复节点 |
| AUD-26607 | Suggestion | process | `src/features/teaching_animation/ai/asset_provenance.py:110-128` | medium | `from_dict` 对外部 JSON 字段只做部分类型收敛，provider/model_id/哈希字段原样透传 |
| AUD-26608 | Major | bug | `src/features/teaching_animation/ai/audio_plan.py:57, 180-181` | medium | `issues()` 音频许可检查只挡 `"missing"`，与 quality_gate/asset_provenance 的许可口径不一致 |
| AUD-26609 | Suggestion | process | `src/features/teaching_animation/ai/audio_plan.py:86-99, 102-119` | medium | `BeatMarker` 与 `SubtitleMapping` 完全缺失 `__post_init__` 输入验证 |
| AUD-26700 | Suggestion | risk | `src/features/teaching_animation/ai/brief.py:338` | medium | from_dict / apply_patch 对数值字段不做类型归一，畸形外部数据以 TypeError 崩溃而非校验错误 |
| AUD-26701 | Suggestion | process | `src/features/teaching_animation/ai/brief.py:435` | high | 模块级死常量 `_ALLOWED_TYPES` 无任何引用 |
| AUD-26702 | Major | bug | `src/features/teaching_animation/ai/capability_context.py:99` | medium | 能力清单（模型契约）与实际编译器参数名大面积漂移，清单宣称的多条命令无法被编译 |
| AUD-26703 | Major | bug | `src/features/teaching_animation/ai/command_catalog.py:612` | medium | stagger_elements 的 `or` 默认值写法把显式 0 值静默替换为默认值（interval=0→12，duration=0→18 并绕过正值校验） |
| AUD-26704 | Major | bug | `src/features/teaching_animation/ai/command_catalog.py:770` | medium | insert_keyframe 在已占用帧上的 undo 不恢复原关键帧，反而把该帧关键帧整个删除（undo 路径数据丢失） |
| AUD-26705 | Major | bug | `src/features/teaching_animation/ai/command_catalog.py:449` | medium | set_color_token 匹配 `var(token)`、set_typo_token 匹配 `font_token`——两种约定在全仓库无任何生产者，两条已广告命令永远失败 |
| AUD-26706 | Suggestion | process | `src/features/teaching_animation/ai/command_catalog.py:135` | high | MAX_DURATION_FRAMES 注释与实际值不符：注释称“60fps 下 10 分钟”，实值 216000 帧 = 60fps 下 60 分钟 |
| AUD-26707 | Suggestion | process | `src/features/teaching_animation/ai/command_catalog.py:804` | medium | 自定义命令类在 apply 路径抛裸 ValueError，绕开 TeachingAIProposalError 错误契约 |
| AUD-26708 | Suggestion | doc-gap | `src/features/teaching_animation/ai/command_catalog.py:313` | medium | set_background / add_audio_ref 接受任意 Mapping 直通场景文档，set_brand_theme 对非法类别静默降级为空 dict |
| AUD-26709 | Suggestion | process | `src/features/teaching_animation/ai/commands.py:91` | medium | set_animation 关键帧解析抛裸 KeyError/TypeError/ValueError，且帧号不做场景范围校验 |
| AUD-26710 | Suggestion | process | `src/features/teaching_animation/ai/commands.py:79` | high | reorder_element 编译期不预检元素存在与索引范围，错误以裸 ValueError/IndexError 在 apply 阶段逃逸 |
| AUD-26711 | Suggestion | process | `src/features/teaching_animation/ai/context_collector.py:253` | medium | collect 对宿主传入条目直接 dict() 无 Mapping 预检，数值字段透传不校验 |
| AUD-26712 | Suggestion | process | `src/features/teaching_animation/ai/context_collector.py:226` | high | time_fn 注解为 Any，丢失可调用签名检查 |
| AUD-26713 | Major | bug | `src/features/teaching_animation/ai/context_provider.py:43` | medium | 两处 `except Exception: pass` 无日志吞掉第一方注册表构建的全部异常，掩盖领域错误 |
| AUD-26714 | Suggestion | doc-gap | `src/features/teaching_animation/ai/context_provider.py:22` | medium | `_ai_capability_catalog` 的 lru_cache 使能力面在进程内永久冻结，与“注册表变化直达 prompt”的文档承诺不符 |
| AUD-26715 | Suggestion | process | `src/features/teaching_animation/ai/creative_rubric.py:174` | medium | readability/hierarchy 直接 `el.style.get(...)` 未守卫 None style，与同文件 brand 维度的守卫不一致 |
| AUD-26716 | Suggestion | process | `src/features/teaching_animation/ai/creative_rubric.py:240` | medium | brand 维度以 `$` 前缀统计 token 引用，但模板 style 在构建期已解析为具体值，该分支近乎恒不命中 |
| AUD-26717 | Suggestion | process | `src/features/teaching_animation/ai/creative_rubric.py:195` | high | rhythm 维度硬编码 24fps 折算事件密度，非 24fps 场景误判 |
| AUD-26800 | Major | bug | `src/features/teaching_animation/ai/creative_templates.py:132, 278-303, 384` | medium | `supports_4k` 能力声明永远无法被触发——兼容性检查根本没有分辨率参数 |
| AUD-26801 | Suggestion | process | `src/features/teaching_animation/ai/creative_templates.py:25` | high | `Iterable` 导入未使用 |
| AUD-26802 | Suggestion | doc-gap | `src/features/teaching_animation/ai/delivery_profile.py:264-280` | high | Registry 文档承诺 `version_of` 保留旧版本，方法不存在且 register 实为整体替换 |
| AUD-26803 | Suggestion | process | `src/features/teaching_animation/ai/delivery_profile.py:167-170` | medium | `width/height` 与 `canvas_aspect` 标签无一致性校验 |
| AUD-26804 | Suggestion | process | `src/features/teaching_animation/ai/delivery_profile.py:184-185` | high | `duration_frames` 对非正时长静默映射为 1 帧 |
| AUD-26805 | Major | bug | `src/features/teaching_animation/ai/design_validator.py:633-651（L635、L645 两处）` | medium | 素材校验复用 `MOTION_GRAPHIC_CANVAS_OUT_OF_BOUNDS` 而非专用的 `MOTION_GRAPHIC_ASSET_UNRESOLVED` |
| AUD-26806 | Major | doc-gap | `src/features/teaching_animation/ai/design_validator.py:20-21, 371, 424-427` | medium | 对比度检查实现与文档承诺不符：只对比场景背景色，而非"文字背后的元素" |
| AUD-26807 | Suggestion | process | `src/features/teaching_animation/ai/design_validator.py:684-686, 757-758` | high | 时长不一致与文本重叠 finding 复用 `MOTION_GRAPHIC_TEXT_OVERFLOW` 错误码 |
| AUD-26808 | Suggestion | process | `src/features/teaching_animation/ai/design_validator.py:723-725` | medium | 顶层 `int()` 直接转换外部场景字段，验证器自身可抛未捕获异常 |
| AUD-26809 | Suggestion | perf-opportunity | `src/features/teaching_animation/ai/design_validator.py:680-684, 478-495, 743-749` | medium | `_check_overlap` O(n²) 且不因元素超限提前截断 |
| AUD-26810 | Suggestion | process | `src/features/teaching_animation/ai/design_validator.py:602-603` | high | 场景无元素（或元素全无 id）时悬空动画引用不被报告 |
| AUD-26811 | Suggestion | process | `src/features/teaching_animation/ai/design_validator.py:37, 39` | high | 未使用导入 `SUPPORTED_ELEMENT_TYPES`、`TeachingSceneDocument`、`Iterable` |
| AUD-26812 | Suggestion | risk | `src/features/teaching_animation/ai/error_codes.py:314-315, 324` | medium | `MotionGraphicAuditEvent.from_dict` 防御不一致：timestamp 为 None 会崩溃，brief_revision 不做类型收敛 |
| AUD-26813 | Suggestion | process | `src/features/teaching_animation/ai/eval_rubric.py:76-77, 97-101` | medium | `weight=NaN` 通过校验，`weighted_score` 变为 NaN |
| AUD-26814 | Major | risk | `src/features/teaching_animation/ai/export_job.py:200-216, 218-223` | medium | 取消/质量阻断/覆盖被拒路径均不清理临时文件，也无任何清理 API——.tmp 大文件泄漏 |
| AUD-26815 | Major | bug | `src/features/teaching_animation/ai/export_job.py:196-216` | medium | `finish()` 内 `os.makedirs`/`os.replace` 的 OSError 逃逸，任务停留在不一致状态 |
| AUD-26816 | Suggestion | risk | `src/features/teaching_animation/ai/export_job.py:209-215` | medium | 覆盖审批检查与 `os.replace` 之间存在 TOCTOU 竞态 |
| AUD-26817 | Suggestion | process | `src/features/teaching_animation/ai/export_job.py:58-74` | medium | `ExportRequest` 无输入验证，绕过 MG-11 的 codec/container 闭集词汇 |
| AUD-26818 | Suggestion | process | `src/features/teaching_animation/ai/export_job.py:167-172, 258, 288, 296` | high | `start()` 失败借用语义无关的 `MOTION_GRAPHIC_CLIP_INSERT_FAILED`；`EXPORT_*` 码为散落字符串未入词汇表 |
| AUD-26819 | Suggestion | risk | `src/features/teaching_animation/ai/export_job.py:29, 161-163` | high | 未使用导入 `Callable`/`Iterable`；`is_terminal` 属性读取未持锁 |
| AUD-26820 | Suggestion | process | `src/features/teaching_animation/ai/fallback.py:173-188` | medium | 用户取消被归类为 `FALLBACK_PROVIDER_UNAVAILABLE` 且 `allows_template=True`；错误消息文本嗅探脆弱 |
| AUD-26821 | Suggestion | process | `src/features/teaching_animation/ai/fallback.py:54-55` | high | `FallbackState` str 子类为死代码 |
| AUD-26900 | Suggestion | process | `src/features/teaching_animation/ai/html_design_quality.py:142-147` | medium | `_page_structure_issues` 的宽泛 except 会把解析器自身缺陷伪装成"模型 HTML 结构问题" |
| AUD-26901 | Suggestion | process | `src/features/teaching_animation/ai/html_edit_audit.py:54-78` | medium | `changed` 与 `summary()` 语义不一致：仅有 `html_size_delta` 变化时判定"已改变"但摘要说"无变化" |
| AUD-26902 | Major | bug | `src/features/teaching_animation/ai/html_edit_autonomy.py:222-223, 410-419` | medium | 增/删页后置条件正则对常见中文措辞双向误判：把"添加页码/删除页面底部…"当成增删页，而真正的"删除第2页"反而漏判 |
| AUD-26903 | Major | bug | `src/features/teaching_animation/ai/html_edit_autonomy.py:224-226, 394-404` | medium | `_REMOVE_QUOTED_RE` 把"不要删除「X」"及"删掉A，改成「B」"中的 X/B 误判为待删文案 |
| AUD-26904 | Suggestion | process | `src/features/teaching_animation/ai/html_edit_autonomy.py:108, 615-620` | medium | `page_texts` 截断 100 字符导致 `explicit_noop_reason` 对长文案漏判 no-op |
| AUD-26905 | Suggestion | process | `src/features/teaching_animation/ai/html_generation_protocol.py:253-263` | medium | STATIC_ANALYSIS 阶段对 `evaluate_html_design` 的 `except Exception` 静默置空结构诊断 |
| AUD-26906 | Suggestion | process | `src/features/teaching_animation/ai/html_generation_protocol.py:431-494` | medium | `_run_preview` 捕获循环（2 + N 页次，每次 timeout 20s）无取消点 |
| AUD-26907 | Suggestion | process | `src/features/teaching_animation/ai/html_generation_protocol.py:486-490` | medium | `page_count == 1` 时跳过 near-blank 检查，全白单页 deck 可确定性通过预览 |
| AUD-26908 | Suggestion | process | `src/features/teaching_animation/ai/html_generation_protocol.py:265-303` | medium | render-only 模式（`post_generation_quality_gate=False`）连 `check_unsafe_content` 固定点复扫也一并跳过 |
| AUD-26909 | Suggestion | process | `src/features/teaching_animation/ai/html_generation_protocol.py:357-365` | high | `_run_preview` 返回类型注解中 `list` 未参数化 |
| AUD-26910 | Major | bug | `src/features/teaching_animation/ai/html_llm_generator.py:225-230` | medium | `strip_markdown_fence` 用 `rfind("```")` 无条件截断，正文含代码围栏的教学页面被静默腰斩 |
| AUD-26911 | Suggestion | process | `src/features/teaching_animation/ai/html_llm_generator.py:170-176` | medium | 插件 prompt 加载失败被 `except (ImportError, AttributeError)` 静默吞掉 |
| AUD-26912 | Suggestion | process | `src/features/teaching_animation/ai/html_quality_gate.py:59, 144-152` | medium | `_FIXED_WIDTH_RE` 无左边界，`min-width:`/`max-width:` 会被当作固定宽度误报 |
| AUD-26913 | Suggestion | process | `src/features/teaching_animation/ai/html_quality_gate.py:62-66, 165-176` | medium | `check_flicker` 漏掉 ms 单位动画时长，且跨元素混合全部 opacity 声明计算 swing |
| AUD-26914 | Suggestion | process | `src/features/teaching_animation/ai/html_quality_gate.py:96-110` | medium | 残留危险 token 扫描对"可见文本中字面出现"误报（教学/代码示例内容会被判 ERROR 阻断） |
| AUD-26915 | Suggestion | process | `src/features/teaching_animation/ai/html_quality_gate.py:97-101` | high | 通过 `getattr` 探测 `HtmlSanitizer` 私有属性 `_allow_*`，脆弱的跨模块私有耦合 |
| AUD-2700 | Critical | bug | `src/app/controllers/edit_controller.py:6888-6893` | medium | Bypass Composite 处理函数引用全库不存在的属性/方法，右键菜单触发即 AttributeError |
| AUD-27000 | Suggestion | process | `src/features/teaching_animation/ai/preview.py:276` | high | sample_storyboard_frames 计算 fps_value 后从未使用，fps 参数为死参数 |
| AUD-27001 | Suggestion | doc-gap | `src/features/teaching_animation/ai/preview.py:136` | medium | SceneDiffSummary 文档称 "hashable"，实际 hash() 会抛 TypeError |
| AUD-27002 | Suggestion | process | `src/features/teaching_animation/ai/proposal.py:147` | high | NarrativeBeat.from_dict 的 asset_refs 未做 str 强转，违反 tuple[str, ...] 契约 |
| AUD-27003 | Suggestion | process | `src/features/teaching_animation/ai/proposal_card.py:28` | medium | 模块级未使用导入：json 与四个 FALLBACK_* 常量 |
| AUD-27004 | Suggestion | process | `src/features/teaching_animation/ai/proposal_card.py:351` | medium | _canvas_aspect_from_scene 对不匹配比例一律返回默认值，且竖屏分支存在死代码 |
| AUD-27005 | Suggestion | process | `src/features/teaching_animation/ai/provider_registry.py:16` | medium | 描述符默认 command_registry 为类级共享实例，未来加状态即成跨实例串改 |
| AUD-27006 | Major | bug | `src/features/teaching_animation/ai/quality_gate.py:128` | medium | has_transparency 输入被静默忽略，承诺的透明度检查从未实现 |
| AUD-27007 | Suggestion | process | `src/features/teaching_animation/ai/quality_gate.py:286` | high | checks_skipped 永远为空，未实现/未触发的检查无法与"通过"区分 |
| AUD-27008 | Suggestion | process | `src/features/teaching_animation/ai/quality_gate.py:226` | high | 非渲染类失败全部复用 MOTION_GRAPHIC_RENDER_FAILED 错误码 |
| AUD-27009 | Suggestion | process | `src/features/teaching_animation/ai/quality_gate.py:24` | high | 三个错误码导入未使用（CANVAS_OUT_OF_BOUNDS/LOW_CONTRAST/TEXT_OVERFLOW） |
| AUD-2701 | Critical | bug | `src/app/controllers/edit_controller.py:3696-3713（` | medium | Solid 生成器参数修改用手工构造函数重建 EditProject，静默丢弃 smart_bins 并将 revision 归零 |
| AUD-27010 | Major | bug | `src/features/teaching_animation/ai/revision.py:386` | medium | accept() 仅拦截 REJECTED，终态 SUPERSEDED/FAILED 版本可被接受，违反自述不变量 |
| AUD-27011 | Suggestion | process | `src/features/teaching_animation/ai/revision.py:348` | medium | attach_plan 不校验 plan 与 revision 的一致性（proposal_ref/brief_hash） |
| AUD-27012 | Suggestion | risk | `src/features/teaching_animation/ai/revision.py:306` | medium | MotionGraphicRevisionLog 无锁，add_feedback 的 latest 检查为 check-then-act |
| AUD-27013 | Suggestion | process | `src/features/teaching_animation/ai/revision.py:438` | medium | mark_failed 无状态守卫，可将 ACCEPTED 版本静默翻转为 FAILED |
| AUD-27014 | Major | perf-opportunity | `src/features/teaching_animation/ai/rollout.py:217` | medium | kill switch 通过 object.__setattr__ 原地篡改冻结的 RolloutPolicy，共享策略对象时波及所有控制器 |
| AUD-27015 | Suggestion | process | `src/features/teaching_animation/ai/rollout.py:169` | medium | cohort_allows 对未知 cohort 抛未类型化的 ValueError，权限判定路径可被外部数据打崩 |
| AUD-27016 | Suggestion | process | `src/features/teaching_animation/ai/rollout.py:197` | high | incidents/feedback 列表无上限增长，data_retention_days 从未被强制执行 |
| AUD-27017 | Suggestion | process | `src/features/teaching_animation/ai/rollout.py:307` | high | runbook 触发条件写"最近 20 次"，satisfaction_average 却是全量平均 |
| AUD-27018 | Critical | bug | `src/features/teaching_animation/ai/service.py:75` | medium | replace_clip 在命令栈外预删除原 clip，undo 后原 clip 永久丢失（数据丢失） |
| AUD-27019 | Major | doc-gap | `src/features/teaching_animation/ai/service.py:192` | medium | apply_html_layer/apply_html_template 直接 apply 后丢弃 SetHtmlLayerCommand，撤销状态丢失，与"undoable and recorded"的文档承诺不符 |
| AUD-2702 | Critical | bug | `src/app/controllers/edit_controller.py:3676-3692；3749-3765` | medium | 生成器重建 MediaAsset 时丢弃 keywords/rating/flag/color_label/comment 日志字段 |
| AUD-27020 | Suggestion | process | `src/features/teaching_animation/ai/service.py:53` | medium | render_preview 中 renderer.prepare 位于 try/finally 之外，prepare 失败不清理 |
| AUD-2703 | Critical | bug | `src/app/controllers/edit_controller.py:9849-9857` | medium | 片段 Transform 拖拽结束的提交命令在真实运行环境中永不执行，编辑结果可随保存丢失 |
| AUD-2704 | Major | bug | `src/app/controllers/edit_controller.py:1759-1806` | medium | `_on_grading_changed_for_color_stack` 中 `trackings`/`video_denoise` 仅在 `engine.layers` 非空时赋值，空层时 UnboundLocalError |
| AUD-2705 | Major | bug | `src/app/controllers/edit_controller.py:8575-8577（对照 4068-4078）` | medium | 标题模板 fallback 分支未解包 `MacroCommand.apply` 的 `(timeline, macro)` 二元组，`_current_timeline` 变成 tuple |
| AUD-2706 | Major | risk | `src/app/controllers/edit_controller.py:7867-7876（启动/登记）、7953-7955 与 8022-8024（完成/失败时` | medium | RenderCacheWorker 无 parent、结束时直接删除最后引用，存在 QThread 运行中被析构的崩溃竞态 |
| AUD-2707 | Major | bug | `src/app/controllers/edit_controller.py:2130-2136、3537-3540、3886-3891、3958-3964、4064-4079、4170-4175、4257-4263、4321-4327、4386-4391、4528-4533、4609-4615（共 12 处` | medium | 属性面板的剪辑参数/效果/转场命令在 EditPage 无 `_command_stack` 时全部走直接 apply，绕过 undo 栈 |
| AUD-2708 | Major | bug | `src/app/controllers/edit_controller.py:1491-1494 + 1503-1517 + 1534-1539（` | medium | 调色栈创建（可撤销）与 clip 绑定/scope 盖章（直接改）非原子，undo 后 clip 悬空引用已删除栈 |
| AUD-2709 | Suggestion | process | `src/app/controllers/edit_controller.py:5591-5593；8986-8988` | high | `hasattr(fps, 'to_fps')` 死代码：fps 已被 `float()` 转换，恒无 `to_fps` 属性 |
| AUD-2710 | Suggestion | process | `src/app/controllers/edit_controller.py:8493-8496（另 8483 后未调` | medium | 占位片段 fallback 轨道解析可抛 KeyError/StopIteration；新素材未刷新媒体池 |
| AUD-27100 | Major | risk | `src/features/teaching_animation/ai/session.py:30-37, 46-50` | medium | 全局会话注册表无锁，get_or_create 存在 check-then-act 竞态 |
| AUD-27101 | Suggestion | process | `src/features/teaching_animation/ai/session.py:35-36` | high | get_or_create 静默覆盖已有会话的 context_provider |
| AUD-27102 | Suggestion | process | `src/features/teaching_animation/ai/session.py:42-43` | medium | close_project 不通知已 attach 的 surface |
| AUD-27103 | Major | bug | `src/features/teaching_animation/ai/session_store.py:244-258（重点 257 行）` | medium | upsert 更新分支：status 缺省回落 "draft" 静默重置状态，且具名参数被忽略 |
| AUD-27104 | Major | bug | `src/features/teaching_animation/ai/session_store.py:260-261, 264-269, 354-363` | medium | 磁盘写失败时内存已提交，异常路径造成内存/磁盘状态分叉且无回滚 |
| AUD-27105 | Major | risk | `src/features/teaching_animation/ai/session_store.py:96-97, 103, 112-113, 341` | medium | reload 对外部 JSON 无防御：scope 未知键/缺字段、数值转换异常直接崩溃 |
| AUD-27106 | Major | bug | `src/features/teaching_animation/ai/session_store.py:96-97（写侧对照 approval_scope.py:182-183，受害对照 approval_scope.py:165）` | medium | grant_scope 反序列化不还原类型：allowed_actions 变纯 str、asset_ids 变 list |
| AUD-27107 | Suggestion | process | `src/features/teaching_animation/ai/session_store.py:201-212, 70, 114` | medium | upsert 不校验 project_id 归属与 status 取值 |
| AUD-27108 | Suggestion | process | `src/features/teaching_animation/ai/session_store.py:360-363` | medium | 临时文件固定名、无 fsync、写失败残留 |
| AUD-27109 | Suggestion | process | `src/features/teaching_animation/ai/session_store.py:174` | high | time_fn 类型注解为 Any |
| AUD-2711 | Suggestion | process | `src/app/controllers/edit_controller.py:6088-6094` | high | `_save_current_project` 临时切换 current_page 无 try/finally，save 抛异常时页面状态不恢复 |
| AUD-27110 | Major | bug | `src/features/teaching_animation/ai/slo_benchmark.py:122-124, 162-164` | medium | 空报告 all_meet_targets 恒为 True（空真），CI 门可假绿 |
| AUD-27111 | Suggestion | process | `src/features/teaching_animation/ai/slo_benchmark.py:156-159` | high | 未登记 slo_id 的样本被静默丢弃 |
| AUD-27112 | Suggestion | process | `src/features/teaching_animation/ai/slo_benchmark.py:169-175` | medium | 退化检测仅基于 p50，忽略 p95 |
| AUD-27113 | Suggestion | process | `src/features/teaching_animation/ai/slo_benchmark.py:17-19` | high | 未使用导入 time、field |
| AUD-27114 | Suggestion | doc-gap | `src/features/teaching_animation/ai/test_matrix.py:218-220（对照 7-10、62-64 行）` | medium | compute_coverage 的 any() 语义与 required_layers 契约/模块文档矛盾 |
| AUD-27115 | Suggestion | process | `src/features/teaching_animation/ai/test_matrix.py:189, 241` | high | Any 在文件底部导入并依赖 type: ignore[name-defined] 补丁 |
| AUD-2712 | Suggestion | process | `src/app/controllers/edit_controller.py:3414-3448` | medium | Routing 提交逐命令执行，一次对话框操作产生多条 undo 记录 |
| AUD-2713 | Suggestion | risk | `src/app/controllers/edit_controller.py:3029-3035` | medium | ADR 延迟校准在 UI 线程同步执行往返测量，可能卡住界面 |
| AUD-2714 | Suggestion | process | `src/app/controllers/edit_controller.py:3799-3803（对照 3742-3746）` | high | 生成器时间线色块回退默认色与 metadata["color"] 默认值不一致；且两个 handler 均不刷新媒体池 |
| AUD-2715 | Suggestion | process | `src/app/controllers/edit_controller.py:7516-7534` | medium | 渲染缓存路径在 asset 缺失时用面板快照整体覆盖当前工程引用 |
| AUD-2716 | Suggestion | process | `src/app/controllers/edit_controller.py:8349` | high | `remove_composite_binding_for_clip` 中的自赋值死语句 |
| AUD-2717 | Suggestion | risk | `src/app/controllers/edit_controller.py:3359-3379` | medium | RoutingDialog 旧实例 close 后不 deleteLater，父窗口子部件累积 |
| AUD-2718 | Suggestion | process | `src/app/controllers/edit_controller.py:9424-9431（transient 直接 apply）与 8685-8704（失败恢复）` | medium | 字幕拖拽提交失败时仅恢复 item 几何，controller 侧 transient 时间线不回滚 |
| AUD-2719 | Suggestion | process | `src/app/controllers/edit_controller.py:8036-8043（仅` | medium | MG 空闲缓存 3 秒轮询定时器一旦创建常驻运行 |
| AUD-2720 | Suggestion | process | `src/app/controllers/edit_controller.py:2916-2917（` | high | 多处 broad `except Exception` 不带堆栈地吞掉领域错误 |
| AUD-27201 | Major | risk | `src/features/teaching_animation/composite_node.py:42-45（注册）、144-154（cleanup 未移除）` | medium | `__init__` 注册的默认场景永不注销：节点删除/加载覆盖后成为孤儿场景，随工程保存持久化并跨保存-加载周期无界累积 |
| AUD-27202 | Major | bug | `src/features/teaching_animation/composite_node.py:82-91` | medium | 宽泛 `except Exception` 静默吞掉全部渲染错误且无任何日志，违反"不得宽泛 except 掩盖领域错误"铁律 |
| AUD-27203 | Major | perf-opportunity | `src/features/teaching_animation/composite_node.py:70-93` | medium | 每帧重建渲染器 prepare/cleanup 且无帧级缓存：HTML 场景每显示帧一次浏览器会话建立-截图-关闭，SVG 每帧 CPU 重栅格化 |
| AUD-27204 | Major | bug | `src/features/teaching_animation/composite_node.py:97、126-133` | medium | HDR 场景实际输出 8bit 数据，TextureResult 却按声明的 color_pipeline 上报 bit_depth/color_space |
| AUD-27205 | Suggestion | process | `src/features/teaching_animation/composite_node.py:67-82` | high | 渲染器选择与 `prepare()` 在 try 保护之外，与注释宣称的 fail-soft 范围不符 |
| AUD-27206 | Suggestion | process | `src/features/teaching_animation/composite_node.py:53-58` | high | `_scene_changed` 的 `scene` 参数与 `_do_evaluate` 的 `ctx`/`fb_manager` 缺类型注解 |
| AUD-27300 | Suggestion | process | `src/features/teaching_animation/core/__init__.py:160-253` | medium | `__all__` 与实际导入面严重不一致，约半数公共名未列入导出 |
| AUD-27301 | Major | bug | `src/features/teaching_animation/core/asset_provider.py:269` | high | `LocalAssetProvider.resolve` 每次未命中缓存即全量 `read_bytes()` 计算 sha256，无任何缓存 |
| AUD-27302 | Major | bug | `src/features/teaching_animation/core/asset_provider.py:269` | medium | `path.read_bytes()` 的 OSError 未处理，单文件 IO 错误可击穿整个 preflight |
| AUD-27303 | Major | bug | `src/features/teaching_animation/core/asset_provider.py:251-258, 184-191` | medium | 预览模式下缺失文件资产不产生任何 preflight 诊断 |
| AUD-27304 | Suggestion | process | `src/features/teaching_animation/core/asset_provider.py:143-169` | medium | `AssetProviderRegistry` 注册与解析无并发保护 |
| AUD-27305 | Suggestion | process | `src/features/teaching_animation/core/asset_provider.py:101-102, 265-266` | medium | `from_dict`/`resolve` 对持久化数据做 `int()`/`float()` 强转，无类型验证 |
| AUD-27306 | Suggestion | process | `src/features/teaching_animation/core/asset_provider.py:153, 163` | high | `AssetProviderRegistry.resolve` 返回类型与实际不符，用 `type: ignore` 掩盖 |
| AUD-27307 | Suggestion | process | `src/features/teaching_animation/core/asset_provider.py:154, 167` | medium | 描述符缓存（含 sha256/license）永不失效 |
| AUD-27308 | Suggestion | process | `src/features/teaching_animation/core/asset_provider.py:321, 325-332` | medium | `MotionAssetPack.content_hash` 既无派生也无校验逻辑 |
| AUD-27309 | Major | bug | `src/features/teaching_animation/core/audio_plan.py:151-158（配合 44-58 行校验）` | medium | `AudioRenderPlan.to_timeline` 裁剪后 fade_in+fade_out 可超过剩余时长，`replace` 触发 `__post_init__` ValueError |
| AUD-27310 | Suggestion | process | `src/features/teaching_animation/core/audio_plan.py:94-103, 170` | medium | `AudioClip.from_dict`/`AudioRenderPlan.from_dict` 对持久化数据强转无验证，`clips` 非 list 时静默清空 |
| AUD-27311 | Major | bug | `src/features/teaching_animation/core/builtin_templates.py:209（声明）, 175-177（实际逻辑）` | medium | steps 模板声明 `columns` 参数但构建函数从未读取，参数静默失效 |
| AUD-27312 | Suggestion | process | `src/features/teaching_animation/core/builtin_templates.py:68` | high | title_card 的 scene_id 直接切片用户标题：None 会 TypeError，且未清洗、可重复 |
| AUD-27313 | Suggestion | process | `src/features/teaching_animation/core/builtin_templates.py:422-426, 448-449` | high | data_chart 对负值与过小 `max_value` 的处理产生错误柱形/溢出 |
| AUD-27314 | Suggestion | process | `src/features/teaching_animation/core/builtin_templates.py:756-766（checklist，另见 formula_proof 628-637、product_feature 935-943、comparison 523-530、process_flow 340-353）` | high | 列表型模板不限制条目数，纵向内容可无提示溢出画布 |
| AUD-27315 | Suggestion | process | `src/features/teaching_animation/core/builtin_templates.py:877, 992` | medium | 以 `len(text)*常数` 估算文本宽度，CJK/宽字符会溢出胶囊与按钮 |
| AUD-27400 | Critical | bug | `src/features/teaching_animation/core/commands.py:46` | medium | RemoveElementCommand 的 undo 不恢复随元素一起被删除的动画轨，撤销后数据永久丢失 |
| AUD-27401 | Major | bug | `src/features/teaching_animation/core/commands.py:813` | medium | DetachComponentInstanceCommand 把来自持久化 metadata 的 override 键未经白名单校验直接传给 dataclasses.replace |
| AUD-27402 | Major | bug | `src/features/teaching_animation/core/commands.py:1061` | medium | UpdateComponentMasterCommand 与 SetComponentOverrideCommand 的更新键缺少字段白名单，与 UpdateElementCommand 校验纪律不一致 |
| AUD-27403 | Suggestion | process | `src/features/teaching_animation/core/commands.py:682` | medium | UpdateAnimationTrackCommand.undo 在目标动画不存在时静默无操作 |
| AUD-27404 | Suggestion | process | `src/features/teaching_animation/core/commands.py:768` | high | RenameMarkerCommand 对空白 label 静默沿用旧名 |
| AUD-27405 | Suggestion | process | `src/features/teaching_animation/core/commands.py:809` | medium | DetachComponentInstanceCommand 硬编码在 frame 0 求值烘焙，与"保持用户所见"的语义有偏差 |
| AUD-27406 | Suggestion | process | `src/features/teaching_animation/core/commands.py:855` | medium | 多个命令持有调用方 Mapping 的引用或仅做浅拷贝，redo 语义可被外部突变破坏 |
| AUD-27407 | Suggestion | process | `src/features/teaching_animation/core/commands.py:550` | medium | MoveElementsToPageCommand.visit 递归遍历层级，深嵌套可触发 RecursionError |
| AUD-27408 | Suggestion | process | `src/features/teaching_animation/core/components.py:226` | medium | ComponentMaster.variants 的嵌套结构在 from_dict/解析路径均未验证，畸形数据在 resolve_component_updates 抛 AttributeError |
| AUD-27409 | Suggestion | process | `src/features/teaching_animation/core/components.py:69` | high | ComponentInstance 未校验 element_ids 非空/唯一，与 ComponentMaster 校验纪律不一致 |
| AUD-27410 | Suggestion | process | `src/features/teaching_animation/core/composition_schema.py:106` | high | Mask/Matte from_dict 将未知 kind/mode 静默降级为 alpha，掩盖领域错误并改变渲染语义 |
| AUD-27411 | Suggestion | process | `src/features/teaching_animation/core/composition_schema.py:158` | medium | read_masks/read_mattes 对单条畸形数据 fail-fast，与同目录容错读取模式不一致 |
| AUD-27412 | Major | bug | `src/features/teaching_animation/core/constraints.py:116` | medium | keep_aspect 在仅一轴为 scale 时另一轴不随统一系数缩放，元素仍被拉伸，与模块自述"never stretch"矛盾 |
| AUD-27413 | Suggestion | process | `src/features/teaching_animation/core/contracts.py:133` | medium | RenderedFrame 不校验 data 字节长度与 width/height/pixel_format 的一致性 |
| AUD-27414 | Suggestion | process | `src/features/teaching_animation/core/contracts.py:476` | high | RenderPlan.fail_policy 为自由字符串，注释声明的合法值未在构造时校验 |
| AUD-27415 | Major | bug | `src/features/teaching_animation/core/design_tokens.py:39` | medium | DesignTokenSet.resolve_value 递归解析无环检测且类别经 getattr 任意取属性，畸形 token 引用可致 RecursionError/TypeError |
| AUD-27416 | Suggestion | process | `src/features/teaching_animation/core/design_tokens.py:99` | medium | DesignTokenSet.from_dict 对 safe_area 非序列值未防御，一条畸形数据使 read_scene_tokens 抛 TypeError |
| AUD-27500 | Suggestion | process | `src/features/teaching_animation/core/devtools_property_mapping.py:91-92, 147` | medium | 元素 id 提取存在永假死分支，`_values_equal` 集合字面量含重复项 |
| AUD-27501 | Suggestion | process | `src/features/teaching_animation/core/devtools_property_mapping.py:87-108` | medium | void/未闭合原生元素会使解析器停滞，后续兄弟原生节点全部丢失 |
| AUD-27502 | Suggestion | process | `src/features/teaching_animation/core/devtools_property_mapping.py:259-264` | medium | 文本编辑会把字符串型 `content` 静默改写为 `{"text": ...}` 字典，改变字段类型形状 |
| AUD-27503 | Suggestion | process | `src/features/teaching_animation/core/devtools_property_mapping.py:209-210, 135-143` | medium | `background`/`color` 新值未验证即写入 `style.fill`，任意文本可成为填充色 |
| AUD-27504 | Suggestion | process | `src/features/teaching_animation/core/executable_template.py:104-108, 129-132` | medium | 未知参数被当作致命错误，与注释“preserved (round-trip) but reported”直接矛盾 |
| AUD-27505 | Major | bug | `src/features/teaching_animation/core/font_registry.py:157-173` | medium | 交付许可检查只看链中第一个可解析字体，实际供字的回退字体许可未被检查 |
| AUD-27506 | Suggestion | process | `src/features/teaching_animation/core/font_registry.py:158-160` | medium | 回退链中未注册的 family 被静默跳过，与“字体替换可诊断”的模块目标相悖 |
| AUD-27507 | Suggestion | process | `src/features/teaching_animation/core/font_registry.py:96-99` | high | `covers_text` 返回未排序的码点元组，输出顺序不确定 |
| AUD-27508 | Suggestion | process | `src/features/teaching_animation/core/graphics.py:46-49` | low | `_fmt` 对 NaN/Inf 坐标产出 `"nan"`/`"inf"` 字符串污染 SVG path 数据 |
| AUD-27509 | Suggestion | process | `src/features/teaching_animation/core/hierarchy.py:129-133` | high | `duplicate_parent` 检查为死代码——dict 键唯一使该分支永不可触发 |
| AUD-27510 | Suggestion | process | `src/features/teaching_animation/core/hierarchy.py:107-115` | medium | `Hierarchy.from_dict` 完全忽略载荷中的 `schema_version`，未来 v2 数据会被静默按 v1 解析 |
| AUD-27511 | Suggestion | process | `src/features/teaching_animation/core/hierarchy.py:196-214, 156-165` | high | `sort_for_paint`/`world_affine` 关键参数与返回值缺类型注解（`elements: Any`、`id_of: Any`） |
| AUD-27512 | Suggestion | process | `src/features/teaching_animation/core/html_fonts.py:103-108` | medium | `default_font_set_hash` 依赖 `_PACK` 字典字面量顺序与 `DEFAULT_FONT_ASSET_IDS` 声明顺序一致的隐式 zip |
| AUD-27513 | Major | bug | `src/features/teaching_animation/core/html_layer.py:137-139, 188` | medium | `from_dict` 信任外部载荷的 `sanitizer_hash`，不校验其与 `sanitized_html` 的一致性 |
| AUD-27514 | Suggestion | process | `src/features/teaching_animation/core/html_layer_commands.py:233-241` | medium | `RemoveHtmlLayerCommand` 移除层后不同步 `scene.pages`，残留由已删层生成的页组 |
| AUD-27515 | Suggestion | process | `src/features/teaching_animation/core/html_layer_commands.py:29` | high | 未使用的 `SceneCommand` 导入 |
| AUD-27516 | Suggestion | process | `src/features/teaching_animation/core/html_layer_commands.py:81, 57-59, 262-264` | medium | `SetHtmlPageTimingCommand.timing: Any` 等私有状态字段缺具体类型注解 |
| AUD-27601 | Major | bug | `src/features/teaching_animation/core/html_page_deck.py:511-517` | medium | page_timing_for 对 default_seconds/durations_seconds 的 float 转换无防御，损坏的持久化 metadata 会在加载/导出链路抛未捕获 ValueError（已实证） |
| AUD-27602 | Major | bug | `src/features/teaching_animation/core/html_page_deck.py:331-339, 563` | medium | PageDeckTiming 数值校验用 `<= 0` 拦不住 NaN，NaN 时长经 page_start_frame 的 round(NaN) 抛 ValueError（已实证） |
| AUD-27603 | Suggestion | process | `src/features/teaching_animation/core/html_page_deck.py:299-310` | medium | 页面探测正则不支持属性值内含 ">"，class 位于其后时 detect_page_count 少计、ensure_page_markers 漏标（已实证） |
| AUD-27611 | Suggestion | process | `src/features/teaching_animation/core/html_template.py:76-85, 179, 181` | medium | HtmlTemplate.__post_init__ 缺少 viewport/fps/duration 正值校验，from_dict 的 bound_asset_ids/editable_tokens 元素未字符串化 |
| AUD-27621 | Suggestion | process | `src/features/teaching_animation/core/layout.py:309-315, 372-373, 207-208` | medium | SceneLayout 的 grid_padding/grid_safe_area 长度未校验，AutoLayout 有校验而 grid 路径绕过，resolve() 时解包抛错、布局被静默丢弃 |
| AUD-27622 | Suggestion | process | `src/features/teaching_animation/core/layout.py:217-227` | high | resolve_grid 静默丢弃超出 rows×columns 容量的 children，无任何诊断 |
| AUD-27631 | Suggestion | process | `src/features/teaching_animation/core/migration.py:173-182` | medium | migrate_payload_json 静默丢弃非对象 scenes 条目，损坏场景在加载-保存往返后被无痕抹除 |
| AUD-27641 | Major | bug | `src/features/teaching_animation/core/model.py:607-611` | medium | _from_current_dict 对非列表/None 的 elements 静默清空或抛 TypeError，损坏文件经"打开-保存"即造成场景内容丢失，且与 v0 迁移路径的显式校验不一致 |
| AUD-27642 | Suggestion | process | `src/features/teaching_animation/core/model.py:192-193, 299, 842-843, 882-883` | high | from_dict 多个外部字段未做 str 强转，JSON 中为数字时打破类型注解并可能造成 id 匹配失配 |
| AUD-27643 | Suggestion | process | `src/features/teaching_animation/core/model.py:583-587, 912-919；migration.py:132, 178-182` | medium | TeachingAnimationProjectPayload.from_dict 对每个场景做两次完整迁移+深拷贝，大项目加载成本翻倍 |
| AUD-27644 | Suggestion | doc-gap | `src/features/teaching_animation/core/model.py:139-141, 410-412, 674-683` | medium | frozen 文档携带可变 Mapping 字段，replace() 生成的新快照与旧快照共享同一 dict 引用，undo/revision 快照隔离依赖外部调用者自律 |
| AUD-27701 | Suggestion | process | `src/features/teaching_animation/core/motion_graphic_generator_registry.py:59-63（同型代码 115-119）` | high | 监听器回调异常被静默吞掉且无任何日志（register 与 clear 两处） |
| AUD-27702 | Major | bug | `src/features/teaching_animation/core/motion_graphic_template_library.py:146-151` | medium | 第三方 manifest 的数值字段（viewport/fps/duration）未验证为正数，0/负值可注册进面板 |
| AUD-27703 | Suggestion | process | `src/features/teaching_animation/core/motion_graphic_template_library.py:31-35, 128-132` | medium | `_REQUIRED_METADATA` 只是默认值而非强制契约，manifest 可覆盖 render_profile / allow_inline_scripts / allow_freeform_css |
| AUD-27704 | Critical | bug | `src/features/teaching_animation/core/motion_graphic_template_store.py:165-174` | medium | template_id 未限制字符集，`root / template_id` 可越出库根写入；overwrite=True 时更会先 rmtree 任意已存在目录 |
| AUD-27705 | Major | bug | `src/features/teaching_animation/core/motion_graphic_template_store.py:168-174（与 132-145 的` | medium | overwrite 导入非原子：旧包先 rmtree 后复制，复制失败即丢失原模板 |
| AUD-27706 | Major | bug | `src/features/teaching_animation/core/motion_graphic_template_store.py:193-211` | medium | zip 导入解压无总量/条目数上限，zip 炸弹可撑满磁盘 |
| AUD-27707 | Suggestion | process | `src/features/teaching_animation/core/motion_graphic_template_store.py:51-59（另见 244-249` | medium | Qt 相关调用用宽 `except Exception` 静默回退且无日志，AppData 定位失败时库位置静默分裂 |
| AUD-27708 | Suggestion | process | `src/features/teaching_animation/core/motion_graphic_template_store.py:222-227` | high | `export_template_zip` 写失败时 `.tmp` 残留，无 finally 清理 |
| AUD-27709 | Suggestion | process | `src/features/teaching_animation/core/pages.py:92-99` | high | `retime_page_tracks` 中动画→元素查找为 O(animations×elements) 的线性 next 扫描 |
| AUD-27710 | Suggestion | process | `src/features/teaching_animation/core/pages.py:189-203` | medium | HTML 层计时总长超过场景时长时，末页被钳为 1 帧、页总时长超出 scene.duration_frames，页视图与时间轴不一致 |
| AUD-27711 | Suggestion | process | `src/features/teaching_animation/core/professional.py:44-52` | medium | `SceneColorPipeline.from_dict` 静默把损坏的管线数据重置为 SDR 默认值，违背自身"降级须上报"的契约 |
| AUD-27712 | Suggestion | process | `src/features/teaching_animation/core/professional.py:141-147` | high | `read_particle_emitters` 静默丢弃非法发射器条目，工程数据部分消失无痕迹 |
| AUD-27713 | Suggestion | process | `src/features/teaching_animation/core/style_schema.py:209-211, 222-223（GradientStop 契约见 50-52）` | medium | `resolved()` 不解析渐变 stop 颜色的 token 引用，与其余字段的解析契约不一致 |
| AUD-27714 | Suggestion | process | `src/features/teaching_animation/core/style_schema.py:74-78, 155-165, 184` | medium | from_dict 家族对坏数据的策略不一致：枚举回退、gradient/stops/extras 直接抛异常 |
| AUD-27715 | Suggestion | process | `src/features/teaching_animation/core/style_schema.py:194-198` | medium | `from_style` 的 shadow 触发键与读取键不匹配，legacy 阴影颜色恒为默认黑 |
| AUD-27716 | Suggestion | process | `src/features/teaching_animation/core/template_commands.py:24` | medium | `from .commands import SceneCommand` 为死导入，三个命令类均未继承或引用该 Protocol |
| AUD-27717 | Major | bug | `src/features/teaching_animation/core/template_commands.py:87-96` | medium | `SetBrandThemeCommand.apply` 对模板场景整档重编译，静默丢弃模板应用之后的场景级编辑 |
| AUD-27718 | Suggestion | risk | `src/features/teaching_animation/core/template_registry.py:55-62` | medium | `SceneTemplateRegistry` 无锁保护，与同包 MotionGraphicGeneratorRegistry 的线程安全基线不一致 |
| AUD-27719 | Suggestion | process | `src/features/teaching_animation/core/template_registry.py:36-49` | medium | `SceneTemplate.to_dict` 无对应 `from_dict`，序列化往返不对称 |
| AUD-27801 | Suggestion | perf-opportunity | `src/features/teaching_animation/core/text_layout.py:247-252` | medium | 省略号宽度按截断前行最大字号测量，与截断后重算的字号不一致 |
| AUD-27802 | Suggestion | process | `src/features/teaching_animation/core/text_layout.py:225` | high | 超过 max_width 的不可断行单元（长单词/长 token）静默溢出行盒 |
| AUD-27803 | Suggestion | process | `src/features/teaching_animation/core/text_layout.py:281-284` | medium | JUSTIFY 对无空格的 CJK 行不生效，中文两端对齐静默退化为左对齐 |
| AUD-27804 | Suggestion | process | `src/features/teaching_animation/core/text_layout.py:41-46（对照 59-65）` | medium | TextSpan 的 font_size/letter_spacing 未做数值校验，NaN 会静默破坏换行 |
| AUD-27805 | Suggestion | process | `src/features/teaching_animation/core/text_layout.py:21` | high | 未使用的导入 `field` |
| AUD-27810 | Suggestion | process | `src/features/teaching_animation/core/validator.py:31-35（对照 379-396、504-532、543-590）` | medium | 运行时校验与导出的 JSON Schema 契约发散（background / animation_id / seed·version / payload clips·nodes） |
| AUD-27811 | Suggestion | process | `src/features/teaching_animation/core/validator.py:509-511` | medium | html_layer.sanitized_html 的非空检查对非字符串值做 str() 强转后放行 |
| AUD-27812 | Suggestion | process | `src/features/teaching_animation/core/validator.py:341-342` | high | z_index 类型检查接受 bool，与同文件其他整数字段的 bool 排除惯例不一致 |
| AUD-27813 | Suggestion | process | `src/features/teaching_animation/core/validator.py:269-272（叠加 299-302）` | medium | pages 结构非法时额外产生误导性的"时长总和不符"二次报错 |
| AUD-27900 | Critical | risk | `src/features/teaching_animation/export_service.py:213-214, 497-502` | medium | HTML 场景以 svg_sequence/html_sequence 导出无 pixel_format 分流：RGBA8 字节被写成 .svg（静默损坏）或 UTF-8 解码崩溃 |
| AUD-27901 | Major | risk | `src/features/teaching_animation/export_service.py:536-545` | medium | _write_video 先写满 stdin 再读 stderr：FFmpeg stats 输出写满管道缓冲后必然死锁 |
| AUD-27902 | Suggestion | process | `src/features/teaching_animation/export_service.py:537-549` | high | FFmpeg 子进程句柄未确定性关闭：成功路径不关 stderr，异常路径 kill 后 stdin/stderr 均不关 |
| AUD-27903 | Suggestion | process | `src/features/teaching_animation/export_service.py:57-61` | high | _realtime_progress 的 fps 参数从未使用 |
| AUD-27904 | Suggestion | process | `src/features/teaching_animation/export_service.py:82-89, 460-468` | medium | provenance 两处宽泛 `except Exception` 静默吞错，交付 manifest 可能缺失 content_hashes 而无任何记录 |
| AUD-27905 | Suggestion | process | `src/features/teaching_animation/export_service.py:76-89` | medium | _effective_html_renderer_build 读取共享单例的 backend_build，并发导出时可能把别的导出的 Chromium build 写进 manifest |
| AUD-27906 | Suggestion | process | `src/features/teaching_animation/export_service.py:396-397` | high | width/height 选项未验证为正整数：0 被静默替换为场景尺寸，负值一路透传 |
| AUD-27907 | Suggestion | process | `src/features/teaching_animation/export_service.py:29, 302` | medium | 实时录制路径硬编码 `transparent=False`，静默丢弃用户的 transparent 选项 |
| AUD-27910 | Major | bug | `src/features/teaching_animation/facade.py:272-283` | medium | export_scene_document 只捕获 (TypeError, ValueError, OSError)，HTML 渲染失败的 HtmlRenderError(RuntimeError) 会以原始异常逃出 facade |
| AUD-27911 | Major | risk | `src/features/teaching_animation/facade.py:184-189` | medium | preview_frame 的 renderer.prepare 位于 try/finally 之外，prepare 中途抛错时浏览器会话泄漏 |
| AUD-27912 | Suggestion | process | `src/features/teaching_animation/facade.py:241` | high | insert_into_timeline 的 mode 参数无白名单校验，未知值静默降级为普通插入 |
| AUD-27913 | Suggestion | process | `src/features/teaching_animation/facade.py:244-246` | medium | insert_into_timeline 把 AttributeError/KeyError 一律归为"可恢复 RENDER_FAILED"，可能掩盖宿主接线错误 |
| AUD-27920 | Major | risk | `src/features/teaching_animation/host_adapters.py:333-349` | medium | TimelineSceneFrameProvider.render_frame 的 prepare 同样位于 try/finally 之外，prepare 中途抛错时浏览器会话泄漏 |
| AUD-27921 | Suggestion | process | `src/features/teaching_animation/host_adapters.py:181-186, 226-229, 191` | medium | undo→redo 循环中自建 track_id 不稳定：clip_id 有意复用而 track_id 每次 redo 重新生成 |
| AUD-27922 | Suggestion | process | `src/features/teaching_animation/host_adapters.py:76-87` | medium | SceneReferenceManifest.from_dict 对损坏数据不设防，None/非数值直接抛原始 TypeError/ValueError |
| AUD-27923 | Suggestion | process | `src/features/teaching_animation/host_adapters.py:328-339` | high | render_frame 中 `if rendered is None` 恒为真（缓存命中已提前 return），死条件误导维护者 |
| AUD-2800 | Major | bug | `src/app/controllers/file_controller.py:6738-6740, 6861, 6911-6916` | medium | 用户在关闭确认中选择「Discard」后，closeEvent 仍无条件把脏状态写入工程文件 |
| AUD-28001 | Critical | bug | `src/features/teaching_animation/module.py:812-824, 2190, 2247-2288` | medium | 缓存的 TeachingAnimationAIService 持有过期场景快照，HTML 确认写入会用旧场景覆盖共享 store 的新版本（静默丢失用户编辑） |
| AUD-28002 | Major | bug | `src/features/teaching_animation/module.py:3326-3358, 2504-2512` | medium | 场景编辑确认缺少 revision/陈旧校验，提案会应用到已变化的场景上 |
| AUD-28003 | Major | bug | `src/features/teaching_animation/module.py:5461-5468, 3397-3405` | medium | 文本"确认"对 html_generate 类待处理请求路由到错误处理器：预览卡被清除且确认不生效 |
| AUD-28004 | Major | doc-gap | `src/features/teaching_animation/module.py:5415-5459, 4192-4236` | medium | 构建失败后的 plan 未清理，后续全新创作请求被吞掉或被误当作失败文档的修订 |
| AUD-28005 | Major | risk | `src/features/teaching_animation/module.py:2233-2246, 1921-1948, 3592-3607; 交叉 render/html_validation.py:152-164` | medium | 确认路径与同步兜底在 UI 线程执行全尺寸真实浏览器验证，阻塞主线程 |
| AUD-28006 | Suggestion | process | `src/features/teaching_animation/module.py:5014-5023` | medium | `_ensure_feedback_store` 为死代码且缓存逻辑失效、条件表达式混乱 |
| AUD-28007 | Suggestion | process | `src/features/teaching_animation/module.py:64-67, 812-824, 4969-5007` | medium | `_html_services` 与待处理请求字典无淘汰，长会话内存持续增长 |
| AUD-28008 | Suggestion | process | `src/features/teaching_animation/module.py:205-221` | high | dispose() 未清理 `_main_window`、motion/scene_edit 待处理字典与最近目标引用 |
| AUD-28009 | Suggestion | process | `src/features/teaching_animation/module.py:1263-1265, 1568-1570` | medium | 两个响应处理器的返回注解为 `bool` 但有意返回 None |
| AUD-2801 | Major | bug | `src/app/controllers/file_controller.py:6924-6927（对照 574-593）` | medium | dropEvent 对拖入的视频/任意文件一律调用 load_image，绕过 _open_media_path 路由 |
| AUD-28010 | Suggestion | process | `src/features/teaching_animation/module.py:5220-5234, 5291-5297` | medium | `_extract_proposal_selection` 正则会把含 A/B/C 的短文本误判为方案选择 |
| AUD-28011 | Suggestion | process | `src/features/teaching_animation/module.py:5678-5688` | medium | `open_motion_graphic_asset` 对持久化 metadata 直接 float()/int()，损坏项目数据会抛未捕获异常 |
| AUD-28012 | Suggestion | process | `src/features/teaching_animation/module.py:5760-5764, 5806-5814` | high | 两处宽泛 `except Exception: pass` 未记录日志，掩盖领域错误 |
| AUD-2802 | Major | risk | `src/app/controllers/file_controller.py:2896-3017（PBO 回读）, 3149-3264（直接回读）, 3026-3071（PBO/FBO 分配）, 3320-3342（viewport 抓取）` | medium | DeckLink 监视输出路径在 UI 线程同步 makeCurrent + glReadPixels/glGenBuffers 等 GL 操作，且从不 doneCurrent |
| AUD-2803 | Major | risk | `src/app/controllers/file_controller.py:167-184` | medium | _SourceViewerDecodeWorker 异常路径泄漏已解码的 GPU 帧 |
| AUD-2804 | Major | bug | `src/app/controllers/file_controller.py:4710-4729, 4785-4787` | medium | load_video 在尝试加载前即重置全部调色面板，失败后旧视频会话状态被清空且无恢复 |
| AUD-2805 | Major | bug | `src/app/controllers/file_controller.py:541-549, 666-667` | medium | _ensure_image_project 无确认地用新图像工程替换打开中的时间线工程，未保存的 timeline 编辑可能丢失 |
| AUD-2806 | Suggestion | risk | `src/app/controllers/file_controller.py:972-975` | medium | _perform_periodic_save 在 QTimer 回调中于 UI 线程同步执行全量保存 |
| AUD-2807 | Suggestion | process | `src/app/controllers/file_controller.py:6664-6692（对照 4856-4915）` | medium | unload_video 的断连清单与 _connect_video_control_bar 不对称（漏掉 loop_toggled 与 Composite 侧桥接） |
| AUD-2808 | Suggestion | process | `src/app/controllers/file_controller.py:473-475, 586, 676, 775, 812-815, 4075, 6598` | medium | 大量跨模块私有 API 访问（save_router._get_project_path_for_file、edit_project_io._autosave_filepath、composite_page._node_graph 等） |
| AUD-2809 | Suggestion | process | `src/app/controllers/file_controller.py:4155-4158` | high | _restore_edit_ui_state 对工程文件中的 view_mode 字符串直接枚举索引，未验证可抛 KeyError |
| AUD-2810 | Suggestion | process | `src/app/controllers/file_controller.py:569（对照 872）` | high | RAW 扩展名判定含 .cr3，但打开对话框过滤器未包含 *.cr3 |
| AUD-28100 | Major | bug | `src/features/teaching_animation/motion/easing.py:133-138` | medium | `elastic_in` 曲线数学错误：起点后立即跌至 ≈-1、终点前≈0.0005 后跳变到 1 |
| AUD-28101 | Major | bug | `src/features/teaching_animation/motion/easing.py:149-156` | medium | `elastic_in_out` 中点跳变 0.5、终点前≈0.75 后跳到 1 |
| AUD-28102 | Major | bug | `src/features/teaching_animation/motion/interpolate.py:140-149` | medium | `_mix_colors` 通道值未钳制，越界 eased/外推生成非法十六进制色串（如 `#-ff...`） |
| AUD-28103 | Major | bug | `src/features/teaching_animation/motion/interpolate.py:104-118` | medium | extrapolate 模式字符串未验证，非法值静默按 "extend" 处理，违反模块自身"非法输入显式 ValueError"契约 |
| AUD-28104 | Suggestion | process | `src/features/teaching_animation/motion/interpolate.py:95-99` | high | `value=inf` 未被拦截：`value != value` 只查 NaN，非有限值契约不完整 |
| AUD-28105 | Suggestion | process | `src/features/teaching_animation/motion/interpolate.py:42-54` | high | `_apply_extrapolation` 为死代码，从未被调用 |
| AUD-28106 | Suggestion | process | `src/features/teaching_animation/motion/interpolate.py:101` | high | `interpolate` 每次调用执行函数内 `from .easing import resolve_easing` |
| AUD-28107 | Major | bug | `src/features/teaching_animation/motion/expression.py:37,65,163-170` | medium | 整数 `**`/`pow` 可绕过复杂度预算，造成求值挂起/巨量内存（沙箱 DoS） |
| AUD-28108 | Major | bug | `src/features/teaching_animation/motion/expression.py:160-174` | medium | `markers[...]` 缺键时原生 KeyError 逃逸，违反"一切非法输入抛 ExpressionError"契约 |
| AUD-28109 | Suggestion | process | `src/features/teaching_animation/motion/expression.py:127-131,163-170` | medium | 求值结果类型未限定：字符串字面量或裸 `markers` 原样返回 |
| AUD-2811 | Suggestion | process | `src/app/controllers/file_controller.py:7, 10, 21, 24, 216` | high | 未使用的导入（sys、uuid、QGraphicsScene、MaskShape、MaskGroup、Rational） |
| AUD-28110 | Suggestion | process | `src/features/teaching_animation/motion/expression.py:133-139` | medium | 深层嵌套表达式可在节点预算生效前触发 RecursionError/MemoryError，未转译为 ExpressionError |
| AUD-28111 | Major | bug | `src/features/teaching_animation/motion/beat.py:87-90` | high | `format_timecode` 将有理帧率取整，分数帧率（如 30000/1001）时间码系统性漂移 |
| AUD-28112 | Suggestion | process | `src/features/teaching_animation/motion/beat.py:13-14` | high | 未使用导入：`field`、`Mapping` |
| AUD-28113 | Major | bug | `src/features/teaching_animation/motion/motion_commands.py:222-228（对照 src/features/teaching_animation/motion/presets.py:168-178）` | medium | 部分 dict 形式 `spring_config` 时，持久化 motion 语义与实际编译轨道使用不同弹簧参数（ripple/重开后动画漂移） |
| AUD-28114 | Suggestion | process | `src/features/teaching_animation/motion/motion_commands.py:27` | high | 未使用导入 `SceneCommand` |
| AUD-28115 | Suggestion | process | `src/features/teaching_animation/motion/motion_commands.py:31` | medium | `from src.shared.keyframe import ...` 使用仓库根绝对路径，与同文件相对导入风格混用 |
| AUD-28116 | Suggestion | perf-opportunity | `src/features/teaching_animation/motion/motion_commands.py:312,369,411` | medium | 冻结命令用 `object.__setattr__` 写私有 undo 令牌，重复 apply 会孤儿化先前动画 |
| AUD-28117 | Suggestion | process | `src/features/teaching_animation/motion/motion_commands.py:138-152` | medium | `StaggerPlan.from_dict` 不做范围校验，坏数据延后到 apply 深处才报错 |
| AUD-28118 | Suggestion | process | `src/features/teaching_animation/motion/motion_commands.py:267,431-434` | medium | `StaggerPlan(**plan.to_dict())` 重建路径使 tuple 字段在运行时持有 list |
| AUD-28119 | Major | bug | `src/features/teaching_animation/motion/presets.py:158` | medium | `_slide_preset` 非法 direction 静默回退为 "up"，方向参数无验证 |
| AUD-2812 | Suggestion | process | `src/app/controllers/file_controller.py:4365, 4372-4375, 4382-4385, 4389-4392, 4542, 4595, 4599, 4768, 6486, 6631-6634` | medium | 大量硬编码中文状态消息绕过 I18N.tr |
| AUD-28120 | Suggestion | process | `src/features/teaching_animation/motion/presets.py:25` | high | 未使用导入 `ease_out` |
| AUD-28121 | Suggestion | process | `src/features/teaching_animation/motion/presets.py:274-282` | high | `register` 对重复 preset_id 静默返回 False，调用方不检查 |
| AUD-28122 | Suggestion | process | `src/features/teaching_animation/motion/motion_binding.py:15,69-74` | medium | 未使用导入 `field`；`from_dict` 静默丢弃非法 target 条目且不验证 scale/offset 有限性 |
| AUD-28123 | Suggestion | process | `src/features/teaching_animation/motion/__init__.py:65-72` | medium | 包级 `__all__` 未导出 `motion_semantics` / `read_motion_plans` |
| AUD-2813 | Suggestion | risk | `src/app/controllers/file_controller.py:2191-2216` | medium | _init_gpu_pipeline 在 GPU 渲染线程 start() 之后、装配完成之前发生异常时泄漏已启动线程 |
| AUD-2814 | Suggestion | process | `src/app/controllers/file_controller.py:1089-1090, 1270-1272, 1324-1325, 1351-1353, 1388-1389` | medium | 可选特性 payload 收集/恢复大量 `except Exception: logger.debug(...)` 吞错，默认不可见 |
| AUD-2815 | Suggestion | process | `src/app/controllers/file_controller.py:3729-3746（调用点 2510-2513）` | medium | _frame_to_timecode 未防御 fps<=0（来自外部工程数据）导致除零 |
| AUD-2816 | Suggestion | process | `src/app/controllers/file_controller.py:564 (` | medium | 多个公共处理器与回调缺少类型注解 |
| AUD-2817 | Suggestion | process | `src/app/controllers/file_controller.py:6552-6557` | high | _on_video_frame_changed_for_composite 每次图求值打 INFO 级日志（最高约 30fps） |
| AUD-28200 | Major | risk | `src/features/teaching_animation/motion/sequence.py:107-112（编辑入口无校验）、57-70（layout 内构造 TimeRange）` | medium | `with_step_gap` 接受无界负 gap，`layout()` 在深层抛出延迟 ValueError |
| AUD-28201 | Suggestion | process | `src/features/teaching_animation/motion/sequence.py:21` | high | `ChildLayout = tuple` 为无参数化的弱类型别名 |
| AUD-28202 | Critical | bug | `src/features/teaching_animation/motion/spring.py:46-52` | medium | 近临界阻尼浮点舍入使 `omega_d` 为 0，`spring()`/`_step_response` 抛 ZeroDivisionError |
| AUD-28203 | Major | doc-gap | `src/features/teaching_animation/motion/spring.py:98-104（对照 79-81 docstring、118-120 measure_spring docstring）` | medium | `duration` 不构成有限端点：`measure_spring` 哨兵值混淆可致单帧跳变，且与 docstring 承诺不符 |
| AUD-28204 | Suggestion | process | `src/features/teaching_animation/motion/spring.py:122-123（对照 spring() 的 83-84 行）` | high | `measure_spring` 的 fps 校验缺 `isfinite`，NaN/inf 静默通过并返回伪 settle 帧 |
| AUD-28205 | Suggestion | process | `src/features/teaching_animation/motion/spring.py:99、103` | medium | `spring()` 每次求值内嵌完整 `measure_spring` 601 帧扫描且无缓存（预览/导出热路径） |
| AUD-28206 | Major | bug | `src/features/teaching_animation/motion/time_range.py:68-70` | medium | `shifted()` 负向越界时静默 clamp 到 0，破坏兄弟相对布局且违背"平移"契约 |
| AUD-28207 | Suggestion | process | `src/features/teaching_animation/motion/time_range.py:61-63` | high | `local_frame` 的 `local < 0` 分支不可达（死代码） |
| AUD-28208 | Suggestion | process | `src/features/teaching_animation/motion/time_range.py:78-93` | medium | `transform_to_parent` 丢弃父窗口 duration，组合结果可越过父范围末尾（可见性误报陷阱） |
| AUD-28301 | Major | bug | `src/features/teaching_animation/presets/fragment.py:195-202` | medium | extract_fragment 只做空间原点归一化，不做时间原点归一化，保存的预设关键帧仍停留在宿主全局帧 |
| AUD-28302 | Suggestion | process | `src/features/teaching_animation/presets/fragment.py:21-33` | high | fragment_bounds 忽略 rotation/scale，旋转/缩放元素的包围盒与实际渲染不符 |
| AUD-28303 | Suggestion | process | `src/features/teaching_animation/presets/fragment.py:54、190` | high | 局部容器注解缺元素类型 |
| AUD-28310 | Suggestion | process | `src/features/teaching_animation/presets/builders.py:86-103` | medium | 渐变工厂不校验 opacities 与 stops 数量一致，也不校验 stop position |
| AUD-28311 | Suggestion | process | `src/features/teaching_animation/presets/builders.py:60-65` | high | track() 对显式传入的倒置 min/max 边界静默改写而非报错 |
| AUD-28320 | Major | bug | `src/features/teaching_animation/presets/library.py:337-351` | medium | _import_folder 在 preset_id 冲突时只改文件夹名、不改写 preset.json 内的 preset_id，导致导入假失败并遮蔽同 id 记录 |
| AUD-28321 | Major | bug | `src/features/teaching_animation/presets/library.py:230-244` | medium | save_preset 把未校验的 scene_data 直接落盘，解析/渲染异常被 `except Exception: pass` 吞掉；且 preset.json 先于 scene.json 写入，失败会留下"幽灵预设" |
| AUD-28322 | Suggestion | process | `src/features/teaching_animation/presets/library.py:222（配合 manifest.py:15` | high | 保存名称 slug 超长时触发晦涩的 preset_id 校验失败 |
| AUD-28323 | Suggestion | risk | `src/features/teaching_animation/presets/library.py:237-241` | high | save_preset 在调用线程（UI 线程）同步渲染全分辨率 GIF 预览 |
| AUD-28324 | Suggestion | process | `src/features/teaching_animation/presets/library.py:94-97、174-181、186-188` | medium | rescan/_read_code_info 的异常覆盖不完整，单个坏包可中断整库扫描 |
| AUD-28325 | Suggestion | process | `src/features/teaching_animation/presets/library.py:268、345-346、361-368` | high | delete/_import_folder/export_to 可能抛出 PresetError 之外的 OSError，UI 调用方只捕 PresetError |
| AUD-28400 | Suggestion | process | `src/features/teaching_animation/presets/library/backgrounds/aurora_glow_bg/build.py:18` | high | `build()` 缺少返回类型注解 |
| AUD-28500 | Suggestion | process | `src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/build.py:18` | high | `build()` 缺少返回类型注解 |
| AUD-28501 | Suggestion | process | `src/features/teaching_animation/presets/library/backgrounds/blueprint_grid_bg/build.py:30-34` | low | 横向网格线只覆盖画布上部，与纵向线全覆盖的密度不对称 |
| AUD-28600 | Suggestion | process | `src/features/teaching_animation/presets/library/charts/animated_histogram/build.py:23` | high | `build()` 缺少返回类型注解 |
| AUD-28700 | Suggestion | process | `src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py:4` | high | `linear_gradient` 导入后未使用 |
| AUD-28701 | Suggestion | process | `src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py:54-61` | medium | 数据点 scale 弹出绕包围盒左上角，弹出过程中圆心偏移约 10px |
| AUD-28702 | Suggestion | process | `src/features/teaching_animation/presets/library/charts/line_curve_chart/build.py:21, 33` | high | `_smooth_path` 参数与 `build()` 均缺类型注解 |
| AUD-28800 | Suggestion | process | `src/features/teaching_animation/presets/library/charts/pie_donut_chart/build.py:39` | high | `build()` 缺少返回类型注解 |
| AUD-28900 | Major | bug | `src/features/teaching_animation/presets/library/light/lens_flare_stack/build.py:27-30, 36-39` | medium | 呼吸缩放绕包围盒左上角，主光斑/光环中心随脉动对角漂移约 ±20px |
| AUD-28901 | Suggestion | process | `src/features/teaching_animation/presets/library/light/lens_flare_stack/build.py:18` | high | `build()` 缺少返回类型注解 |
| AUD-2900 | Critical | bug | `src/app/controllers/layer_controller.py:307-321（同类：421-433、725-738）` | medium | UPDATE_AUX_TEX 用 `id(layer)` 作 layer_id，与渲染端 `layer.id`(UUID) 键不匹配：应用预设/库调色后 LUT 与 Warper 静默不生效，且产生无法回收的孤儿 GPU 纹理 |
| AUD-29000 | Suggestion | process | `src/features/teaching_animation/presets/library/light/light_sweep_banner/build.py:18` | high | `build()` 缺少返回类型注解 |
| AUD-2901 | Major | bug | `src/app/controllers/layer_controller.py:1292-1295` | medium | `_composite_startup_fallback` 异常分支引用未定义局部名 `_logger`，触发即 NameError |
| AUD-2902 | Major | bug | `src/app/controllers/layer_controller.py:1022-1042` | high | `request_render` 用宽泛 except 吞掉整个渲染管线异常且无堆栈 |
| AUD-2903 | Major | process | `src/app/controllers/layer_controller.py:90-120、1820-1840` | medium | 图层结构操作（增/删/移/可见性/重命名/全局重置）全程不压 history，撤销粒度与持久化脱节 |
| AUD-2904 | Suggestion | perf-opportunity | `src/app/controllers/layer_controller.py:1212-1216、1230-1249` | medium | Composite 页激活期间每帧执行 `print` + 多条 `logger.info` 调试输出 |
| AUD-2905 | Suggestion | process | `src/app/controllers/layer_controller.py:551-584` | medium | `on_capture_still` 在保存结果未知前先写缩略图/参考帧 sidecar，保存失败留下孤儿文件 |
| AUD-2906 | Suggestion | process | `src/app/controllers/layer_controller.py:126、135、149、982、995、1434、1022` | high | 类型注解缺失/错误：`index: int = None` 与无注解的信号槽 |
| AUD-2907 | Suggestion | process | `src/app/controllers/layer_controller.py:674-680` | high | 分类粘贴对话框任何异常都静默退化为"应用全部类别" |
| AUD-2908 | Suggestion | process | `src/app/controllers/layer_controller.py:277-298 与 627-643` | high | 预设图层剥离逻辑重复实现：库应用路径内联复制了 `_prepare_preset_layers` |
| AUD-2909 | Suggestion | risk | `src/app/controllers/layer_controller.py:1786-1814、1771-1784` | high | UI 线程同步磁盘 I/O：逐个加载 AI 插件 EXR 结果与全量缓存目录扫描 |
| AUD-2910 | Suggestion | process | `src/app/controllers/layer_controller.py:255、302 与 398-399、417` | medium | 外部工程数据的 `active_layer_index` 未验证，且库应用路径忽略源活动层索引（恒 0）与预设路径行为不一致 |
| AUD-29100 | Suggestion | process | `src/features/teaching_animation/presets/library/particles/confetti_burst/build.py:21` | high | `build()` 缺少返回类型注解 |
| AUD-29101 | Suggestion | process | `src/features/teaching_animation/presets/library/particles/confetti_burst/build.py:32-36` | medium | 旋转绕矩形左上角而非形心，下落彩带绕角点"公转" |
| AUD-2911 | Suggestion | process | `src/app/controllers/layer_controller.py:1163-1167、1732-1737、794-798、1149-1150` | high | 多处窄范围 `except Exception: pass` / debug 级吞异常，失败不可观测 |
| AUD-2920 | Major | bug | `src/app/controllers/media_analysis_controller.py:182-195` | medium | `get_asset_status` 契约与实现不符：scheduler 从未被查询，stale/running/failed 查询态不可达 |
| AUD-29200 | Major | bug | `src/features/teaching_animation/presets/library/particles/floating_orbs/build.py:37-40` | medium | 循环尾段关键帧被压缩，部分光球在循环点附近速度突跳约 8 倍 |
| AUD-29201 | Suggestion | process | `src/features/teaching_animation/presets/library/particles/floating_orbs/build.py:22` | high | `build()` 缺少返回类型注解 |
| AUD-2921 | Suggestion | process | `src/app/controllers/media_analysis_controller.py:256-257` | high | `_on_request_done` 失败消息硬编码中文，未走 i18n |
| AUD-2922 | Suggestion | process | `src/app/controllers/media_analysis_controller.py:95-133` | high | `request_analysis_draft` 不校验空/重复 asset_ids |
| AUD-2923 | Suggestion | process | `src/app/controllers/media_analysis_controller.py:203-217` | medium | `_current_project_id` 只写不读；工程切换未清理 service 中未确认的 propose |
| AUD-2930 | Suggestion | process | `src/app/controllers/host_help_sources.py:62-97、112-116、150-159、187-198、223-227` | high | 全文件以 `except Exception` + debug 级日志兜底，意外（非导入类）失败在生产环境不可见 |
| AUD-29301 | Suggestion | process | `src/features/teaching_animation/presets/library/shapes3d/isometric_cube/build.py:25` | high | build() 缺少返回类型注解 |
| AUD-29302 | Suggestion | process | `src/features/teaching_animation/presets/library/shapes3d/isometric_cube/build.py:26,41,45,49` | medium | 同一 KeyframeTrack 实例被三个立方体面共享，存在可变对象别名隐患 |
| AUD-29401 | Suggestion | process | `src/features/teaching_animation/presets/library/shapes3d/wireframe_globe/build.py:31` | high | build() 缺少返回类型注解 |
| AUD-29501 | Suggestion | process | `src/features/teaching_animation/presets/library/text/kinetic_pop_title/build.py:18` | high | build() 缺少返回类型注解 |
| AUD-29601 | Suggestion | process | `src/features/teaching_animation/presets/library/text/neon_glow_title/build.py:18` | high | build() 缺少返回类型注解 |
| AUD-29701 | Major | doc-gap | `src/features/teaching_animation/presets/loader.py:76` | medium | 全部悬空的 scene 级动画未被丢弃，与文档语义矛盾并使预设加载失败 |
| AUD-29702 | Major | bug | `src/features/teaching_animation/presets/loader.py:93` | medium | `TeachingSceneDocument.from_dict` 抛出的 ValueError/TypeError 未包装为 PresetError，逃逸出只捕获 PresetError 的调用方 |
| AUD-29703 | Suggestion | process | `src/features/teaching_animation/presets/loader.py:64` | high | `by_element` 使用裸 `list` 泛型注解 |
| AUD-29704 | Major | risk | `src/features/teaching_animation/presets/manifest.py:86` | medium | `tags`/`schema_version` 类型错误时抛出的 ValueError/TypeError 逃逸 PresetError，预设扫描崩溃 |
| AUD-29705 | Major | bug | `src/features/teaching_animation/presets/manifest.py:45` | medium | `preview` 字段无任何校验，路径遍历/绝对路径可将 GIF 写到预设库外并覆盖任意文件 |
| AUD-29706 | Suggestion | process | `src/features/teaching_animation/presets/manifest.py:89` | high | `schema_version` 无上界检查，未来版本的 manifest 被静默按 v1 解析 |
| AUD-29707 | Suggestion | process | `src/features/teaching_animation/presets/manifest.py:86` | high | `tags` 为字符串时被 `tuple()` 拆成单字符标签（静默垃圾数据） |
| AUD-29708 | Major | risk | `src/features/teaching_animation/presets/preview.py:36` | medium | 全尺寸 CPU 渲染 + GIF 编码为同步 API，被 UI 线程直接调用，4K 场景可冻结界面数秒以上 |
| AUD-29800 | Suggestion | process | `src/features/teaching_animation/render/__init__.py:25-28` | medium | `__all__` 遗漏 4 个已导入再导出的 HTML 渲染符号 |
| AUD-29801 | Major | risk | `src/features/teaching_animation/render/advanced.py:96` | medium | `render_draw_svg` 对 `stroke_width` 直接 `float()`，非数值场景内容触发未捕获 ValueError 进入渲染线程 |
| AUD-29802 | Suggestion | process | `src/features/teaching_animation/render/advanced.py:69-79` | high | 折线图负值数据点绘制在画布外（y 超出 height），与 bar 分支的零高钳制不一致 |
| AUD-29803 | Major | bug | `src/features/teaching_animation/render/asset_resolver.py:99-105` | medium | `data_uri` 无大小/类型上限，任意大资产整文件读入并 base64 内联进 SVG |
| AUD-29804 | Suggestion | risk | `src/features/teaching_animation/render/asset_resolver.py:104` | medium | `data_uri` 的 `read_bytes()` 无 OSError 处理，preflight 通过后的删除/权限竞态使异常冒泡 |
| AUD-29805 | Major | risk | `src/features/teaching_animation/render/benchmark.py:498-528` | medium | `_measure_gpu_effect` 中 ModernGL 上下文与纹理创建位于 try/finally 之外，早期失败泄漏 GL 资源 |
| AUD-29806 | Suggestion | process | `src/features/teaching_animation/render/benchmark.py:560-576` | medium | `_measure_export_encode` 的 av container 无 finally 关闭，encode 中途异常时未释放 |
| AUD-29807 | Suggestion | process | `src/features/teaching_animation/render/benchmark.py:400-403` | medium | `run_benchmark` 未校验场景元素非空，`scene.elements[0]` 可 IndexError |
| AUD-29808 | Major | bug | `src/features/teaching_animation/render/bitrate_probe.py:70-84, 122-131` | medium | 两处 ffprobe `subprocess.run` 均无 timeout，损坏文件可无限挂起，违反“绝不抛错/退化为 unknown”契约 |
| AUD-29809 | Suggestion | risk | `src/features/teaching_animation/render/bitrate_probe.py:140` | medium | `video.stat().st_size` 存在 TOCTOU 竞态，文件消失时 OSError 突破 Never-raises 契约 |
| AUD-29810 | Major | perf-opportunity | `src/features/teaching_animation/render/cache.py:17-18, 97-110` | medium | `build_cache_manifest` 每次调用整读并哈希全部资产文件，而渲染路径每帧调用（build 确认后最多两次） |
| AUD-29811 | Suggestion | process | `src/features/teaching_animation/render/cache.py:167-174` | medium | `SceneRenderCache` 淘汰为插入序 FIFO 而非 LRU：命中不提升、原地更新不续期 |
| AUD-29812 | Suggestion | process | `src/features/teaching_animation/render/cache.py:52-69, 158-174` | medium | `cache_key` 是无缓存的 property，单次 `put` 重复执行 json.dumps+sha256 最多 3 次 |
| AUD-29813 | Suggestion | process | `src/features/teaching_animation/render/consistency.py:83-92, 99-103` | medium | 像素比较为纯 Python 逐字节双重遍历，4K 单帧对比约 3300 万次解释器级循环 |
| AUD-29814 | Suggestion | process | `src/features/teaching_animation/render/consistency.py:21, 152-153` | high | `golden_evidence` 返回未参数化的 `dict`；`field` 导入未使用 |
| AUD-29901 | Suggestion | process | `src/features/teaching_animation/render/effect_capabilities.py:48-54` | medium | frozen dataclass `EffectDeclaration` 含不可哈希的 dict 字段，隐式 `__hash__` 调用即抛 TypeError，且 params 内容可被外部突变 |
| AUD-29902 | Suggestion | process | `src/features/teaching_animation/render/effect_capabilities.py:267-271` | medium | `resolve_effect_chain` 的 fallback 路径不复查 `drop_high_cost` 成本策略 |
| AUD-29903 | Major | bug | `src/features/teaching_animation/render/evaluator.py:203` | medium | element style 中非数值 `opacity` 经裸 `float()` 无防护，脏场景数据使整帧求值抛异常 |
| AUD-29904 | Major | bug | `src/features/teaching_animation/render/evaluator.py:274-277` | medium | `base_style` 为 None 时防御分支 `dict(base_style)` 自身抛 TypeError |
| AUD-29905 | Suggestion | process | `src/features/teaching_animation/render/evaluator.py:227-229` | medium | Auto Layout `resolve` 失败被宽泛 except 静默吞掉且不记录任何 diagnostics |
| AUD-29906 | Suggestion | process | `src/features/teaching_animation/render/evaluator.py:170-187` | medium | 表达式求值仅捕获 `ExpressionError`，深嵌套源码触发的 `RecursionError`/`MemoryError` 会逃逸并打断整帧渲染 |
| AUD-29907 | Suggestion | process | `src/features/teaching_animation/render/evaluator.py:78、223` | high | `_decompose_affine(m: Any)` 与 `layout_rects: dict[str, Any]` 丢失精确类型注解 |
| AUD-29908 | Major | risk | `src/features/teaching_animation/render/frame_adapter.py:44-55` | medium | `QApplication` 无锁 check-then-act 惰性初始化在渲染 worker 线程执行：可双实例创建或在工作线程创建 |
| AUD-29909 | Suggestion | process | `src/features/teaching_animation/render/frame_adapter.py:45` | medium | `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")` 为进程级副作用，worker 先行时会劫持宿主 GUI 平台 |
| AUD-29910 | Suggestion | process | `src/features/teaching_animation/render/frame_adapter.py:57-67` | medium | QImage 分配失败时 `bits()` 返回 None 导致 AttributeError，且异常路径 QPainter 未 end |
| AUD-3000 | Major | bug | `src/app/controllers/menu_toolbar_controller.py:1566` | medium | `QLabel` 未导入，GPU 信息标签永远创建失败且被宽泛 except 静默吞掉 |
| AUD-30001 | Major | bug | `src/features/teaching_animation/render/html_layer/budget.py:62-69` | medium | measure_dom 宽泛 `except Exception: pass` 吞掉解析错误后仍以部分计数出具预算报告 |
| AUD-30002 | Major | bug | `src/features/teaching_animation/render/html_layer/budget.py:79,102-103` | medium | data_uri_bytes 把全部 data: URI 求和后与"单条上限" max_data_uri_bytes 比较 |
| AUD-30003 | Suggestion | process | `src/features/teaching_animation/render/html_layer/budget.py:63,79,90` | high | enforce_budget 文档串与实现不符；data URI 按"字符"计数却命名 bytes |
| AUD-30004 | Suggestion | process | `src/features/teaching_animation/render/html_layer/budget.py:32` | high | _DomCounter.handle_starttag 的 attrs 参数为裸 `list` 注解 |
| AUD-30005 | Major | bug | `src/features/teaching_animation/render/html_layer/html_patch.py:135-140；src/features/teaching_animation/render/html_layer/node_identity.py:69-72` | medium | REPLACE_TEXT 不校验 edit_mode，且 text_slot 全库恒为 True——token_only 节点可被改写文本 |
| AUD-30006 | Major | bug | `src/features/teaching_animation/render/html_layer/html_patch.py:165-169；src/features/teaching_animation/render/html_layer/html_patch_apply.py:254` | medium | SET_ANIMATION_TIMING 的 duration_ms 未做类型/非负校验，int() 的裸异常逃出 HtmlPatchError 契约 |
| AUD-30007 | Suggestion | process | `src/features/teaching_animation/render/html_layer/html_patch.py:163-164,181-189` | medium | _is_transform_value 宣称 "finite" 但未做有限性检查 |
| AUD-30008 | Critical | doc-gap | `src/features/teaching_animation/render/html_layer/html_patch_apply.py:39-43,107-124,180-189` | medium | _locate_element 不识别自闭合写法，回退把"文档剩余部分"当作 inner——replace_text 会清空目标之后全部 HTML |
| AUD-30009 | Major | bug | `src/features/teaching_animation/render/html_layer/html_patch_apply.py:261-266,270-272` | medium | set_animation_timing 对 `animation:` 简写的整个值做整体替换，动画名/迭代/缓动被抹掉 |
| AUD-3001 | Major | bug | `src/app/controllers/menu_toolbar_controller.py:816-820` | medium | `panel_opacity` 预览对停靠（非浮动）dock 无效，且该设置全库无任何启动应用路径 |
| AUD-30010 | Major | bug | `src/features/teaching_animation/render/html_layer/html_patch_apply.py:342-349` | medium | 补丁后 source_hash 被写成"已净化 HTML 的哈希"，违反 FH-01 authored(预净化)语义且重复计算同一哈希 |
| AUD-30011 | Major | bug | `src/features/teaching_animation/render/html_layer/html_patch_apply.py:230-242` | medium | _op_set_slot_attr 用全文字符串 find 定位槽位，<style> 中的 `[data-topos-editable="x"]` 选择器会先被命中 |
| AUD-30012 | Suggestion | process | `src/features/teaching_animation/render/html_layer/html_patch_apply.py:196-198` | medium | _op_set_asset 以子串 `"src" in open_tag` 选择属性，可被 data-src/class 文本误判 |
| AUD-30013 | Suggestion | process | `src/features/teaching_animation/render/html_layer/node_identity.py:61-69` | medium | extract_declared_nodes 宽泛 `except Exception: return {}` 静默丢弃全部可寻址节点 |
| AUD-30014 | Major | bug | `src/features/teaching_animation/render/html_layer/sanitizer.py:747-757,764-774` | medium | CSS 自定义属性在 url() 白名单检查之前无条件放行，`--x: url(https://…)` 可绕过 URL 策略 |
| AUD-30015 | Suggestion | process | `src/features/teaching_animation/render/html_layer/sanitizer.py:951` | high | `allowed_tags or ALLOWED_TAGS`——显式传入空 frozenset 会静默回退为"全部允许" |
| AUD-30016 | Suggestion | process | `src/features/teaching_animation/render/html_layer/sanitizer.py:12-13,56-59,66-67,269-273` | high | 注释/模块契约与实现漂移：animate/set 声称"被排除"实则在白名单；"单个 <style>"实际保留任意多个 |
| AUD-30017 | Critical | bug | `src/features/teaching_animation/render/html_layer/template_promote.py:38-41,237-248；src/features/teaching_animation/render/html_layer/sanitizer.py:372-374` | medium | 参数值仅剥 `<script>` 块；经自由 profile 重建后 `on*` 事件处理器等活动内容注入存活，违背"参数值绝不能注入"承诺 |
| AUD-30018 | Major | bug | `src/features/teaching_animation/render/html_layer/template_promote.py:255-281` | medium | `if layer_metadata:` 恒为真——schema 键被无条件注入，保守净化回退路径成为死代码，所有模板实例化都获得自由 profile |
| AUD-30019 | Suggestion | process | `src/features/teaching_animation/render/html_layer/template_promote.py:85-91,237-248；src/features/teaching_animation/render/html_layer/sanitizer.py:755-757` | medium | token/参数值未限定为单一 CSS 值，`;`/`}` 可重组样式规则（受属性白名单约束的有限注入） |
| AUD-3002 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:1766-1774` | high | `_sync_checkable_module_actions` 新建 `ConfigManager()` 而非复用 `self.config_manager` |
| AUD-3003 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:748-751、776-779（同类：1234-1238、1876-1883）` | high | `show_preferences_dialog` 等多处 `except Exception: pass` 无日志吞错 |
| AUD-3004 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:231、297` | high | lambda 内以 `or` 链接多个调用，依赖返回值 None 的脆弱写法 |
| AUD-3005 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:632-639` | medium | `disable_all_layers` 绕过 `on_layer_visibility_changed` 直接改 `layer.visible`，逻辑重复 |
| AUD-3006 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:9-13、1308-1312` | medium | 未使用导入 `sys`/`subprocess`/`time`；工具栏 undo/redo 为永不触发的死 action |
| AUD-3007 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:587、591、595、620、639、1572、1540、1551` | medium | 状态栏与对话框文案硬编码英文（及个别硬编码中文默认值），未走 I18N.tr |
| AUD-3008 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:499-509` | medium | `_inject_shortcut_text` 追加的 `\t快捷键` 文本在用户自定义快捷键后不会刷新 |
| AUD-3009 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:1832、1843` | high | `_build_panels_menu` 循环体内重复计算 `registry.get_categories(...)` |
| AUD-3010 | Suggestion | risk | `src/app/controllers/menu_toolbar_controller.py:1604-1605` | medium | `on_dependency_report_requested` 在 UI 线程同步扫描 LUT/插件/AI 模型依赖 |
| AUD-30100 | Major | bug | `src/features/teaching_animation/render/html_native_overlay.py:446（同时影响 452、456、484、486）` | medium | CSS 动画名直接内插未转义的 element_id，可注入/破坏整份 overlay 样式 |
| AUD-30101 | Major | bug | `src/features/teaching_animation/render/html_native_overlay.py:609` | medium | `native_overlay_css_variables` 把未校验的 token 名/值直接拼进 CSS 规则 |
| AUD-30102 | Suggestion | process | `src/features/teaching_animation/render/html_native_overlay.py:340` | high | chart/draw 内容解析失败静默返回空串，图层无声消失 |
| AUD-30103 | Suggestion | process | `src/features/teaching_animation/render/html_native_overlay.py:462` | high | 死变量 `previous` |
| AUD-3011 | Suggestion | process | `src/app/controllers/menu_toolbar_controller.py:1288-1295` | high | 最近项目文件不存在时点击无任何反馈 |
| AUD-30110 | Suggestion | process | `src/features/teaching_animation/render/html_preview_renderer.py:14` | high | `_context` 为死状态：赋值后从未被读取 |
| AUD-3012 | Suggestion | process | `全文件（示例：40` | high | 公开方法普遍缺少参数/返回类型注解 |
| AUD-30120 | Major | risk | `src/features/teaching_animation/render/html_realtime_recorder.py:292（Popen stderr=PIPE）；351、353（阻塞 put）；377-378（先 wait 后读 stderr）` | medium | VP8 路径 ffmpeg stderr=PIPE 全程不排水，长录制可致导出线程永久死锁 |
| AUD-30121 | Major | risk | `src/features/teaching_animation/render/html_realtime_recorder.py:245（except 吞错后退出整个消费循环）；349-353（阻塞 put）` | medium | writer 线程死亡后，收尾的阻塞 put 在无消费者的队列上永久挂起 |
| AUD-30122 | Major | bug | `src/features/teaching_animation/render/html_realtime_recorder.py:125（吞异常）；97-99（守卫样式` | medium | 录制守卫移除失败被静默吞掉，可导出整片隐藏（空白）视频且报告成功 |
| AUD-30123 | Suggestion | process | `src/features/teaching_animation/render/html_realtime_recorder.py:406` | high | `transparent` 参数被接受但两条录制路径均未使用 |
| AUD-30124 | Suggestion | process | `src/features/teaching_animation/render/html_realtime_recorder.py:128-131（同 145-150）` | high | `_probe_duration` / `_probe_output_fps_and_frames` 的 `ffmpeg_binary` 参数被忽略 |
| AUD-30125 | Suggestion | process | `src/features/teaching_animation/render/html_realtime_recorder.py:425` | high | duration/fps 非正值静默钳制，与 width/height 的显式 ValueError 不一致 |
| AUD-30126 | Suggestion | process | `src/features/teaching_animation/render/html_realtime_recorder.py:20` | high | `import shutil` 未使用 |
| AUD-30130 | Major | bug | `src/features/teaching_animation/render/html_renderer_service.py:118（release 内 factory 调用）；204-205（finally 调 release）` | medium | `release()` 在 `render_frame` 的 finally 中调用 `self._factory()`，工厂异常会覆盖返回值且使池永久缩水 |
| AUD-30131 | Major | risk | `src/features/teaching_animation/render/html_renderer_service.py:105-113（release 无锁读` | medium | `close()` 与 `release()` 之间存在竞态，可泄漏整个 worker（Chromium 进程） |
| AUD-30132 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_service.py:97（acquire 抛 RuntimeError）；166-169（仅捕获 TimeoutError）` | medium | 服务并发关闭时 `acquire()` 的 RuntimeError 可逃出 fail-closed 契约 |
| AUD-30133 | Suggestion | perf-opportunity | `src/features/teaching_animation/render/html_renderer_service.py:88` | high | `backend_build` 属性无超时阻塞获取 worker |
| AUD-30134 | Suggestion | risk | `src/features/teaching_animation/render/html_renderer_service.py:74-75` | high | `__init__` 中工厂中途失败时已创建 worker 泄漏 |
| AUD-30135 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_service.py:35` | high | 未使用的 `Any` 导入 |
| AUD-30200 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/capture_backend.py:44-48` | medium | `is_uniform_frame` 缺少零尺寸/空数据防护，姊妹函数有而它没有 |
| AUD-30201 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/capture_backend.py:175、209` | high | `CaptureResult` 是字符串字面量而非类型别名；`font_set` 默认值违反自身 Mapping 注解 |
| AUD-30202 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/fixtures.py:187-193` | medium | `attack_data_uri_oversized` 描述为"oversized"但 payload 仅约 20 字节，尺寸向量未被真正行使 |
| AUD-30203 | Major | perf-opportunity | `src/features/teaching_animation/render/html_renderer_spike/html_spike.py:167-176、270-271` | medium | 安全门实验对脚本/API 类攻击空转：占位请求被"unrecognized_scheme"拒绝即判 denied，`expected_denial_reason` 从未被断言 |
| AUD-30204 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/html_spike.py:159、223、34-35、39、45、51、65` | medium | 死代码与未用导入：两处 `ToposAssetRegistry()` 赋值无消费、多个导入未使用、`SpikeConfig.budget` 无执行点 |
| AUD-30205 | Major | bug | `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:1051-1054（注入点）、750（脚本开关）` | medium | `_inject_csp_meta` 的 `<head[^>]*>` 正则同样匹配 `<header>`，无 `<head>` 的页面 CSP meta 会被注入 body 而被浏览器忽略，CSP 防线静默失效 |
| AUD-30206 | Major | bug | `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:740-741、843-846、1013-1015` | medium | screenshot `clip` 以物理像素传入，而 Playwright clip 语义是 CSS 像素：dpr≠1 时超出视口，尺寸不可整除回退的分辨率会全部捕获失败 |
| AUD-30207 | Major | bug | `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:357-372` | medium | 虚拟时钟 pump 的步数/实时预算超限后直接跳变到目标时间，破坏模块自身声明的字节级确定性契约 |
| AUD-30208 | Major | bug | `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:825-833（无条件 evaluate）、537-540（注释）、548-556（每节点 getBoundingClientRect/getComputedStyle）` | medium | DOM 诊断在每次捕获的主路径无条件执行，对全部 DOM 节点做强制样式/布局计算，注释却称"仅在判定 uniform 时使用" |
| AUD-30209 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:852-859` | medium | 仅捕获 PWTimeout/PWError，其他异常会裸抛出 `capture()`，违反 fail-closed 结构化失败契约 |
| AUD-30210 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:861-863、1003-1018` | high | `_png_to_rgba8` 三种失败原因（PIL 缺失/解码异常/尺寸不匹配）合并报同一个 "png decode failed" CRASH |
| AUD-30211 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:84-89、905` | high | 类型注解缺失：`_thread_browser` 无返回注解、`_intercept` 的 `route` 参数未注解 |
| AUD-30212 | Major | bug | `src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:110、整个 capture 函数体` | medium | `url_policy` 参数被接受但从未被使用——后端完全没有 URL 层强制，违反 FrameCaptureBackend 契约 |
| AUD-30213 | Major | bug | `src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:121、157-159、204、239-257` | medium | 未实现帧 seek（frame/fps 仅写入 evidence），且 `_extract_body` 丢弃 authored `<head>/<style>`，确定性实验证据空洞 |
| AUD-30214 | Major | bug | `src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:141-143` | medium | 具名 `QWebEngineProfile` 是磁盘型 profile，"Ephemeral/无残留"注释只对 cookies 成立 |
| AUD-30215 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:46、53-56` | medium | `_DEFAULT_CHROMIUM_FLAGS` 无条件包含 `--no-sandbox`，与 playwright 后端明确的安全立场矛盾 |
| AUD-30216 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:176-177、181-184、228-233、213-218` | medium | `_poll_cancel` 定时器链在 token 未取消时永不停止；cancel/timeout 早退路径跳过 deleteLater 清理 |
| AUD-30217 | Suggestion | risk | `src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:139-140` | medium | 在调用线程创建 QApplication/WebEngine，未声明主线程约束 |
| AUD-30218 | Major | bug | `src/features/teaching_animation/render/html_renderer_spike/security.py:150-153` | medium | 主机白名单解析与 URL 规范不一致：反斜杠混淆可使 `allowed_network_hosts` 被绕过 |
| AUD-30219 | Suggestion | process | `src/features/teaching_animation/render/html_renderer_spike/security.py:201、335、357-364` | medium | data: URI 预算双轨且互不一致：4MB 字符上限生效、`max_data_uri_bytes`=32KB 无任何运行时执行点 |
| AUD-30300 | Major | bug | `src/features/teaching_animation/render/html_scene_renderer_v2.py:150` | medium | `resolve_bound_image_bytes` 用宽泛 `except Exception: pass` 吞掉所有错误 |
| AUD-30301 | Major | doc-gap | `src/features/teaching_animation/render/html_scene_renderer_v2.py:56` | medium | `_append_validation_background` 用 `document.lower()` 的索引切片原文档，Unicode 大小写展开字符使插入位置偏移 |
| AUD-30302 | Suggestion | perf-opportunity | `src/features/teaching_animation/render/html_scene_renderer_v2.py:306` | medium | `render()` 每帧调用 `inspect.signature` 反射探测服务签名 |
| AUD-30303 | Suggestion | process | `src/features/teaching_animation/render/html_scene_renderer_v2.py:52` | medium | 验证背景色未校验即内插进 `<style>` |
| AUD-30304 | Major | bug | `src/features/teaching_animation/render/html_validation.py:190` | medium | 以异常消息子串（"uniform frame"/"near-uniform"）分类空白捕获，脆弱的字符串契约 |
| AUD-30305 | Suggestion | process | `src/features/teaching_animation/render/html_validation.py:38` | medium | `scene.fps`/`scene.scene_id` 直接属性访问与同文件防御式 getattr 混用 |
| AUD-30306 | Suggestion | process | `src/features/teaching_animation/render/html_validation.py:19` | high | `RenderFrame` 类型别名与实际回调签名不符且未被使用 |
| AUD-30307 | Suggestion | perf-opportunity | `src/features/teaching_animation/render/perf_slo.py:31` | medium | 冻结 dataclass `PerfBudget.limits` 持有可变 Mapping |
| AUD-30308 | Suggestion | process | `src/features/teaching_animation/render/release_fixtures.py:144` | medium | 硬编码 `renderer_build="teaching-animation-svg/0.2"`，与上下文默认版本漂移 |
| AUD-30309 | Critical | risk | `src/features/teaching_animation/render/render_worker.py:69` | medium | `_handles` 无界增长：完成 job 的句柄与全部帧数据永不释放，应用级 worker 内存泄漏可致 OOM |
| AUD-30310 | Suggestion | risk | `src/features/teaching_animation/render/render_worker.py:72` | medium | `_handles` 无锁共享可变；`render_sync` 回调注解与 `submit` 取消语义不一致 |
| AUD-30311 | Major | doc-gap | `src/features/teaching_animation/render/scene_renderer_router.py:335` | medium | `_html_with_native_overlay` 以 `document.lower()` 的索引切片原文档，主渲染路径的插入偏移 bug |
| AUD-30312 | Suggestion | process | `src/features/teaching_animation/render/scene_renderer_router.py:297` | medium | `HtmlVectorSceneRendererV1Adapter.render_frame` 复制基类的会话/取消检查与 RenderedFrame 重建逻辑 |
| AUD-30401 | Major | bug | `src/features/teaching_animation/render/svg_renderer.py:584` | medium | 预合成实例只展开一层，嵌套场景内部的二级实例被静默丢弃 |
| AUD-30402 | Major | bug | `src/features/teaching_animation/render/svg_renderer.py:187` | medium | 遮罩绑定支持以实例为目标，但渲染实例时从不引用遮罩 |
| AUD-30403 | Major | bug | `src/features/teaching_animation/render/svg_renderer.py:604` | medium | final 档位下嵌套场景的 blocked 效果未 fail-closed，与父场景行为不一致 |
| AUD-30404 | Major | perf-opportunity | `src/features/teaching_animation/render/svg_renderer.py:447` | medium | 每帧重复磁盘 IO 与 base64：图片 data_uri 逐帧重读，嵌套实例逐帧重建 AssetResolver 并 preflight |
| AUD-30405 | Major | doc-gap | `src/features/teaching_animation/render/svg_renderer.py:149` | medium | 父场景与嵌套场景元素 element_id 相同时，filter/gradient 定义 id 在同一 SVG 文档内重复 |
| AUD-30406 | Suggestion | process | `src/features/teaching_animation/render/svg_renderer.py:530` | high | mask 的 composition_diagnostics 计算后被整体丢弃 |
| AUD-30407 | Suggestion | perf-opportunity | `src/features/teaching_animation/render/svg_renderer.py:556` | medium | `_filter_fragment` 对每个元素每帧最多重复构建三次 |
| AUD-30408 | Suggestion | process | `src/features/teaching_animation/render/svg_renderer.py:13` | high | 未使用导入：read_mattes / SceneAssetRef / SceneElement；travel matte 在 SVG 路径完全未实现 |
| AUD-30409 | Suggestion | process | `src/features/teaching_animation/render/svg_renderer.py:212` | medium | `_sanitize_svg` 用 xml.etree 解析不可信模型内容，缺少尺寸/实体加固 |
| AUD-30410 | Major | bug | `src/features/teaching_animation/render/v2_adapter.py:195` | medium | 协议参数 `cancel_token` 被完全忽略，调用方无法按 V2 约定取消渲染 |
| AUD-30411 | Suggestion | process | `src/features/teaching_animation/render/v2_adapter.py:200` | high | 取消时抛裸 `RuntimeError` 而非 `RenderCancelled`，异常语义与 V1 路径不一致 |
| AUD-30412 | Suggestion | process | `src/features/teaching_animation/render/v2_adapter.py:140` | medium | `SvgRendererSession` 未实现 RendererSession 协议要求的 `close()`，靠 `type: ignore` 掩盖 |
| AUD-30413 | Suggestion | process | `src/features/teaching_animation/render/v2_adapter.py:113` | medium | plan 为 travel matte 声明 SVG_RASTER 能力，但 SVG 渲染器根本不实现 matte |
| AUD-30414 | Suggestion | process | `src/features/teaching_animation/render/v2_adapter.py:191` | medium | `render()` 忽略后续 FrameContext 的 surface 变化，但 evidence 记录的是新 context 的值 |
| AUD-30501 | Major | risk | `src/features/teaching_animation/scene_cache.py:58-62` | medium | 共享协调器惰性初始化无锁，多线程首次并发调用会创建多个 RenderWorker |
| AUD-30502 | Suggestion | risk | `src/features/teaching_animation/scene_cache.py:49-52` | high | close() 在锁外调用 worker.shutdown()，与并发 schedule() 存在窗口；且全库未发现 close() 调用点 |
| AUD-30503 | Suggestion | process | `src/features/teaching_animation/scene_cache.py:26-27` | low | schedule() 在持有 RLock 期间执行模块导入 |
| AUD-30504 | Major | bug | `src/features/teaching_animation/scene_store.py:66-71, 84-87, 54` | medium | replace_all()/clear() 清空 revision 计数后从 1 重新计数，跨项目切换可产生与历史相同编号的 revision |
| AUD-30505 | Suggestion | process | `src/features/teaching_animation/scene_store.py:60-64, 66-71, 84-87` | medium | remove()/clear()/replace_all(()) 不通知订阅者，删除事件对宿主不可见 |
| AUD-30506 | Suggestion | process | `src/features/teaching_animation/scene_store.py:20-23` | high | SceneChange 数据类定义后从未被使用（死代码） |
| AUD-30507 | Suggestion | process | `src/features/teaching_animation/scene_store.py:66-71` | medium | replace_all() 的"清空+逐个重注册"不是原子操作，中间态对并发读者可见 |
| AUD-30600 | Major | risk | `src/features/teaching_animation/ui/asset_library_panel.py:732-739` | medium | “重新生成预览”在 UI 线程同步渲染整段 GIF，期间界面冻结 |
| AUD-30601 | Suggestion | process | `src/features/teaching_animation/ui/asset_library_panel.py:303、606-621、522-534` | medium | 搜索每敲一键全量重建网格并重新解码每张 GIF 首帧，无图标缓存 |
| AUD-30602 | Suggestion | process | `src/features/teaching_animation/ui/asset_library_panel.py:735` | high | `_regenerate_preview` 用宽泛 `except Exception` 把编程错误伪装成“预览失败” |
| AUD-30603 | Suggestion | process | `src/features/teaching_animation/ui/asset_library_panel.py:454-457` | high | `catalog` 属性缺少返回类型注解 |
| AUD-30610 | Major | bug | `src/features/teaching_animation/ui/editor_controller.py:227-240（结合 180-189）` | medium | 自动打点写入的关键帧会被已存在的窄区间轨道静默钳制，渲染出错误动画 |
| AUD-30611 | Major | bug | `src/features/teaching_animation/ui/editor_controller.py:172-177、187-189` | medium | 一次拖拽 + 自动打点会拆成最多 6 条独立撤销记录，与“一手势一撤销”的既定约定相悖 |
| AUD-30612 | Suggestion | process | `src/features/teaching_animation/ui/editor_controller.py:166-171（对照 49-53）` | medium | `commit_preview` 的字段快照遗漏 `layout` 与 `page_id`，预览可改但提交会静默丢弃 |
| AUD-30613 | Suggestion | process | `src/features/teaching_animation/ui/editor_controller.py:23-30` | medium | `SceneDiff.changed_fields` 用 `= None` 默认值 + `object.__setattr__` 修补 + type-ignore |
| AUD-30614 | Suggestion | process | `src/features/teaching_animation/ui/editor_controller.py:242` | high | `apply_track_edits` 的 `edits` 参数缺类型注解 |
| AUD-30620 | Suggestion | process | `src/features/teaching_animation/ui/editor_window_registry.py:21-27` | medium | 重开路径忽略传入的 scene 实例与新回调，正确性完全依赖“调用方永远传 store 当前实例” |
| AUD-30630 | Suggestion | process | `src/features/teaching_animation/ui/html_browser_preview.py:160-171` | medium | profile 先于 view 创建且同为 self 直接子对象，与注释声称的销毁顺序保证相矛盾；安全目前仅靠窗口显式调用 close() |
| AUD-30631 | Suggestion | process | `src/features/teaching_animation/ui/html_browser_preview.py:292-299` | medium | `set_page` 对一次翻页发出两次 `pageChanged`（Python 立即发 + JS 桥异步回发） |
| AUD-30632 | Suggestion | process | `src/features/teaching_animation/ui/html_browser_preview.py:313-314、489-490` | high | 冗余的函数内局部 import（json / QTimer 均已可用） |
| AUD-30640 | Major | bug | `src/features/teaching_animation/ui/html_inspector.py:246-252、339-346` | medium | 数值行直接对外部 `parameter_ui` 元数据做 `float()`/`int()` 强转，畸形模板数据会在场景刷新中抛未捕获异常 |
| AUD-30641 | Suggestion | process | `src/features/teaching_animation/ui/html_inspector.py:194-196` | medium | 文本行的 `editingFinished` 不比较初值，纯焦点经过也会发出补丁命令 |
| AUD-30700 | Suggestion | process | `src/features/teaching_animation/ui/html_source_editor.py:24,111,124-132` | medium | HTML 草稿字典 `_drafts` 永不清理，跨场景/删层后无限驻留 |
| AUD-30701 | Suggestion | process | `src/features/teaching_animation/ui/layer_tree_model.py:170-174` | medium | 全局背景取值对显式 `null` 未防御，`str(None)` 产生字面量 "None" |
| AUD-30702 | Major | doc-gap | `src/features/teaching_animation/ui/layer_tree_panel.py:463-464（footer 移动）、503-518 与 528-535（拖拽）` | medium | 重排序号按"可见行"计算而非全文档元组索引：任一页面折叠时 footer 移动与拖拽落点均写入错误元组位置 |
| AUD-30703 | Major | doc-gap | `src/features/teaching_animation/ui/layer_tree_panel.py:488-499` | medium | 多行拖拽（`start != end`）直接 return：Qt 已完成的视觉移动既不提交命令也不重建，界面与文档顺序脱节 |
| AUD-30704 | Suggestion | process | `src/features/teaching_animation/ui/layer_tree_panel.py:431-436` | medium | 多选删除逐个 emit `removeRequested`，产生 N 个独立 undo 步骤 |
| AUD-30705 | Suggestion | process | `src/features/teaching_animation/ui/layer_tree_panel.py:133,367,488,547,602-603` | medium | 多处形参注解退化为 `object` 并依赖 `type: ignore`；`_element_count` 为死代码 |
| AUD-30706 | Suggestion | process | `src/features/teaching_animation/ui/mg_keyframes_model.py:260-265` | medium | 全局背景取值复刻了 `str(None)→"None"` 的 null 盲区 |
| AUD-30707 | Suggestion | process | `src/features/teaching_animation/ui/mg_keyframes_model.py:249,258` | medium | 背景行标签硬编码英文，绕过 i18n（同模块其它标签均走 I18N.tr） |
| AUD-30800 | Major | bug | `src/features/teaching_animation/ui/mg_keyframes_panel.py:1167` | medium | 硬编码 Space 播放键绕过用户自定义快捷键配置 |
| AUD-30801 | Suggestion | process | `src/features/teaching_animation/ui/mg_keyframes_panel.py:1306` | medium | 网格与标尺绘制固定从第 0 帧遍历，横向滚动后重复绘制大量屏幕外刻度线 |
| AUD-30802 | Suggestion | perf-opportunity | `src/features/teaching_animation/ui/mg_keyframes_panel.py:1833` | medium | 行索引/行查找在循环内线性扫描，形成 O(n²) 热点 |
| AUD-30803 | Suggestion | process | `src/features/teaching_animation/ui/mg_keyframes_panel.py:863` | medium | delete_selected 在未产生任何编辑时也无条件清空点选区 |
| AUD-30804 | Suggestion | process | `src/features/teaching_animation/ui/mg_keyframes_panel.py:928` | medium | 剪贴板跨场景残留，切换场景后仍可粘贴上一场景的关键帧 |
| AUD-30805 | Suggestion | process | `src/features/teaching_animation/ui/mg_keyframes_panel.py:292` | medium | 类型注解缺失/不精确：隐式 Optional 与 Qt 覆写方法上的 type: ignore |
| AUD-30806 | Suggestion | process | `src/features/teaching_animation/ui/mg_keyframes_panel.py:89` | medium | _tool_icon 宽泛捕获 Exception 且无任何日志 |
| AUD-30807 | Suggestion | process | `src/features/teaching_animation/ui/mg_shortcuts.py:82` | medium | `Qt.Key(event.key())` 对枚举外的键值会抛 ValueError 且无防护 |
| AUD-30808 | Suggestion | process | `src/features/teaching_animation/ui/mg_shortcuts.py:46` | medium | 未类型化参数与宽泛的 event 类型注解 |
| AUD-30900 | Critical | bug | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:1169` | medium | 删除最后一页未捕获模型 ValueError，Qt 槽内未处理异常可致进程中止 |
| AUD-30901 | Major | bug | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:2973` | medium | `save()` 无保护调用宿主 saver 与缓存协调器，Ctrl+S / 关闭保存时 I/O 异常直接穿入 Qt 槽 |
| AUD-30902 | Major | bug | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:2223` | medium | 关闭时 DevTools 保存路径：apply 失败仍显示成功并强制 save+close，未应用更改被静默丢弃；失败分支滞留 `_close_after_devtools_apply` |
| AUD-30903 | Major | bug | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:427` | medium | 拖拽期间每个 mouse-move 触发全量 `_refresh_scene`（忽略 diff），图层树/关键帧面板/原生 overlay 全部重建 |
| AUD-30904 | Major | bug | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:2188` | medium | `_apply_live_dom_source` 的异常保护放在命令构造而非 `execute`，DevTools Apply 的 `apply()` 抛 ValueError 未捕获 |
| AUD-30905 | Suggestion | risk | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:2818` | medium | `_request_html_preview` 在线程已停但 finished 事件未送达时不清除 pending，陈旧请求稍后被重新启动 |
| AUD-30906 | Suggestion | risk | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:2888` | medium | `_stop_html_preview` 在 UI 线程无限期 `thread.wait()`，取消链路失效时关闭冻结 |
| AUD-30907 | Suggestion | process | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:1055` | low | `QTimer.singleShot` 回调未绑定窗口生命周期，窗口销毁后触发会对已删除 wrapper 抛 RuntimeError |
| AUD-30908 | Suggestion | process | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:2156` | high | 类型注解缺失/不精确 |
| AUD-30909 | Suggestion | process | `src/features/teaching_animation/ui/motion_graphic_editor_window.py:869` | high | 869-870 行 import 续行缩进异常 |
| AUD-3100 | Major | bug | `src/app/controllers/page_controller.py:1400-1424（配合 1211-1243 的部分拆除、543-545 的每次 enter 调用）` | medium | Composite 时间线传输条 13 个槽闭包每次进入页面重复连接且从不拆除旧连接 |
| AUD-31001 | Suggestion | process | `src/features/teaching_animation/ui/motion_graphic_style.py:457` | high | QSplitter::handle 规则重复定义两次 |
| AUD-31002 | Suggestion | risk | `src/features/teaching_animation/ui/page_navigation.py:99` | medium | `_updating` 状态锁只写不读，是死代码 |
| AUD-31003 | Suggestion | risk | `src/features/teaching_animation/ui/page_timing_panel.py:429` | medium | `_link_slider` 双向互连未加状态锁，靠数值收敛终止乒乓 |
| AUD-31004 | Suggestion | process | `src/features/teaching_animation/ui/page_timing_panel.py:384` | medium | `backgroundSettingsRequested` 无任何生产消费者，注释描述的刷新顺序已失实 |
| AUD-31005 | Suggestion | process | `src/features/teaching_animation/ui/page_timing_panel.py:60` | high | `self._timing` 缺少类型注解 |
| AUD-31006 | Major | bug | `src/features/teaching_animation/ui/scene_inspector.py:574` | medium | 多选编辑 Content 会把字符串写进 dict 内容的图元，破坏其数据并使渲染持续报错 |
| AUD-31007 | Major | bug | `src/features/teaching_animation/ui/scene_inspector.py:557` | medium | Transform 行每次拖动刻度都发射一条完整 elementUpdateRequested，逐 tick 生成 undo 条目 |
| AUD-31008 | Suggestion | process | `src/features/teaching_animation/ui/scene_inspector.py:413` | medium | 动画摘要读取使用宽泛 `except Exception` 静默吞错 |
| AUD-31009 | Major | bug | `src/features/teaching_animation/ui/scene_timeline.py:133` | medium | 时长 DragValue 每个 valueChanged 刻度都触发一条 SetDurationCommand，拖动一次产生大量 undo |
| AUD-3101 | Major | bug | `src/app/controllers/page_controller.py:2389-2416` | medium | `_show_color_timeline_clip_static_preview` 重试耗尽后进入不设上限的 100ms 无限重试环 |
| AUD-31010 | Suggestion | process | `src/features/teaching_animation/ui/scene_timeline.py:41` | high | `_MarkerTimelineSlider.paintEvent` 缺少参数类型注解 |
| AUD-31011 | Major | risk | `src/features/teaching_animation/ui/scene_view.py:91` | medium | 每帧新建的 QSvgRenderer 以 self 为父且从不释放，预览路径无限累积 |
| AUD-31012 | Suggestion | process | `src/features/teaching_animation/ui/scene_view.py:96` | medium | rgba8 帧构造 QImage 前未校验缓冲区长度 |
| AUD-3102 | Major | risk | `src/app/controllers/page_controller.py:3072-3085（` | medium | Edit→Color 切换路径在 UI 线程同步执行 PyAV 容器探测 |
| AUD-3103 | Major | bug | `src/app/controllers/page_controller.py:3044-3048（` | medium | 播放头跟随的视频轨分层约定在两条路径中相互矛盾 |
| AUD-3104 | Major | bug | `src/app/controllers/page_controller.py:880-884` | high | `_apply_composite_preview_color_space` 用宽泛 except 将色彩管线失败降级为 DEBUG 日志 |
| AUD-3105 | Suggestion | process | `src/app/controllers/page_controller.py:873-879` | high | HLG 源在旧版 Viewer 回退路径被静默映射为 sRGB |
| AUD-3106 | Suggestion | process | `src/app/controllers/page_controller.py:416-420、473-479` | medium | `leave_page_hook`/`enter_page_hook` 中两处 `except Exception: pass` 完全吞掉失败 |
| AUD-3107 | Suggestion | risk | `src/app/controllers/page_controller.py:322-332（` | medium | UI 线程调用 `gl_viewport.makeCurrent()` 后未配对 `doneCurrent()` |
| AUD-3108 | Suggestion | process | `src/app/controllers/page_controller.py:1130、601-606、1248、1500-1509、1371/1381、327 等` | medium | 大量跨模块私有成员读写，封装破坏 |
| AUD-3109 | Suggestion | process | `src/app/controllers/page_controller.py:1835-1882（` | high | 死代码：两个无调用方的私有辅助方法（约 100 行） |
| AUD-3110 | Suggestion | process | `src/app/controllers/page_controller.py:2973、3481、3555（` | high | 隐式 Optional 参数与缺失类型注解 |
| AUD-31101 | Suggestion | process | `src/features/teaching_animation/ui/storyboard_panel.py:49-52, 59-63` | medium | set_scenes() 的选中 id 在新列表中不存在时静默丢失选中，且不回退到第 0 行 |
| AUD-31102 | Suggestion | process | `src/features/teaching_animation/ui/storyboard_panel.py:46, 57, 61` | high | ItemDataRole 使用魔法数字 256 |
| AUD-31103 | Suggestion | process | `src/features/teaching_animation/ui/storyboard_panel.py:77-86` | medium | _rows_moved 依赖"拖拽后 currentItem 即被拖项"这一 Qt 隐式行为，无当前项时重排事件静默丢失 |
| AUD-3111 | Suggestion | process | `src/app/controllers/page_controller.py:1897-1900` | high | `switch_to_page_with_context` 局部导入的 `PageType` 未使用 |
| AUD-3112 | Suggestion | process | `src/app/controllers/page_controller.py:936-942（` | medium | Composite 自动绑定的播放头兜底把音频轨 clip 也作为候选 |
| AUD-3113 | Suggestion | process | `src/app/controllers/page_controller.py:284-286（对比 3590-3596 的` | medium | `switch_to_page` 调用 `enter_page_hook` 无异常隔离，失败时页面停在半切换状态 |
| AUD-31200 | Critical | bug | `src/main.py:291（使用处）；src/main.py:104-106（导入处缺失）` | medium | QTimer 未导入：可选模块加载失败时启动即 FATAL 退出 |
| AUD-31201 | Major | doc-gap | `src/main.py:309（对照 main.py:137 的` | medium | 致命错误路径用 sys.exit(1) 而非 _orderly_shutdown，重新暴露已文档化的解释器终结化崩溃 |
| AUD-31202 | Suggestion | process | `src/main.py:161-166, 263（下游 src/app/controllers/file_controller.py:429-430）` | medium | --project 参数未做存在性校验，非法路径静默无效 |
| AUD-31203 | Suggestion | process | `src/main.py:140（` | medium | 类型注解缺失/不精确与不可达返回 |
| AUD-31401 | Suggestion | process | `src/shared/actions/__init__.py:10-11` | high | 未使用的导入：dataclasses.field、typing.Callable/Any/List |
| AUD-31402 | Suggestion | process | `src/shared/actions/__init__.py:49` | high | ActionRegistry.__init__ 缺少返回类型注解 |
| AUD-31500 | Critical | risk | `src/shared/ai_models/ai_plugin_engine.py:455-460, 487-492` | medium | 取消路径依赖 QThread.terminate() 强杀正在执行 ONNX 原生推理的线程 |
| AUD-31501 | Major | risk | `src/shared/ai_models/ai_plugin_engine.py:39-41, 88-92, 182-183, 199` | medium | 类级 `_session_cache`/`_current_model_path` 无锁跨线程读写，多 worker 并发时缓存被互相清空 |
| AUD-31502 | Major | bug | `src/shared/ai_models/ai_plugin_engine.py:257-266, 355-364（循环内无 abort 检查；类中不存在 abort 标志）` | medium | 分块推理循环无任何协作取消检查点，取消只能靠 terminate 强杀 |
| AUD-31503 | Suggestion | risk | `src/shared/ai_models/ai_plugin_engine.py:209-223` | medium | `_load_with_timeout` 超时后泄漏仍在加载的 daemon 线程，且吞掉真实加载异常 |
| AUD-31504 | Suggestion | process | `src/shared/ai_models/ai_plugin_engine.py:246-251, 344-349` | high | 插件元数据（外部数据）的 tile/overlap 未验证，overlap>=tile_size 时 range 步长为 0 直接 ValueError |
| AUD-31505 | Suggestion | process | `src/shared/ai_models/ai_plugin_engine.py:324-325, 382-385, 407-413` | medium | upscale 输出坐标 int() 截断可能在末行/列留下零权重黑线 |
| AUD-31506 | Suggestion | process | `src/shared/ai_models/ai_plugin_engine.py:16, 64-67, 43, 69, 141` | high | 模块级无条件 import onnxruntime、run() 用 print_exc 而非 logger、类与方法普遍缺类型注解 |
| AUD-31510 | Major | perf-opportunity | `src/shared/ai_models/ai_mask_engine.py:391-396` | medium | SAM decode 每次请求全量重算所有历史点，会话累计 O(N²) 次解码器推理 |
| AUD-31511 | Major | bug | `src/shared/ai_models/ai_mask_engine.py:317-318, 346, 354-357` | medium | SAM encode 失败/取消后 `sam_img_shape` 与 `sam_embedding` 状态不一致，可静默产出错位蒙版 |
| AUD-31512 | Suggestion | process | `src/shared/ai_models/ai_mask_engine.py:282-285` | medium | BiRefNet sigmoid 启发式在 logits 全落 [0,1] 时失效，软蒙版取值未归一化 |
| AUD-31513 | Suggestion | process | `src/shared/ai_models/ai_mask_engine.py:297, 355, 452, 45, 250` | medium | 异常以 logger.info 记录、未使用导入、日志前缀混乱、方法缺类型注解 |
| AUD-31520 | Major | bug | `src/shared/ai_models/denoise_worker.py:533-536, 515-517, 552-554` | medium | 控制器持有已 deleteLater 的 QThread 包装，二次调用 is_running/start_processing 触碰已销毁对象抛 RuntimeError |
| AUD-31521 | Major | bug | `src/shared/ai_models/denoise_worker.py:379-381, 287-293` | medium | 用户取消以 error 信号上报为"发生致命错误"，混入完整 traceback |
| AUD-31522 | Major | risk | `src/shared/ai_models/denoise_worker.py:513-517, 380, 300-318` | high | start_processing 在调用线程阻塞 wait()，abort 仅在块间生效，长推理期间 UI 冻结 |
| AUD-31523 | Major | bug | `src/shared/ai_models/denoise_worker.py:338-341` | medium | overlap==1 时 ramp 计算除以零产生 NaN 权重，输出边缘黑边且被 nan_to_num 静默掩盖 |
| AUD-31524 | Suggestion | process | `src/shared/ai_models/denoise_worker.py:349, 356, 252` | high | 外部参数与图像输入未验证：overlap>=tile_size 致 range 步长 0，非 3 通道图像致 cv2.error |
| AUD-31525 | Suggestion | risk | `src/shared/ai_models/denoise_worker.py:526-528, 540-545` | medium | error 路径不清理 worker 对象，每次失败泄漏一个 AIDenoiseWorker（含会话引用） |
| AUD-31600 | Major | bug | `src/shared/ai_models/generation/credential_store.py:65-73` | medium | macOS `security` CLI 以 argv 传递明文 API key，进程列表可被本机其他用户窥视 |
| AUD-31601 | Major | bug | `src/shared/ai_models/generation/credential_store.py:197-210` | medium | HybridCredentialStore 写单后端读定优先：偏好切换后旧后端残留的过期 key 遮蔽新 key |
| AUD-31602 | Suggestion | process | `src/shared/ai_models/generation/credential_store.py:236-254（另见 281-288 preference、provider_profiles.save_to_user_file）` | medium | 同一 JSON 文件多处 read-modify-write 无并发防护，后写者整文件覆盖 |
| AUD-31603 | Suggestion | risk | `src/shared/ai_models/generation/credential_store.py:48-53（79-85 同）` | medium | `security` 子进程调用无 timeout，Keychain 受锁时可能无限阻塞调用线程 |
| AUD-31604 | Suggestion | process | `src/shared/ai_models/generation/credential_store.py:338-342` | medium | 模块级构造三个 store 作"协议断言"，`type: ignore` 反而抑制真实的协议检查 |
| AUD-31605 | Suggestion | process | `src/shared/ai_models/generation/generation_models.py:152-159` | medium | GenerationConsent.is_complete 不校验 provider_id/target_location 非空，§2.3"每次确认 provider/目标位置"未在合同层强制 |
| AUD-31606 | Suggestion | process | `src/shared/ai_models/generation/generation_models.py:540-542（状态机声明见 478-492）` | high | GeneratedAsset.with_state 不校验状态机合法迁移，terminal 态可被复活/绕过 |
| AUD-31607 | Suggestion | process | `src/shared/ai_models/generation/generation_models.py:301-306（另 313-318、326-333）` | medium | GenerationRecipe.migrate 对外部 0.x payload 的数值/枚举转换无容错，抛裸 ValueError 且缺上下文 |
| AUD-31608 | Suggestion | process | `src/shared/ai_models/generation/generation_models.py:178-189` | medium | GenerationBudget 无 __post_init__ 校验，"硬上限"合同接受非法值 |
| AUD-31609 | Suggestion | process | `src/shared/ai_models/generation/generation_models.py:368、540-547` | medium | 类型与结构小问题：reference_bindings 弱类型为 tuple[dict,...]；with_state/with_preview_handle 重复局部 import |
| AUD-31610 | Major | bug | `src/shared/ai_models/generation/image_gen_job.py:210-214` | medium | 状态机在 transport 调用前一次性连跳 UPLOADING→RUNNING→DOWNLOADING，可观察状态与真实阶段不符 |
| AUD-31611 | Major | bug | `src/shared/ai_models/generation/image_gen_job.py:135-138` | medium | 仅为计算 cache key 对整幅 4K float32 图像做两次全量拷贝（astype + tobytes，瞬时 ~2×133MB） |
| AUD-31612 | Suggestion | process | `src/shared/ai_models/generation/image_gen_job.py:61-73（expand_request_params 227-246 未校验）` | high | border_expand_mask / expand_request_params 不校验扩展比例 ∈ [0,1]，越界值产生语义错误的 mask |
| AUD-31613 | Suggestion | process | `src/shared/ai_models/generation/image_gen_job.py:162-165` | medium | store_cache 元数据明文保存 prompt 前 120 字符，与"默认不保留 prompt 原文"的数据最小化策略不一致 |
| AUD-31614 | Major | perf-opportunity | `src/shared/ai_models/generation/model_pack_manager.py:556-559（_verify_dir 全量遍历 762-775；require_usable 579-589）` | medium | get_status/require_usable 每次调用都对 GB 级模型目录全量重算 sha256，无校验结果缓存 |
| AUD-31615 | Major | bug | `src/shared/ai_models/generation/model_pack_manager.py:762-767` | medium | _verify_dir 目录摘要未对子目录遍历顺序排序，同一目录树可算出不同 digest → 已装 pack 被误判校验失败 |
| AUD-31616 | Major | bug | `src/shared/ai_models/generation/model_pack_manager.py:726-740` | medium | verify() 校验成功不回写 record.sha256/verification_mode，成功结果无法在 get_status 生效 |
| AUD-31617 | Suggestion | risk | `src/shared/ai_models/generation/model_pack_manager.py:818-819（_load_manifest 静默吞错 799-800）` | high | _save_manifest 非原子写（truncate 式 open("w")），崩溃可致 manifest 损坏 → 全部安装记录静默清零 |
| AUD-31618 | Suggestion | doc-gap | `src/shared/ai_models/generation/model_pack_manager.py:52-58` | high | default_cache_dir docstring 仍描述旧路径（edit 侧），parents[4] 恰好同深度才未出错，再移动一层即静默写错位置 |
| AUD-31619 | Suggestion | risk | `src/shared/ai_models/generation/model_pack_manager.py:628-635、815` | medium | _installed 与 manifest 保存无锁：下载 worker 回调与 UI/推理线程并发时可抛 RuntimeError |
| AUD-31620 | Major | bug | `src/shared/ai_models/generation/provider_profiles.py:122（另 223、235）` | medium | shared 层运行时反向 import features.edit.ai_editing（infer_capabilities），违反模块边界且 ImportError 未被加载降级捕获 |
| AUD-31621 | Major | bug | `src/shared/ai_models/generation/provider_profiles.py:111-112、135-141（except 见 289-293、329-332）` | medium | 单条损坏 profile 的 data_policy/limits 非对象时抛 AttributeError，逃出"单条损坏跳过"策略使全部 profile 加载失败 |
| AUD-31622 | Suggestion | process | `src/shared/ai_models/generation/provider_profiles.py:113-114、143-147` | medium | from_dict 宽松转换：modalities 字符串被拆成单字符元组；数值字段未强转与注解不符 |
| AUD-31623 | Major | bug | `src/shared/ai_models/generation/user_ai_config.py:24-29（后果链见 credential_store.py:236-254、281-288 与 provider_profiles.py:308-315）` | medium | load_user_ai_config 把暂时性读取失败（OSError）与文件不存在同等返回 {}，配合调用方 read-modify-write 整体覆盖 → profiles 与 API key 全部丢失 |
| AUD-31700 | Major | bug | `src/shared/ai_models/generation/volcengine_generation_provider.py:177-182` | medium | `_open` 的 certifi try 块吞掉 opener 抛出的 OSError 家族异常并无条件重发同一 POST（重复请求/重复计费风险） |
| AUD-31701 | Major | bug | `src/shared/ai_models/generation/volcengine_generation_provider.py:198-202（` | medium | endpoint 未做任何验证：空/相对端点使 `urllib.request.Request` 在 try 外抛原始 `ValueError` 逃逸 provider 错误契约 |
| AUD-31702 | Major | perf-opportunity | `src/shared/ai_models/generation/volcengine_generation_provider.py:244-253（图像）、376-384（TTS）` | medium | `is_cancelled` 仅在网络请求前检查一次，阻塞 HTTP 期间完全不可取消 |
| AUD-31703 | Major | bug | `src/shared/ai_models/generation/volcengine_generation_provider.py:288-303` | medium | 图像尺寸静默强制 2048×2048 正方形，`aspect_ratio` 请求字段被完全忽略 |
| AUD-31704 | Major | bug | `src/shared/ai_models/generation/volcengine_generation_provider.py:315-323、348-351` | medium | 多个 b64 候选写同一缓存文件 `{request_id}.png`，后者覆盖前者 |
| AUD-31705 | Major | bug | `src/shared/ai_models/generation/volcengine_generation_provider.py:215-222（记录）、296-303（请求体未含任何参考输入）、91-95（无数据策略防护）` | medium | 参考素材被静默丢弃，但 provenance 仍记录 `reference_asset_ids`/`reference_bindings`，溯源失真 |
| AUD-31706 | Suggestion | process | `src/shared/ai_models/generation/volcengine_generation_provider.py:428-432 vs 524-527` | medium | TTS `_body` 与 `_cache_audio` 的 output_format 来源不一致，缓存扩展名可能与实际音频容器不符 |
| AUD-31707 | Suggestion | process | `src/shared/ai_models/generation/volcengine_generation_provider.py:312` | high | `max(1, request.budget.max_candidates)` 使预算为 0 时仍回收 1 个候选 |
| AUD-31708 | Suggestion | process | `src/shared/ai_models/generation/volcengine_generation_provider.py:85-96、244-246、377-379` | medium | `_secret` 先访问凭据存储（keychain/文件 IO）再做模态检查；逐 cue TTS 场景每条字幕重复读取凭据 |
| AUD-31709 | Suggestion | process | `src/shared/ai_models/generation/volcengine_generation_provider.py:115-120（仅 174 行用到 limits 的 timeout）` | medium | `profile.limits.max_retries` 完全未使用，429/5xx/瞬时网络错误无任何重试 |
| AUD-31710 | Suggestion | risk | `src/shared/ai_models/generation/volcengine_generation_provider.py:348-349、528-529` | high | generated_media 缓存目录无容量上限与清理策略，生成文件无限累积 |
| AUD-31711 | Suggestion | process | `src/shared/ai_models/generation/volcengine_generation_provider.py:445-448、460-486` | medium | `_parse_json_audio` 无法解析顶层 JSON 数组，而 `_looks_like_json` 明确接受 `[` 前缀 |
| AUD-31712 | Suggestion | process | `src/shared/ai_models/generation/volcengine_generation_provider.py:296-303` | medium | 请求字段映射不全：`recipe.seed`、`style`、`negative_prompt` 被忽略 |
| AUD-31713 | Suggestion | process | `src/shared/ai_models/generation/volcengine_generation_provider.py:18、22（导入）、275/313/463/478（isinstance）、546-549（工厂签名）` | medium | 质量杂项：未使用的 `urllib.parse` 导入、`isinstance(x, typing.Mapping)` 弃用用法、公共工厂返回私有类型 |
| AUD-31800 | Major | risk | `src/shared/ai_models/inference_engine.py:917-935` | medium | cancel/cancel_all 持有互斥锁期间同步 wait(3000)，调用线程可被阻塞数秒且所有管理操作被锁死 |
| AUD-31801 | Major | risk | `src/shared/ai_models/inference_engine.py:924, 934` | medium | wait 超时后从 `_workers` 弹出 worker，运行中的 QThread 可能失去最后引用被 GC 销毁（"QThread: Destroyed while thread is still running" 崩溃） |
| AUD-31802 | Major | bug | `src/shared/ai_models/inference_engine.py:140-141, 599, 609, 614-621, 702` | medium | tile_size/overlap 无关系校验：stride ≤ 0 时静默输出全黑图像或抛 ValueError；overlap == 1 时除零 |
| AUD-31803 | Major | bug | `src/shared/ai_models/inference_engine.py:182-185, 484-486, 632-634` | medium | 用户取消以 RuntimeError("Cancelled") 抛出，被统一 except 捕获后走 error 信号 → 主动取消被上报为推理失败 |
| AUD-31804 | Major | bug | `src/shared/ai_models/inference_engine.py:778-789, 797-812` | medium | 输入侧无 HDR 范围处理而输出侧无条件 clip(0,1)，与"HDR 内部数据不得无理由 clamp"铁律冲突且行为不对称 |
| AUD-31805 | Suggestion | process | `src/shared/ai_models/inference_engine.py:978-984` | high | `_save_to_cache` 宽泛 `except Exception: pass` 吞掉缓存保存失败 |
| AUD-31806 | Suggestion | process | `src/shared/ai_models/inference_engine.py:67` | high | `AIInferenceWorker.finished` 遮蔽 QThread 内置 `finished` 信号 |
| AUD-31807 | Suggestion | risk | `src/shared/ai_models/inference_engine.py:465-470, 523-525, 937-940, 73` | medium | 会话缓存 check-then-act 竞态可重复建会话，且缓存无淘汰、worker 完成后未 deleteLater |
| AUD-31808 | Suggestion | doc-gap | `src/shared/ai_models/inference_engine.py:31, 552-554, 952-961` | medium | 死代码与文档不符：`_emit_preview` 结果未存储、`_run_inference` 的 tile 形参从未使用、`import time` 未使用 |
| AUD-31809 | Major | bug | `src/shared/ai_models/model_manager.py:204-213, 229-232, 302-305（sha256 示例 80-89）` | medium | 下载非原子且异常中断不清理残留文件：部分文件在下次启动被 `_sync_registry` 仅凭存在即标记为可用，且注册表 sha256 全为 None 无法校验 |
| AUD-31810 | Suggestion | risk | `src/shared/ai_models/model_manager.py:287-293` | high | `_save_manifest` 非原子写入，崩溃可致 manifest.json 截断损坏 |
| AUD-31811 | Suggestion | risk | `src/shared/ai_models/model_manager.py:57, 538-543, 395-401` | medium | MODEL_REGISTRY 为模块级可变全局、单例创建无锁，`cancel_download` 在调用线程同步 wait(3000) |
| AUD-31812 | Suggestion | process | `src/shared/ai_models/model_manager.py:36-37, 170-171` | high | `ModelDownloadWorker.finished` 遮蔽 QThread 内置 finished；`Callable`/`asdict` 导入未用 |
| AUD-31813 | Suggestion | process | `src/shared/ai_models/nafnet.py:75` | high | 全模块无类型注解；构造器使用可变默认参数 |
| AUD-31814 | Suggestion | process | `src/shared/ai_models/onnx_env.py:60-66, 158-161` | medium | `_get_config()` 懒单例无线程保护；模块函数无类型注解；`get_configured_tile_size` 对配置值无合法性校验 |
| AUD-31815 | Suggestion | risk | `src/shared/ai_models/rife_interpolator.py:34, 68-71` | medium | 会话硬编码 CPUExecutionProvider，忽略用户 GPU 偏好与会话线程配置；`_SESSION_CACHE` 模块级无锁 |
| AUD-31816 | Major | bug | `src/shared/ai_models/swinir.py:213` | high | `PatchMerging.forward` 引用未定义名 `dim` → NameError，调用即崩（当前 DORMANT 未接线） |
| AUD-31817 | Suggestion | process | `src/shared/ai_models/swinir.py:116, 258-259` | high | 无类型注解；可变默认参数；`drop_path` 参数被静默替换为 nn.Identity |
| AUD-31901 | Major | perf-opportunity | `src/shared/app_restart.py:22-24` | low | 未处理冻结打包（frozen）场景：以 `[sys.executable, script_path]` 重启在 PyInstaller 等分发形态下会失败 |
| AUD-31902 | Suggestion | process | `src/shared/app_restart.py:32-39` | medium | subprocess.Popen 失败无异常处理，旧进程既未重启也未给出用户反馈 |
| AUD-31903 | Suggestion | risk | `src/shared/app_restart.py:41-42` | high | 以 time.sleep(0.3) 阻塞调用线程错峰端口竞争，属不可靠的时序假设 |
| AUD-3200 | Suggestion | test-gap | `src/app/feature_flags.py:15-21` | medium | 兼容包装层在生产与测试代码中均无调用方，且每次调用重复重建 flags（无缓存） |
| AUD-32001 | Major | bug | `src/shared/assets/icon_manager.py:304-306（ICON_MAP 同样问题见 :483）` | medium | icon_favorite_outline 实为实心星形，与 icon_favorite 完全相同，收藏两态视觉不可分辨 |
| AUD-32002 | Major | bug | `src/shared/assets/icon_manager.py:170-172（ICON_MAP` | medium | 'fa5s.object-intersect' 字形在 qtawesome FA5 solid charmap 中不存在，布尔交集按钮图标恒为空白 |
| AUD-32003 | Major | bug | `src/shared/assets/icon_manager.py:83-108` | medium | icon_mask 的 SVG 成功路径完全忽略 color 参数，蒙版激活态橙色高亮永不生效 |
| AUD-32004 | Suggestion | process | `src/shared/assets/icon_manager.py:46-50` | high | get_icon 使用宽泛 except Exception，吞掉 qtawesome 的具体错误类型（铁律：不得宽泛 except 掩盖领域错误） |
| AUD-32005 | Suggestion | process | `src/shared/assets/icon_manager.py:86-103` | high | icon_mask 每次调用都重新执行磁盘存在性检查 + SVG 解析 + 64×64 栅格化，无缓存 |
| AUD-32006 | Suggestion | process | `src/shared/assets/icon_manager.py:174-176 与 :513` | high | gradient 图标两个入口不一致：函数用 'ei.tint'，ICON_MAP 用 'fa5s.sliders-h' |
| AUD-32007 | Suggestion | process | `src/shared/assets/icon_manager.py:610（QToolButton 仅在 :625 函数体内局部导入）` | high | make_link_button 返回注解 "QToolButton" 缺少 TYPE_CHECKING 导入，静态检查无法解析 |
| AUD-32008 | Suggestion | process | `src/shared/assets/icon_manager.py:79` | high | icon_hidden 默认色 '#444444' 为魔法字面量，游离于 IconColors 常量体系之外 |
| AUD-32009 | Suggestion | process | `src/shared/assets/__init__.py:1（根因配合 icon_manager.py 未定义` | high | 星号 re-export 且上游无 __all__，将 os/qta/logger 等模块级名字一并泄漏到包命名空间 |
| AUD-3201 | Major | bug | `src/app/feature_registry.py:89-90` | medium | `register_save_handlers` 调用未按 R2-4 意图做异常隔离，异常可沿注册链上抛导致半注册状态 |
| AUD-3202 | Major | bug | `src/app/feature_registry.py:176-177, 190-191` | medium | `on_page_enter`/`on_page_leave` 分发无异常隔离，单模块异常可中断页面切换并阻断其余模块通知 |
| AUD-3203 | Suggestion | process | `src/app/feature_registry.py:219-221` | high | `reload_i18n` 忽略 `I18N.reload_locale` 的失败返回值，失败时仍记录成功日志 |
| AUD-3204 | Suggestion | process | `src/app/feature_registry.py:37-53, 92` | medium | `register_module` 返回值无任何调用方消费，False 的两种语义不可区分 |
| AUD-3205 | Suggestion | process | `src/app/feature_registry.py:2` | high | 未使用的 `Any` 导入 |
| AUD-32101 | Major | bug | `src/shared/codec/color_metadata_config.py:69-76` | medium | `__post_init__` 警告宣称回退到 'bt709'/'limited' 但从不实际改写字段，无效值继续下传编码器 |
| AUD-32102 | Suggestion | process | `src/shared/codec/color_metadata_config.py:43-45` | medium | HDR10 静态元数据（mastering_display/max_cll/max_fall）零验证，`Optional[tuple]` 未参数化 |
| AUD-32103 | Suggestion | process | `src/shared/codec/color_metadata_config.py:79` | high | `from_metadata` 注解引用 `"VideoMetadata"` 但该名称未导入（无 TYPE_CHECKING 兜底） |
| AUD-32104 | Major | risk | `src/shared/codec/decode_worker_budget.py:123-127（同型代码 134-137）` | medium | `_apply_updates` 在锁外执行，两个并发 acquire/release 的旧快照可后到先应用，最终下发过期的 worker 数 |
| AUD-32105 | Suggestion | process | `src/shared/codec/decode_worker_budget.py:144-150` | medium | `bind()` 替换回调后不回放当前 assigned 值，绑定瞬间错过 rebalance 的上下文将长期运行在过期值 |
| AUD-32106 | Suggestion | risk | `src/shared/codec/decode_worker_budget.py:112-114` | medium | `acquire(capacity=...)` 永久改写进程级单例容量，且与 slice_threads_policy 的进程级线程值存在跨模块踩踏窗口 |
| AUD-32107 | Suggestion | process | `src/shared/codec/decode_worker_budget.py:70、195-215` | high | 类型注解缺口：`__exit__(self, *_exc)`、`describe() -> dict` 等未参数化 |
| AUD-32108 | Major | bug | `src/shared/codec/slice_threads_policy.py:48-70` | medium | `_native_set`/`_native_get` 用裸 `except Exception` 吞掉包括 ToposCodecError 在内的领域错误且零日志，native 下发失败完全不可见 |
| AUD-32109 | Suggestion | process | `src/shared/codec/slice_threads_policy.py:56-57、66-68（对照 topos_binding.py:1253-1279、2794-2797）` | medium | 每次 native 调用都新构 `ToposCodec()`，连带 load_library 全路径搜索与全量签名重配，register/unregister 反复付费 |
| AUD-32110 | Suggestion | process | `src/shared/codec/slice_threads_policy.py:37-43` | high | 类型注解缺口：`bind(setter=None, getter=None)` 无参数/返回注解，`_order: list`、`_setter/_getter` 未参数化 |
| AUD-32201 | Major | bug | `src/shared/codec/topos_binding.py:4166` | medium | movie_config 未验证 width/height/qp/timescale 数值域，ctypes 静默回绕可落入合法域并绕过 native 校验 |
| AUD-32202 | Major | risk | `src/shared/codec/topos_binding.py:4362` | medium | ParallelSequentialDecoder worker 在 try 块之前的异常导致主线程永久阻塞且泄漏 fd |
| AUD-32203 | Major | risk | `src/shared/codec/topos_binding.py:2488` | medium | ToposMuxFile 顺序写回调未持实例锁，与 seek_write 回调的加锁不对称（Windows 下文件指针竞争） |
| AUD-32204 | Major | bug | `src/shared/codec/topos_binding.py:3396` | medium | decode_reduced/decode_request/decode_batch 未复用 C4 TLS scratch，逐帧全平面零填分配 |
| AUD-32205 | Suggestion | risk | `src/shared/codec/topos-color-multitask /topos-color01/src/shared/codec/topos_binding.py:4358` | medium | ParallelSequentialDecoder 死代码与全局线程池副作用不恢复 |
| AUD-32206 | Suggestion | process | `src/shared/codec/topos_binding.py:1090` | medium | ToposDecoder 平面输入校验缺口：make_views 不查 dtype/C 连续，max_slice_workers 负值回绕，batch 不校验 views_list 长度 |
| AUD-32207 | Suggestion | process | `src/shared/codec/topos_binding.py:202` | medium | 多个 public API 参数缺类型注解 |
| AUD-32208 | Suggestion | doc-gap | `src/shared/codec/topos_binding.py:3108` | high | 杂项：encode() 文档引用不存在的方法；decode() 死变量 widths |
| AUD-32300 | Suggestion | process | `src/shared/codec/topos_image_binding.py:609` | medium | decode 最终错误路径未拼入 Python 侧 I/O 回调详情（`_with_io_detail` 覆盖不一致） |
| AUD-32301 | Suggestion | process | `src/shared/codec/topos_image_binding.py:770-773` | medium | write_packet 使用固定 `.toos-tmp` 临时名——并发写同一目标路径时互相破坏 |
| AUD-32302 | Suggestion | risk | `src/shared/codec/topos_image_binding.py:871-872, 890-891` | medium | `durable=True` 未 fsync 父目录，原子替换的崩溃持久性不完整 |
| AUD-32303 | Suggestion | risk | `src/shared/codec/topos_image_binding.py:773-821` | medium | `os.open` 与首个 try 之间存在无保护间隙，异常时泄漏 fd 与临时文件 |
| AUD-32304 | Suggestion | risk | `src/shared/codec/topos_image_binding.py:105-125` | medium | HALF LUT 惰性初始化无锁（良性竞争、重复建表） |
| AUD-32305 | Suggestion | process | `src/shared/codec/topos_image_binding.py:406-407, 423-430, 432` | medium | `tc_image_derive_idsc`/`tc_image_write` 未做 hasattr 特性检测，旧库加载即抛 AttributeError |
| AUD-32306 | Suggestion | process | `src/shared/codec/topos_image_binding.py:987, 955, 367` | high | 类型注解缺失/不精确（`byref` 无返回注解、`library_identity` 裸 `tuple`、`ToposImageInfo.raw` 裸 `dict`） |
| AUD-32307 | Major | bug | `src/shared/codec/topos_meta.py:123-127（调用方 src/shared/codec/topos_image_binding.py:930-934）` | medium | decode_topos_meta 的 UTF-8 解码抛 UnicodeDecodeError 而非 ToposMetaError，击穿"任何失败一律降级"契约 |
| AUD-32308 | Suggestion | process | `src/shared/codec/topos_profiles.py:330-337, 396-398, 318-322` | medium | `available_topos_tiers()` 不过滤 `available=False` 档位；AVAILABLE_TOPOS_TIERS 注释与 raw 档 `available=True` 相互矛盾 |
| AUD-32309 | Suggestion | process | `src/shared/codec/topos_profiles.py:795-800, 713-715, 739-741` | medium | `default_topos_image_tier_id` 家族内多 default 歧义——raw16-4 的 `default=True` 永不可达 |
| AUD-32310 | Suggestion | process | `src/shared/codec/topos_profiles.py:123-126, 582` | high | `_bpp_from_mb_s` 与 `TRAW_RATIO_NOMINAL` 无引用死代码 |
| AUD-32311 | Suggestion | process | `src/shared/codec/topos_trwm.py:63-78（对照 98-106）` | medium | encode_trwm 不校验 wb 增益正有限与 stops/black 有限——编码域与解码域校验不对称 |
| AUD-32400 | Major | bug | `src/shared/color/__init__.py:26-32` | medium | 注释声称"延迟导入"color_picker，实际为顶层立即导入，未实现解耦目标 |
| AUD-32401 | Major | bug | `src/shared/color/color_picker.py:437,448-454` | medium | 屏幕吸管混合逻辑坐标与物理像素坐标，HiDPI/Retina 屏上取到错误位置的颜色 |
| AUD-32402 | Major | risk | `src/shared/color/color_picker.py:2754-2755（另见 437、2444）` | medium | get_color() 每次调用泄漏整个选择器 widget 树及全屏截图 QImage，注释与 WA_DeleteOnClose=False 的实际代码相反 |
| AUD-32403 | Major | bug | `src/shared/color/color_picker.py:1942-1948（配合 1784、1874）` | medium | 导入色板 JSON 完全无结构验证，恶意/畸形数据导致 UI 与数据不一致或异常 |
| AUD-32404 | Major | bug | `src/shared/color/color_picker.py:1973-1994` | medium | ASE 导出文件格式无效：头部颜色计数把占位色计入但循环跳过，且每个颜色块缺少 4 字节块长度字段 |
| AUD-32405 | Major | bug | `src/shared/color/color_picker.py:1848-1856、2637-2640` | medium | 色板页 RGB 输入的连续中间值经 colorSelected 信号灌入并挤掉颜色历史记录 |
| AUD-32406 | Major | bug | `src/shared/color/color_picker.py:216-223、1227、1288-1296（死常量 183-194）` | medium | 溢色检查对 Rec.709/ACEScg 回退按 sRGB 矩阵计算，警告结果错误；ACESCG_TO_XYZ 为死代码 |
| AUD-32407 | Major | bug | `src/shared/color/color_picker.py:1599-1604、2494-2505（消费点 1784）` | medium | 用户配置文件内容未做结构验证，畸形数据使取色器构造抛未捕获异常 |
| AUD-32408 | Suggestion | process | `src/shared/color/color_picker.py:401-407` | medium | IconButton.paintEvent 每次重绘都重新解析两份 SVG（QSvgRenderer 无缓存） |
| AUD-32409 | Suggestion | process | `src/shared/color/color_picker.py:498-504` | high | HueSlider.paintEvent 逐像素 setPen+drawLine 绘制 250 条线，无缓存 |
| AUD-32410 | Suggestion | process | `src/shared/color/color_picker.py:44-50、54-71、2608-2613` | high | 配置读写与重置用宽泛 except Exception 吞错且仅 print 输出 |
| AUD-32411 | Suggestion | process | `src/shared/color/color_picker.py:1858-1866（对照签名 52-64、完整保存 2682-2683）` | medium | SwatchPage._confirm_color 的中间保存会先把 picker 自定义色板/历史瞬时写空，依赖紧随其后的第二次保存补救 |
| AUD-32412 | Suggestion | process | `src/shared/color/color_picker.py:2665-2683（另见 1164）` | high | 透明度不随 last_color/历史持久化，重开后丢失 |
| AUD-32413 | Suggestion | process | `src/shared/color/color_picker.py:72-81（未用导入）、147-152（COLOR_SPACES）、183-194（ACESCG_TO_XYZ）、240-246（convert_color_space）` | medium | 大量未使用导入与死代码 |
| AUD-32414 | Suggestion | process | `src/shared/color/color_picker.py:432-442` | high | 屏幕吸管只覆盖主显示器，多屏环境无法取副屏颜色 |
| AUD-32415 | Suggestion | process | `src/shared/color/color_picker.py:52、196、270、286、1622、2269 等全文件` | high | 模块级函数与各方法普遍缺失类型注解 |
| AUD-32500 | Critical | bug | `src/shared/color/color_space_descriptor.py:89（叠加 81–86 的 LINEAR 分支）` | medium | `to_ocio_name` 对未映射 (primaries, transfer) 组合静默回退 "lin"，默认 OCIO 引擎把已编码信号当线性、整体跳过 ODT 编码 |
| AUD-32501 | Major | doc-gap | `src/shared/color/color_space_descriptor.py:364（docstring 声明在 332–336）` | medium | `descriptor_from_config` 声称受 `color.smart_default` 控制，但该键从未被读取——死配置 + 文档与行为不符 |
| AUD-32502 | Major | bug | `src/shared/color/color_space_descriptor.py:313–325` | medium | `collect_color_management_settings` 宽泛 `except Exception` 且零日志——工程保存时色彩管理设定可能被静默丢弃 |
| AUD-32503 | Suggestion | process | `src/shared/color/color_space_descriptor.py:232（` | high | 多处类型注解缺失/宽松 |
| AUD-32510 | Major | bug | `src/shared/color/color_transform_provider.py:505–506、538–539（对照 OCIO 版 692–693、720–721）` | medium | `BuiltinColorProvider.transform` LIMITED→LIMITED 时不做 range 展开，transfer 解码作用于压缩信号（与 OCIO 实现不对称） |
| AUD-32511 | Major | risk | `src/shared/color/color_transform_provider.py:732–736、739–751、769–770、800–802` | medium | 模块级 `_provider_cache`/`_provider_diagnostics` 无同步，导出 worker 线程与 UI 线程并发读写 |
| AUD-32512 | Suggestion | process | `src/shared/color/color_transform_provider.py:511–519、527–535` | medium | transform 的 transfer 分支用 `== ColorTransfer.PQ` 枚举等值比较，漏掉 ST2084 别名及 GAMMA22 等，未列 transfer 静默按线性处理 |
| AUD-32513 | Suggestion | process | `src/shared/color/color_transform_provider.py:149–158、167–176、199–208、218–227、248–252、258–261 等，链式调用 505–545` | high | 每个转换 helper 全图 `.copy()`，单次 transform 链式最多 7 次全图拷贝 |
| AUD-32514 | Suggestion | process | `src/shared/color/color_transform_provider.py:445–467` | high | `_apply_primaries_np` 每次调用对常量矩阵做运行时 `np.linalg.inv` |
| AUD-32515 | Suggestion | process | `src/shared/color/color_transform_provider.py:492–495、688（死导入）；619–632（注释 "explicit > bundled fallback" 但` | high | 未使用导入与注释-行为不符：transform 内导入的 `ColorSpaceDescriptor`/`LINEAR_SCENE` 从未使用；显式 config 路径无效时并不回退 bundled config（与注释矛盾） |
| AUD-32520 | Suggestion | process | `src/shared/color/lut_loader.py:85–99、101–106` | high | `parse_cube` 头部关键字缺参/坏参抛裸 IndexError/ValueError；DOMAIN_MIN/MAX 少于 3 值静默通过、采样期才以 broadcast 错误炸出 |
| AUD-32521 | Suggestion | process | `src/shared/color/lut_loader.py:156–160、173–178` | high | `parse_3dl` 允许 size=0 头通过校验，随后 `data.max()` 对空数组抛 "zero-size array reduction" |
| AUD-32530 | Suggestion | process | `src/shared/color/types.py:84、92` | high | ST2084 作为 PQ 的双重枚举别名构成等值比较陷阱（本批已在 provider 侧发现不一致检查） |
| AUD-32531 | Suggestion | process | `src/shared/color/types.py:252–265（primaries 名表无 AP1）、271–285（transfer 名表无 TRAW_LOG0）` | medium | 显示名映射缺 ColorPrimaries.AP1 与 ColorTransfer.TRAW_LOG0，均落 'Unknown' |
| AUD-32601 | Critical | bug | `src/shared/config/config_manager.py:647-648` | medium | `get()` 不支持 `default` 参数，多处两参调用抛 TypeError，被 except 吞掉后偏好静默失效（含 HDR 预览 tonemap 与色彩引擎选择） |
| AUD-32602 | Major | bug | `src/shared/config/config_manager.py:383-390` | medium | `load_config` 对非 JSONDecodeError 的加载异常不备份，直接用默认配置覆盖原用户配置文件 |
| AUD-32603 | Major | bug | `src/shared/config/config_manager.py:613-614, 636` | medium | `_save_config_dict` 吞掉所有保存异常，`save_config` 仍清空 `_dirty_keys`，待存偏好在持久化失败时静默丢失 |
| AUD-32604 | Major | bug | `src/shared/config/config_manager.py:616-636（关键 619-632）` | medium | `save_config` 空 dirty 路径用内存快照全量覆盖磁盘，多实例场景会回滚其他实例刚写入的配置键 |
| AUD-32605 | Major | risk | `src/shared/config/config_manager.py:659-662（对照 629 的` | medium | `set()` 在锁外修改 `self.config` 与 `_dirty_keys`，与持锁迭代 `_dirty_keys` 的 `save_config()` 构成数据竞争 |
| AUD-32606 | Suggestion | process | `src/shared/config/config_manager.py:367, 388, 618, 634` | medium | `DEFAULT_CONFIG` 全程浅拷贝，嵌套容器（features/backend/recent_files/page_layouts 等）与类属性共享同一对象 |
| AUD-32607 | Suggestion | process | `src/shared/config/config_manager.py:294, 361, 647, 650` | high | 公开方法缺少类型注解（`get`/`set`/`load_config`/`__init__` 参数） |
| AUD-32608 | Suggestion | process | `src/shared/config/config_manager.py:361-390, 650-662` | medium | 外部数据（config.json 值 / `set()` 传入值）缺乏类型与值域校验 |
| AUD-32609 | Suggestion | process | `src/shared/config/config_manager.py:616-636, 591-614（使用侧证据：video_denoise_pipeline.py:30-36、edit_controller.py:5338-5350 等）` | medium | 每次 `set(auto_save=True)` 全量读盘+全量写盘+fsync；"临时 `ConfigManager()` 读一个键"的模式重复整文件解析 |
| AUD-32800 | Major | bug | `src/shared/control_surfaces/tangent/hub_client.py:165-175（配合 :187 的` | medium | `sendall` 在 0.25s socket 超时下可能部分发送，返回 False 后连接保持"健康"，导致出站帧流损坏 |
| AUD-32801 | Suggestion | risk | `src/shared/control_surfaces/tangent/hub_client.py:131-134（另见 :98-105）` | medium | `stop()` 的有界 join 超时后不检查残留线程，紧接的 `start()` 可被 `is_running` 误判而空跑，集成静默失效 |
| AUD-32802 | Suggestion | process | `src/shared/control_surfaces/tangent/hub_client.py:322-333` | medium | INITIATE_COMMS 握手发送失败仅记 debug 日志，仍发出 `panels_received` 并继续会话 |
| AUD-32803 | Suggestion | risk | `src/shared/control_surfaces/protocol.py:80-83（触发点 :141` | medium | `_pack_f32` 对超出 float32 范围的值抛 `OverflowError` 而非约定的 `ValueError`，会从 `send_parameter_value` 未捕获地传播到调用线程 |
| AUD-32804 | Suggestion | process | `src/shared/control_surfaces/protocol.py:175-176（对照 :185-188、:200-205 的容忍策略）` | medium | INITIATE_COMMS 解码严格要求 `offset == len(payload)`，与其他命令"容忍尾部扩展字段"的策略不一致，Hub 版本追加字段会使握手永久失败 |
| AUD-32901 | Major | bug | `src/shared/db/database.py:110-116（另 118-126 同模式）` | medium | 排序热路径上每次调用新建/关闭 SQLite 连接，无复用无批量接口 |
| AUD-32902 | Major | risk | `src/shared/db/database.py:54-84` | medium | layouts 单主键→复合主键迁移非原子，崩溃窗口内可永久丢失全部布局预设 |
| AUD-32903 | Major | risk | `src/shared/db/database.py:110-126、128-141、143-239（全部读写方法同模式）` | medium | 全部方法无 try/finally，异常路径连接泄漏且异常直透 UI 槽 |
| AUD-32904 | Suggestion | process | `src/shared/db/database.py:175、214` | high | json.loads 直接解析持久化字段，无损坏数据防御 |
| AUD-32905 | Suggestion | process | `src/shared/db/database.py:116` | medium | get_file_rating 对 NULL rating 返回 None 而非 0 |
| AUD-32906 | Suggestion | process | `src/shared/db/database.py:229-237（save_default_layout 硬编码` | medium | 内置 'Default' 预设名与用户预设名共享主键，互相覆盖（潜在） |
| AUD-32907 | Suggestion | process | `src/shared/db/database.py:5、139` | high | QByteArray 导入未使用；set_setting 对任意对象盲目 str() |
| AUD-32908 | Suggestion | process | `src/shared/db/database.py:8-239（所有函数与方法）` | high | 全文件无类型注解 |
| AUD-33001 | Suggestion | process | `src/shared/diagnostics/flash_diag.py:43-45` | high | 每条事件重新 open/close 文件，高频发布链路下 I/O 开销放大 |
| AUD-33002 | Suggestion | process | `src/shared/diagnostics/flash_diag.py:41-47` | high | `except Exception: pass` 完全静默，诊断失效无从察觉 |
| AUD-33003 | Suggestion | process | `src/shared/diagnostics/flash_diag.py:39-40` | high | `record.update(fields)` 允许调用方关键字覆盖保留字 `t`/`k` |
| AUD-33010 | Major | bug | `src/shared/diagnostics/performance_bundle.py:44-46` | medium | 绝对路径正则漏掉 `/Volumes`、`/root`、UNC 路径，素材路径可未脱敏进入导出报告 |
| AUD-33011 | Suggestion | process | `src/shared/diagnostics/performance_bundle.py:134-151` | medium | `machine` 段绕过 `sanitize_value`，脱敏契约仅靠外部函数自觉维持 |
| AUD-33012 | Suggestion | process | `src/shared/diagnostics/performance_bundle.py:158-166` | medium | `export_performance_bundle` 不校验输出路径，且非原子写入可残留半截 JSON |
| AUD-33013 | Suggestion | process | `src/shared/diagnostics/performance_bundle.py:71-74` | high | 类型注解 `Optional[Any]` 恒等于 `Any`，无约束意义 |
| AUD-33020 | Major | bug | `src/shared/diagnostics/resource_snapshot.py:186-195` | high | `cpu_percent` 每次新建 `psutil.Process()`，首调用恒返回 0.0，指标永远为零 |
| AUD-33021 | Major | risk | `src/shared/diagnostics/resource_snapshot.py:72-78` | medium | 注册表对 provider 持强引用且生产代码从不注销，渲染器对象图泄漏 + 快照调用已关闭对象 |
| AUD-33022 | Major | risk | `src/shared/diagnostics/resource_snapshot.py:138-151` | medium | `stop()` join 超时后无条件置空线程引用，重启会产生双采样线程且僵尸线程永不退出 |
| AUD-33100 | Critical | process | `src/shared/export/export_panel.py:49-53（generate_export_filename）；触发链 896 → 1444-1469（update_naming_preview）` | medium | 自定义命名模式输入非法 format 串触发未捕获 ValueError，Pattern 输入框每敲一键即调用，键入 `{` 即崩 |
| AUD-33101 | Major | process | `src/shared/export/export_panel.py:1210-1213（_on_naming_mode_changed）；1354 与 1417（把 currentText 传入 generate_export_filename，其内部 L35-55 全为英文比较）` | medium | 命名模式以本地化显示文本与硬编码英文比较/传参，非英文 locale 下命名功能整体失效（含默认模式的后缀行） |
| AUD-33102 | Major | bug | `src/shared/export/export_panel.py:1279-1290（连接于 949/962）` | medium | `update_output_size` 先读值后 setRange 引发重入 valueChanged，标签以陈旧值计算，导出尺寸与控件实际值偏差约 4 倍 |
| AUD-33103 | Major | bug | `src/shared/export/export_panel.py:1375-1377 与 1434-1436（写入侧 1269/1314，early-return 1274）` | medium | resize 尺寸以解析显示标签文本为数据源，orig_w==0 时导出 resize=(0,0)，且设计脆弱（对话框版已实际爆雷，见 AUD-33110） |
| AUD-33104 | Suggestion | process | `src/shared/export/export_panel.py:1184-1187, 1283-1289, 1312-1313` | high | 直接操纵 DragValue 私有成员 `_suffix`/`_update_display()`（含 reset 路径），而公共 `setSuffix()` 存在且行为等价 |
| AUD-33105 | Suggestion | process | `src/shared/export/export_panel.py:661, 680` | high | `_source_label`/`_timeline_label` 全文件从未创建，set_export_source/set_timeline_id 的 UI 更新分支是死代码 |
| AUD-33106 | Suggestion | process | `src/shared/export/export_panel.py:1250, 1269/1314, 1469, 1092, 1237-1241, 1228` | medium | 多处 UI 文案绕过 i18n 硬编码英文，与初始化的 i18n 文本混排 |
| AUD-33107 | Suggestion | process | `src/shared/export/export_panel.py:503-509` | high | `set_auto_file` 移除旧 auto 项后若新路径已是手动项则早退，不发 files_changed |
| AUD-33108 | Suggestion | process | `src/shared/export/export_panel.py:470-474, 478-483` | medium | 拖放/添加不校验文件类型，任意文件可入导出队列（外部输入未验证） |
| AUD-33109 | Suggestion | process | `src/shared/export/export_panel.py:1401-1403` | high | `_start_batch_export` 输出目录无效时静默 return，用户点击导出无任何反馈 |
| AUD-33110 | Major | bug | `src/shared/export/export_dialog.py:273-274（写入侧为 89/206/247）` | medium | `get_settings` 解析标签文本用英文 "Result: " 前缀，而标签写入用 `I18N.tr("export.result")`（zh="结果"），非英文 locale 勾选 resize 后读取设置即抛 ValueErro |
| AUD-33111 | Major | bug | `src/shared/export/export_dialog.py:214-221（连接于 81/87）` | medium | `update_output_size` 先读 `val` 再 `setRange` 触发 QSpinBox 重入 `valueChanged`，标签最终用陈旧值计算，与控件实际值不符 |
| AUD-33112 | Suggestion | process | `src/shared/export/export_dialog.py:259-260, 276-278（combo 项为 107-113 的` | medium | `fmt_map`/格式判断用硬编码英文键匹配 combo 的 I18N 显示文本，仅因各 locale 均未翻译格式名而侥幸未爆 |
| AUD-33113 | Suggestion | process | `src/shared/export/export_dialog.py:210-211, 229-231, 244-245` | medium | `update_output_size` 只防护 `orig_w == 0`，`orig_h == 0` 时 height/short_edge 分支除零 |
| AUD-33114 | Suggestion | process | `src/shared/export/export_dialog.py:13-280（setup_ui/get_settings/update_output_size 等均无参数与返回注解）` | high | ExportDialog 全类方法缺类型注解 |
| AUD-33115 | Suggestion | doc-gap | `src/shared/export/batch_queue_store.py:119-137` | medium | `save_batch_queue(settings=None)` 整文件覆写会抹掉已持久化的 settings，与 docstring 声明的“不覆盖”语义矛盾 |
| AUD-33116 | Suggestion | process | `src/shared/export/batch_queue_store.py:173` | medium | `load_batch_queue` 对 items 元素无类型校验，单个畸形项导致整表返回空 |
| AUD-33117 | Suggestion | process | `src/shared/export/batch_queue_store.py:155-157, 176-178, 188-189` | medium | 三个入口均 `except Exception` 宽泛捕获，吞掉编程错误与磁盘/权限错误的区分 |
| AUD-33118 | Suggestion | process | `src/shared/export/encoder.py:177-184` | high | `quality`（0-100）直接映射 `crf`，超出 x264/x265 合法范围（0-51） |
| AUD-33119 | Suggestion | perf-opportunity | `src/shared/export/encoder.py:404-440` | high | `ExportConfig` 未冻结且 `framerate: Optional[Any]` 弱类型；`get_total_frames` 无顺序校验可返回负数 |
| AUD-33200 | Suggestion | process | `src/shared/export/export_stage_telemetry.py:386` | medium | 阶段 backend 聚合取最后一个采样（last-wins），可能掩盖导出中途的后端切换 |
| AUD-33201 | Suggestion | process | `src/shared/export/export_stage_telemetry.py:195-198` | medium | full_gpu 上传门豁免方向存疑：cpu_to_gpu_upload 阶段被采样时正的 upload 字节不产生 blocker |
| AUD-33202 | Suggestion | process | `src/shared/export/export_stage_telemetry.py:345-348` | medium | set_native_encode_stage_stats 接受任意对象且无结构校验，序列化错误延迟到导出结束才暴露 |
| AUD-33203 | Major | bug | `src/shared/export/frame_provider.py:448` | medium | read_frame_at 调用 MediaSource Protocol 之外的 read_next_frame，且不校验返回帧号 |
| AUD-33204 | Suggestion | process | `src/shared/export/frame_provider.py:151-162` | medium | _probe_metadata 宽泛 except Exception 吞掉探测期领域错误后继续运行 |
| AUD-33205 | Major | bug | `src/shared/export/gif_encoder.py:126-144` | medium | close() 将全部已落盘帧重新载入内存，违背模块声明的“不在 RAM 保留完整动画”设计目标 |
| AUD-33206 | Suggestion | process | `src/shared/export/gif_encoder.py:193-196` | medium | 浮点帧范围启发式（max<=1.0 即视为归一化 [0,1]）在边界数据上会产生错误缩放 |
| AUD-33207 | Suggestion | process | `src/shared/export/gpu_benchmark_report.py:211-218` | high | _atomic_write_text 写入失败时遗留 .tmp 临时文件（delete=False 且无异常清理） |
| AUD-33208 | Suggestion | doc-gap | `src/shared/export/gpu_encode_surface.py:115-128` | medium | submit() 回调返回 False（文档化失败信号）时不自动 release，lease 释放依赖调用方纪律 |
| AUD-33209 | Suggestion | process | `src/shared/export/gpu_encode_surface.py:34` | high | descriptor.planes 声明为 Sequence（可变），frozen dataclass 不保证深层不可变 |
| AUD-33210 | Suggestion | process | `src/shared/export/gpu_trail.py:252-259（对照 303-309、319-339）` | medium | record_stage 与 record_stage_sample 混用同名段时守卫不对称，stage 登记证据被样本聚合静默覆盖 |
| AUD-33211 | Suggestion | process | `src/shared/export/presets.py:127-142（LT 同 144-159）` | medium | ProRes PROXY/LT 预设声明 bit_depth=8 + yuv422p，与 Apple ProRes 全系 10-bit 规格不符 |
| AUD-33212 | Suggestion | doc-gap | `src/shared/export/presets.py:497-521` | high | get_dnxhr_profile_for_resolution 的 width/height 参数完全未使用，与 docstring 承诺不符 |
| AUD-33213 | Suggestion | process | `src/shared/export/presets.py:630-634（预设定义 212-227）` | medium | get_recommended_preset('vfx', is_hdr=True) 返回 BT709/SDR 色彩标签的 ProRes 4444 XQ，HDR 用例元数据误标 |
| AUD-33214 | Suggestion | process | `src/shared/export/presets.py:57-63` | high | ContainerFormat 导入未使用 |
| AUD-33301 | Major | bug | `src/shared/export/quick_export_dialog.py:583` | medium | 容器-编码表硬编码且已与 deliver 编解码注册表漂移 |
| AUD-33302 | Major | bug | `src/shared/export/quick_export_dialog.py:492` | high | LUT 默认目录依赖 os.getcwd()，随进程工作目录漂移 |
| AUD-33303 | Suggestion | process | `src/shared/export/quick_export_dialog.py:1176（另见 1228、1260、1295）` | medium | 本地化显示文本被用作语义映射键，与 userData 方案不一致 |
| AUD-33304 | Suggestion | process | `src/shared/export/quick_export_dialog.py:1117` | high | 宽高比联动基于原图尺寸且 int 截断，orig=(0,0) 时高度塌缩为 1 |
| AUD-33305 | Suggestion | process | `src/shared/export/quick_export_dialog.py:1189` | high | 图像页文件名未做非空/非法字符校验 |
| AUD-33306 | Suggestion | process | `src/shared/export/quick_export_dialog.py:1156` | high | 公共方法与 get_settings 缺类型注解 |
| AUD-33310 | Suggestion | process | `src/shared/export/render_cache.py:201` | high | get() 检测到损坏条目只计数不清理，失败被重复支付 |
| AUD-33311 | Suggestion | process | `src/shared/export/render_cache.py:35` | medium | 不可结构化字段回退 repr，digest 可能含对象地址而不稳定 |
| AUD-33312 | Suggestion | process | `src/shared/export/render_cache.py:243` | medium | invalidate() 只捕 FileNotFoundError，Windows 被占用文件抛 PermissionError 直接上抛 |
| AUD-33320 | Critical | bug | `src/shared/export/sequence_exporter.py:332` | medium | packed 整数帧数据不归一化，uint8 输入导出为全白/255 倍过曝图像 |
| AUD-33321 | Major | bug | `src/shared/export/sequence_exporter.py:367` | medium | `_planar_to_rgba` 少于 3 平面时 u/v 未绑定，帧导出必然抛错 |
| AUD-33322 | Major | bug | `src/shared/export/sequence_exporter.py:654` | high | 16-bit PNG 使用 Pillow 不支持的 'RGBA;16' 模式，导出必失败（已实测复现） |
| AUD-33323 | Major | bug | `src/shared/export/sequence_exporter.py:544` | medium | EXR/TIFF/DPX 依赖链整体缺失且未声明，默认 vfx 推荐路径在当前环境必然失败 |
| AUD-33324 | Major | bug | `src/shared/export/sequence_exporter.py:622` | medium | 格式降级静默更换实际文件，返回/记录路径与磁盘不符；宽泛 except 掩盖领域错误 |
| AUD-33325 | Suggestion | process | `src/shared/export/sequence_exporter.py:373` | medium | 色度上采样假设整数倍缩放，非整除分辨率形状失配 |
| AUD-33326 | Suggestion | process | `src/shared/export/sequence_exporter.py:465` | high | LOG 色彩策略实现与注释公式不符 |
| AUD-33327 | Suggestion | process | `src/shared/export/sequence_exporter.py:800` | high | cleanup() 先 clear 后取 len，清理计数恒为 0 |
| AUD-33328 | Suggestion | process | `src/shared/export/sequence_exporter.py:108` | high | SequenceExportConfig/metadata/start_frame 为死代码；FrameInput 类型别名无意义 |
| AUD-33400 | Major | risk | `src/shared/export/topos_encoder.py:1866（吞错）、1892-1893（无 journal 判死）、1942-1946（state 非 prepared → rmtree）` | medium | 序列事务日志写失败被吞掉，两阶段提交安全机制随之静默失效（活暂存被并发清除 / 崩溃后备份连同旧帧一起被删） |
| AUD-33401 | Major | risk | `src/shared/export/topos_encoder.py:1902-1908（os.kill 判活）、1837-1840（恢复入口在提交锁之外）` | medium | 暂存属主活性判定依赖 PID（复用敏感、跨主机失真），且恢复扫描不持提交锁——网络盘多主机场景可误删活事务 |
| AUD-33402 | Major | bug | `src/shared/export/topos_encoder.py:1763（直接引用）、463-467（基类构造显式接受该模式）、3485（仅子类赋值）` | medium | 基类以 `container_mode="image_sequence"` 运行时引用未定义属性 `_image_tier`，首帧即 AttributeError |
| AUD-33403 | Suggestion | risk | `src/shared/export/topos_encoder.py:1580-1591（置 None）vs 1485-1488（worker 访问）` | medium | `_pipeline_discard` join 超时后置空共享队列/条件变量，与仍在运行的转换 worker 竞争，后台线程抛 AttributeError 噪声 |
| AUD-33404 | Suggestion | process | `src/shared/export/topos_encoder.py:3294-3299（无几何校验）、3013-3014（reshape）、3022-3029（永久降级）` | medium | packed 路径缺少 `data.shape` 与帧宽高/导出配置的一致性校验；尺寸巧合时 reshape 静默错排，失败时单帧畸形输入永久禁用 native 融合转换 |
| AUD-33405 | Suggestion | risk | `src/shared/export/topos_encoder.py:2516-2520（计数器）、3221（调用点）、1485/1498（worker 线程执行` | medium | 转换 worker 线程并发读写 `_frames_domain_checked` 与告警标志，无同步（抽样调度不确定） |
| AUD-33406 | Suggestion | process | `src/shared/export/topos_encoder.py:2605-2608` | high | `_seg_discard` 未 `cancel_futures=True`，abort/定稿失败后已排队 GOP 段仍在后台完整编码 |
| AUD-33407 | Suggestion | process | `src/shared/export/topos_encoder.py:2990-2994、3384-3387（两处同型）` | medium | packed 转换的 matrix 兜底静默替换为 bt709——当前不可达，但与码表无同步约束，属潜伏的隐性色彩偏移 |
| AUD-33408 | Suggestion | process | `src/shared/export/topos_encoder.py:1602-1605 与 1610-1611（重复）、2231-2232（无效赋值）` | medium | 代码健康：close() 内不可达的重复 0 帧检查、`_audio_warned` 无效赋值 |
| AUD-33501 | Suggestion | process | `src/shared/export/topos_rate_control.py:185-190` | medium | anchored() 会覆盖本帧 _account 新置位的 reseed_hint |
| AUD-33502 | Suggestion | process | `src/shared/export/topos_rate_control.py:158-164` | medium | 全零字节历史可使 _next_qp 触发 math.log2 定义域异常 |
| AUD-33503 | Suggestion | process | `src/shared/export/topos_rate_control.py:79-83, 211` | medium | cut_factor/lend_cap_factor 无符号校验；telemetry() 返回类型未参数化 |
| AUD-33504 | Critical | bug | `src/shared/export/video_encoder.py:1286-1287（对照 476-478、439-441）` | medium | VP9 编码参数（crf/bitrate/preset/gop_size）全部未下发给编码器 |
| AUD-33505 | Major | bug | `src/shared/export/video_encoder.py:2234-2235` | high | 音频码率选项键 'b:a' 会被 avcodec_open2 静默忽略 |
| AUD-33506 | Major | bug | `src/shared/export/video_encoder.py:2339-2347（对照 open() 的 981-985 清理）` | medium | close() 异常路径：容器未显式关闭、半成品文件不清理、不可重试 |
| AUD-33507 | Major | bug | `src/shared/export/video_encoder.py:1914-1918（对照 BGR 分支 1901-1905 的正确处理）` | medium | RGB 序 16-bit 4 通道 packed 帧落入 uint8 'rgba' 格式，转换必失败 |
| AUD-33508 | Major | risk | `src/shared/export/video_encoder.py:1706-1708, 1716-1719（契约注释 363-364、队列 794-796）` | medium | 多转换线程下 _converted_map 无界，注释宣称的内存上界不成立 |
| AUD-33509 | Major | bug | `src/shared/export/video_encoder.py:2227-2232 与 2270-2271` | medium | 音频流声道布局与帧声道布局两套推导不一致 |
| AUD-33510 | Suggestion | process | `src/shared/export/video_encoder.py:2166-2169` | high | _create_planar_av_frame 的打包分支硬编码 'rgb24'（当前不可达） |
| AUD-33511 | Suggestion | process | `src/shared/export/video_encoder.py:1124-1144` | medium | 硬编探针 CodecContext 未显式关闭 |
| AUD-33512 | Suggestion | process | `src/shared/export/video_encoder.py:408-452` | medium | FFmpegEncoderConfig 缺 fps/crf/threads 等构造期验证 |
| AUD-33513 | Suggestion | process | `src/shared/export/video_encoder.py:429-432` | medium | yuv420p + has_alpha 被静默降级为无 alpha，仅告警 |
| AUD-33514 | Suggestion | process | `src/shared/export/video_encoder.py:786-790` | high | 类型注解不完整（未参数化的容器类型） |
| AUD-33600 | Major | risk | `src/shared/export/video_export_worker.py:332` | medium | cleanup() 忽略 wait() 超时结果且不先请求取消，线程仍运行时 QThread 析构可致 qFatal 崩溃 |
| AUD-33601 | Suggestion | process | `src/shared/export/video_export_worker.py:296` | medium | `_is_exporting` 仅由 finished/error 信号槽复位，信号丢失时永久卡 True，重复 export() 被静默吞掉且无调用方反馈 |
| AUD-33602 | Suggestion | process | `src/shared/export/video_export_worker.py:303` | medium | VideoExporter.export() 直接写 worker 私有属性，破坏封装且与库内既有约定相悖 |
| AUD-33603 | Suggestion | process | `src/shared/export/video_export_worker.py:66` | high | 死状态：`_config` 只写不读，`last_outcome` 全库无读方且新一轮导出开始时不重置 |
| AUD-33604 | Suggestion | process | `src/shared/export/video_export_worker.py:79` | medium | 关键公开方法缺类型注解，泛型 dict 未参数化 |
| AUD-33605 | Suggestion | process | `src/shared/export/video_export_worker.py:129` | medium | post-export 检查中 `_inspect.signature` 重复求值最多 3 次，run() 成功路径过长 |
| AUD-33700 | Major | perf-opportunity | `src/shared/feature_flags.py:177-183` | medium | is_enabled 对未知 feature id 失败开放（fail-open），与 M0 冻结注释矛盾 |
| AUD-33701 | Major | bug | `src/shared/feature_flags.py:257-263（预设晚于 config 应用）；预设内` | medium | 产品模式预设静默覆盖 config 中已显式开启的 AI 能力开关，违反模块级契约 |
| AUD-33702 | Suggestion | process | `src/shared/feature_flags.py:253-255` | high | config 特性值用 bool() 强转，字符串 "false"/"0" 会被解析为 True |
| AUD-33703 | Major | bug | `src/shared/ffmpeg_locator.py:71-76` | medium | _resolve_in_dir 未做可执行权限检查，不可执行的 bin/ffmpeg 会遮蔽 PATH 上的有效 ffmpeg |
| AUD-33800 | Major | bug | `src/shared/i18n/i18n.py:167-171（主路径）、src/shared/i18n/i18n.py:184-189（回退路径）` | medium | tr() 主路径与回退路径的占位符格式化只捕获 KeyError，ValueError/IndexError 会逃逸到调用方 |
| AUD-33801 | Major | bug | `src/shared/i18n/i18n.py:96-97、123（json.load 直接赋值）；277-282、287-294（` | medium | 加载的翻译 JSON 不校验根类型，非 dict 根在模块合并路径抛未捕获异常 |
| AUD-33802 | Suggestion | risk | `src/shared/i18n/i18n.py:310-317（reload_locale 先换基础词典、再逐个重合并模块）；263-264（` | medium | 类级共享翻译状态无锁，reload_locale 两步更新存在中间态窗口 |
| AUD-33803 | Suggestion | process | `src/shared/i18n/i18n.py:364` | high | 使用已弃用的 locale.getdefaultlocale() |
| AUD-33804 | Suggestion | process | `src/shared/i18n/i18n.py:22` | high | _instance 为从未使用的死属性，误导单例设计意图 |
| AUD-33805 | Suggestion | process | `src/shared/i18n/i18n.py:371-372、392-393` | medium | get_system_locale 两处宽泛 except Exception |
| AUD-33806 | Suggestion | process | `src/shared/i18n/i18n.py:323（` | high | 类型注解不完整/风格不统一 |
| AUD-33901 | Critical | doc-gap | `src/shared/io/file_io.py:477` | medium | 预览缓存 EXR 仍用 ZIP(3) 压缩——与 T-P1-71 已文档化的 libdeflate SIGILL 崩溃修复相矛盾 |
| AUD-33902 | Major | bug | `src/shared/io/file_io.py:866` | medium | `save_image` 宽泛 except 吞掉 `_save_toos_image` 设计性 ValueError，fail-closed 错误文案经公共 API 永不可达 |
| AUD-33903 | Major | bug | `src/shared/io/file_io.py:799-804（同型问题 340-345）` | medium | RAW 单例缓存错误路径留下悬挂状态（已 close 对象 + 旧路径），此后旧路径永久解码失败 |
| AUD-33904 | Major | bug | `src/shared/io/file_io.py:971-974` | medium | `_save_toos_image` 异常路径不保证 `encoder.close()`，绕过编码器"close 覆盖全部退出路径"的资源契约 |
| AUD-33905 | Major | bug | `src/shared/io/file_io.py:865（另见 855、858）` | medium | `save_image` 直接 `cv2.imwrite` 写路径，与全文件"imencode+tofile 处理 Windows Unicode 路径"的既有策略不一致 |
| AUD-33906 | Major | bug | `src/shared/io/file_io.py:857` | medium | TIFF 导出对 float 图像无条件 `np.clip(image, 0, 1)`，>1.0 高光数据被静默裁剪 |
| AUD-33907 | Suggestion | process | `src/shared/io/file_io.py:473-481（对照 305-320）` | medium | 预览缓存命中/未命中返回的 dtype 与值域不一致（uint16 0-65535 vs float32 0-1） |
| AUD-33908 | Suggestion | process | `src/shared/io/file_io.py:365-367` | high | `_read_full_image` 文件句柄未用 with 关闭 |
| AUD-33909 | Suggestion | process | `src/shared/io/file_io.py:442（另见 552）` | high | 裸 `except:` 捕获 BaseException（两处），违背项目"宽泛 except"铁律 |
| AUD-33910 | Suggestion | risk | `src/shared/io/file_io.py:295-296` | medium | `load_image` 建缓存目录为 TOCTOU 竞态写法，无 `exist_ok=True` |
| AUD-33911 | Suggestion | doc-gap | `src/shared/io/file_io.py:59（对照 81-83）` | high | `save_baked_layer` 文档声称"保留 32 位浮点精度"但实际写 HALF(1) 16 位 EXR |
| AUD-33912 | Suggestion | process | `src/shared/io/file_io.py:947-949` | high | `_save_toos_image` 中 a16 `pix_fmt` 分支为不可达死代码 |
| AUD-33913 | Suggestion | process | `src/shared/io/file_io.py:734` | high | `_apply_raw_tone_and_detail` 全仓库无调用（死代码） |
| AUD-33914 | Suggestion | process | `src/shared/io/file_io.py:804、822` | high | `update_raw_decoding` 两处 `except Exception` 静默返回，无日志 |
| AUD-33915 | Suggestion | process | `src/shared/io/file_io.py:288、510、792、831` | medium | 公共方法大量缺失类型注解 |
| AUD-34001 | Major | bug | `src/app/integrations/file_explorer/adapter.py:120-130` | medium | read_port_config 裸 `except:` 吞掉一切异常且不校验 host/port 值，坏配置可致服务器静默启动失败并陷入无限重试 |
| AUD-34002 | Major | risk | `src/app/integrations/file_explorer/adapter.py:276-279（关联 185-188、161）` | medium | stop_server 兜底 terminate() 后立即丢弃 worker 引用，且 stop() 在 server 尚未创建时会空转，可致 QThread 带线程销毁崩溃 |
| AUD-34003 | Suggestion | process | `src/app/integrations/file_explorer/adapter.py:242-248` | medium | is_port_in_use 裸 `except:` 一律返回 False，把探测异常当成"端口空闲" |
| AUD-34004 | Suggestion | process | `src/app/integrations/file_explorer/adapter.py:171-172` | high | FlaskWorker.run 兜底 except 仅记录 str(e)，丢失堆栈，服务器启动失败难排查 |
| AUD-34005 | Suggestion | process | `src/app/integrations/file_explorer/adapter.py:196-206` | medium | 单例约束不完整：__init__ 不登记 _instance，直接构造两次可绕过防护 |
| AUD-34006 | Suggestion | process | `src/app/integrations/file_explorer/adapter.py:30-38` | medium | import 期副作用重：目录树上溯搜索 + 全局 sys.path 注入，环境不满足时直接 RuntimeError |
| AUD-34007 | Suggestion | process | `src/app/integrations/file_explorer/adapter.py:109-117、120-121、133-139、142-148` | medium | 多个公开函数与 FlaskWorker 参数缺少类型注解 |
| AUD-34008 | Suggestion | process | `src/app/integrations/file_explorer/edit_benchmark_service.py:290-291` | medium | start_session 对外部插件传入的 frame_count/warmup_frames 直接 int() 转换，无类型守卫 |
| AUD-34009 | Suggestion | process | `src/app/integrations/file_explorer/edit_benchmark_service.py:292-296` | medium | telemetry.reset() 抛异常会连带丢弃已成功采集的 engine 基线，增量指标静默虚高 |
| AUD-34100 | Major | bug | `src/shared/layout/profile.py:66-76` | medium | from_dict 对外部配置数据仅部分校验，畸形 profile 条目可沿启动路径抛未捕获异常 |
| AUD-34101 | Suggestion | process | `src/shared/layout/profile.py:41, 99, 110, 123` | medium | frozen 不可变契约被 mutable dict 字段破坏：with_* 系列共享引用、实例不可哈希 |
| AUD-34102 | Major | bug | `src/shared/layout/store.py:293-317（对照 store.py:327-330、feature_flags.py:8-24）` | medium | _panel_feature_enabled 将带点的 workspace.*/advanced.* 面板按特性前缀过滤，默认布局的工作区面板被全部剔除 |
| AUD-34103 | Major | bug | `src/shared/layout/store.py:202, 210-211` | medium | migrate_legacy_page_layouts 仅识别扁平 {page: hex}，对应用实际写入的 mode-scoped 结构静默迁移 0 条 |
| AUD-34104 | Suggestion | process | `src/shared/layout/store.py:153-162` | medium | list_profiles 首段循环为死代码（循环体 pass），_get_all_profiles_raw 结果未被使用 |
| AUD-34105 | Suggestion | process | `src/shared/layout/store.py:307-314` | high | feature_map 为恒等映射，属死代码 |
| AUD-34106 | Suggestion | process | `src/shared/layout/store.py:188-239（对照 dock_layout_controller.py:1074、2020-2034）` | medium | 迁移不清理 legacy page_layouts 键且每次启动重跑；用户重置布局后旧布局可被"复活" |
| AUD-34107 | Suggestion | process | `src/shared/layout/store.py:129-141` | medium | delete_profile 返回值只反映缓存是否存在，未反映配置中是否真的删除 |
| AUD-34108 | Suggestion | process | `src/shared/layout/store.py:103-114, 588-590` | medium | 缓存无失效机制：get_profile 永远优先缓存，clear_cache 无任何调用方 |
| AUD-34201 | Major | risk | `src/shared/logging/log_manager.py:84（关联 89-91）` | medium | 裸文件名 log_file_path 使 os.makedirs("") 抛 FileNotFoundError，文件 handler 建立全程无异常防护，可致启动/偏好变更崩溃 |
| AUD-34202 | Suggestion | process | `src/shared/logging/log_manager.py:86-87` | medium | max_log_file_size_mb 未做类型验证，字符串值将触发字符串乘法/TypeError |
| AUD-34203 | Suggestion | risk | `src/shared/logging/log_manager.py:58-68` | medium | root.handlers.clear() 不关闭旧 handler，重复 initialize 泄漏文件句柄，且 clear 与并发 emit 存在竞态窗口 |
| AUD-34204 | Suggestion | process | `src/shared/logging/log_manager.py:79-82、115-118` | high | 默认日志路径解析逻辑在 _add_file_handler 与 get_log_file_path 中重复 |
| AUD-34205 | Suggestion | process | `src/shared/logging/log_manager.py:25、32` | high | config_manager 参数缺少类型注解 |
| AUD-34300 | Major | bug | `src/shared/media/adapter.py:215-228, 372-388（证据交叉：src/shared/media/frame.py:450-466、src/shared/media/topos_source.py:2070-2084）` | medium | 平面格式帧依赖的 `to_rgb()` 对 planar 恒返回 None，planar 后端（topos）素材经适配器取帧恒为 None |
| AUD-34301 | Major | bug | `src/shared/media/adapter.py:467-501` | medium | `total_frames/fps/width/height` 属性静默吞掉一切异常并返回默认值，无任何日志 |
| AUD-34302 | Suggestion | process | `src/shared/media/adapter.py:503-512（对照 447-454 的` | medium | `duration` 属性无异常保护，`release()` 后访问抛 AttributeError，与兄弟属性行为不一致 |
| AUD-34303 | Suggestion | process | `src/shared/media/adapter.py:162-168, 714-718` | medium | `get_frame` 宽泛 except 把领域错误统一折叠为 None；Wrapper 回调 `except Exception: pass` 完全静默 |
| AUD-34304 | Suggestion | perf-opportunity | `src/shared/media/adapter.py:274-311` | high | `_detect_actual_bit_depth` 用整帧 `max()` 推断位深：暗帧误判 + 每帧全量扫描 |
| AUD-34305 | Suggestion | process | `src/shared/media/adapter.py:562-575` | high | `probe_video_info` 异常路径未释放已创建的 adapter，依赖 `__del__` 兜底 |
| AUD-34306 | Suggestion | process | `src/shared/media/adapter.py:98-110, 447-461` | medium | `__init__` 早期失败时 `__del__`→`release()` 访问未绑定的 `_source` |
| AUD-34307 | Suggestion | process | `src/shared/media/adapter.py:722-733` | medium | `VideoDecoderWrapper.VIDEO_EXTENSIONS` 硬编码扩展名表与 Factory 双源维护，且含 `.dng` 静态图片格式 |
| AUD-34308 | Suggestion | process | `src/shared/media/__init__.py:172-173, 382-383, 117` | medium | 私有名进入 `__all__`、`ExtendedPixelFormatInfo` 漏出 `__all__` |
| AUD-34309 | Major | bug | `src/shared/media/asset_proxy_manager.py:242-247` | medium | `_pending_generates` 去重 guard 在 `existing is None` 时形同虚设，重复派发生成 |
| AUD-34310 | Major | bug | `src/shared/media/asset_proxy_manager.py:347-356, 386-399（交叉证据：src/shared/media/disk_proxy_service.py:363-367、635-641）` | medium | 取消/回调被吞的路径使 manifest 永久滞留 GENERATING、pending 永不清理，代理无法再生成 |
| AUD-34311 | Major | bug | `src/shared/media/asset_proxy_manager.py:373-374, 317（交叉证据：src/shared/media/proxy.py:833-857` | medium | `_sync_from_disk_proxy_info` 读取 `DiskProxyInfo` 上不存在的 `width/height`，READY entry 尺寸恒 0x0，`open_proxy_source` 固定按 1920x10 |
| AUD-34312 | Suggestion | process | `src/shared/media/asset_proxy_manager.py:366, 375（对照 213-214）` | medium | `_sync_from_disk_proxy_info` 用 `self._config` 而非 ensure 时生效的 `effective_config` 重建 key/codec |
| AUD-34313 | Suggestion | risk | `src/shared/media/asset_proxy_manager.py:201-211, 231` | medium | manifest 未配置且源文件存在时落入 `assert self._manifest is not None` 崩溃，违背自身注释契约 |
| AUD-34314 | Suggestion | risk | `src/shared/media/asset_proxy_manager.py:384, 392-394, 343-345` | medium | 后台线程 emit 携带可变 entry 引用 + `_mark_failed` 原地改 entry，跨线程接收方可能读到已变更状态 |
| AUD-34315 | Suggestion | process | `src/shared/media/cache.py:182-209` | medium | `put()` 替换已存在帧号时先按"满"淘汰 LRU、后删旧条目，无谓多淘汰一帧 |
| AUD-34316 | Suggestion | process | `src/shared/media/cache.py:106-118, 244-247` | medium | 过期判定基于创建时间，`touch()` 维护的 `last_access` 被忽略，长暂停后热帧被判过期 |
| AUD-34317 | Suggestion | process | `src/shared/media/cache.py:318-338` | medium | `_estimate_size` 对未知类型（含 GPU 零拷贝帧）返回 0，内存上限失真且无 GPU 释放钩子 |
| AUD-34318 | Suggestion | risk | `src/shared/media/cache.py:393-398, 419-434` | medium | `get_missing_frames` 持锁全量扫描大区间；`prefetch_hint` 可产生负帧号、区间参数无校验 |
| AUD-34401 | Major | bug | `src/shared/media/color_decode.py:632、src/shared/media/color_decode.py:677` | medium | limited_to_full/full_to_limited 色度 offset 公式错误，中性色度被映射偏移约 4–9 LSB |
| AUD-34402 | Suggestion | process | `src/shared/media/color_decode.py:484、src/shared/media/color_decode.py:936、src/shared/media/color_decode.py:955` | medium | 未收录色彩矩阵静默回退 BT.709，错色无告警 |
| AUD-34403 | Suggestion | process | `src/shared/media/color_decode.py:809-815` | high | linear_to_hlg 对负输入无钳制，NaN 可穿透 np.clip 进入显示链 |
| AUD-34404 | Suggestion | process | `src/shared/media/color_decode.py:903` | high | tonemap_pq_to_srgb 存在死变量 scale_factor |
| AUD-34405 | Suggestion | process | `src/shared/media/color_decode.py:86-94、src/shared/media/color_decode.py:407、src/shared/media/color_decode.py:427` | medium | resolve 接受的 transfer 集合与 ColorConverter 可解码集合不对称，错误推迟到转换期 |
| AUD-34406 | Suggestion | process | `src/shared/media/color_decode.py:159、src/shared/media/color_decode.py:42` | high | chroma_siting 未做取值范围校验；模块存在未使用导入 Any |
| AUD-34411 | Major | doc-gap | `src/shared/media/decode_policy.py:343-346、src/shared/media/decode_policy.py:41-44、src/shared/media/decode_policy.py:185` | medium | _get_8bit_format 递归降级未实现（pass 占位），预览实际返回 'yuv422p' 而非文档承诺的 'yuv420p' |
| AUD-34412 | Suggestion | process | `src/shared/media/decode_policy.py:285-287` | high | get_output_format 首分支语义颠倒且近乎死代码 |
| AUD-34413 | Suggestion | process | `src/shared/media/decode_policy.py:484-491` | high | get_export_format 中 base_format 计算后未使用（死变量） |
| AUD-34421 | Major | bug | `src/shared/media/color_metadata.py:76-94、src/shared/media/color_metadata.py:144-156、src/shared/media/color_metadata.py:201-215` | medium | 枚举映射表缺 TRAW_LOG0/ST2084 条目，ffmpeg→enum→ffmpeg 往返丢失 transfer 标签 |
| AUD-34422 | Suggestion | doc-gap | `src/shared/media/color_metadata.py:588-591、src/shared/media/color_metadata.py:680、src/shared/media/color_metadata.py:741、src/shared/media/color_metadata.py:792` | medium | normalize_color_* 对未知字符串返回原值而非 'unknown'，违反自身 docstring 契约 |
| AUD-34423 | Suggestion | process | `src/shared/media/color_metadata.py:185-198、src/shared/media/color_metadata.py:129-141、src/shared/media/color_metadata.py:436` | medium | 标准名/反向映射缺 AP1 条目；get_standard_name 参数缺类型注解 |
| AUD-34500 | Major | risk | `src/shared/media/decode_queue.py:297-312（清队）、546-560（解码后入队）、584-596（弹出不回插）` | medium | seek() 与预读线程竞态：清空已解码队列后，正在解码的旧帧会重新入队 |
| AUD-34501 | Major | bug | `src/shared/media/decode_queue.py:451-456、266-284、388-398` | medium | request_frame 返回的请求句柄可能不在队列中，cancel_request 对其无效 |
| AUD-34502 | Suggestion | doc-gap | `src/shared/media/decode_queue.py:339-346（get_frame_entry 在 375-381 重复同一逻辑）` | medium | get_frame 帧号不匹配时将条目放回队尾并返回 None，打乱队列次序且行为未在文档说明 |
| AUD-34503 | Suggestion | process | `src/shared/media/decode_queue.py:571-580、335-336/371-372` | medium | 解码失败以 None 哨兵入队：错误与"暂无帧"不可区分，且可能占满队列 |
| AUD-34504 | Suggestion | process | `src/shared/media/decode_queue.py:477-489` | medium | probe() 失败时 total_frames=0，预读上界不受总帧数约束 |
| AUD-34505 | Suggestion | risk | `src/shared/media/decode_queue.py:561、568、578（无锁写）；408-414（加锁读）` | medium | _stats 计数器在预读线程无锁自增，与 get_stats 的加锁读取不一致 |
| AUD-34506 | Suggestion | process | `src/shared/media/decode_queue.py:455、71-92` | medium | 原位下调既有请求优先级后队列不重排；dataclass(order=True) 首比较字段是 frame_index 而非 priority |
| AUD-34507 | Major | doc-gap | `src/shared/media/disk_proxy_service.py:246-306（尤其 263-267 与 306）` | medium | get_disk_proxy_info 缓存后永不复验，docstring 承诺的 STALE（源文件已修改）检测未实现 |
| AUD-34508 | Major | bug | `src/shared/media/disk_proxy_service.py:276-279` | medium | 磁盘发现路径用硬编码 (3840, 2160) 推算 proxy_resolution，非 4K 源得到错误分辨率 |
| AUD-34509 | Major | risk | `src/shared/media/disk_proxy_service.py:362-368；分发侧 737-744` | medium | GENERATING 分支把 on_progress 挂到全局回调列表且永不移除（泄漏+跨文件串扰），on_complete/on_error 被静默丢弃 |
| AUD-34510 | Suggestion | risk | `src/shared/media/disk_proxy_service.py:354-372` | medium | generate_proxy 的 GENERATING 检查与置位之间存在 TOCTOU 竞态 |
| AUD-34511 | Suggestion | process | `src/shared/media/disk_proxy_service.py:635-646` | medium | _on_generation_complete 的 cancelled/else 分支为死代码，且 else 分支会丢弃用户错误回调 |
| AUD-34512 | Suggestion | process | `src/shared/media/disk_proxy_service.py:762-783` | medium | _update_frame_provider_service 跨模块访问另一服务的私有成员 `_lock/_path_to_clips/_clips` |
| AUD-34513 | Suggestion | process | `src/shared/media/disk_proxy_service.py:134-148` | medium | asset_proxy_to_disk_proxy_info 将 source_mtime/source_size 置 0，且默认格式与 topos-first 默认策略不一致 |
| AUD-34514 | Suggestion | risk | `src/shared/media/disk_proxy_service.py:548-579（562 行起在 with 块外取 self._total_generated 等）、290` | medium | get_proxy_stats 在锁外读取统计字段；_total_bytes_saved 把"发现磁盘上已有代理"累计为节省字节 |
| AUD-34515 | Suggestion | process | `src/shared/media/error_handling.py:279-283` | high | 模板查找为精确类型匹配，任何子类异常都会落入"未知错误"模板 |
| AUD-34516 | Suggestion | process | `src/shared/media/error_handling.py:517-530、537-603` | medium | FallbackStrategy.max_attempts 从未使用，attempt() 不参考 get_available_fallbacks() 的错误相关方案 |
| AUD-34517 | Suggestion | process | `src/shared/media/error_handling.py:620-625` | high | 调用 MediaSourceFactory 的私有方法 `_get_backend_order` |
| AUD-34518 | Suggestion | process | `src/shared/media/error_handling.py:73-75、78-89、93-165` | high | ErrorSeverity 枚举与模板中的裸字符串 severity 互不关联 |
| AUD-34519 | Suggestion | process | `src/shared/media/error_handling.py:637-649、678-679、720-725` | low | 降级尝试中 open() 失败时，已 create 的 MediaSource 未关闭 |
| AUD-34520 | Suggestion | process | `src/shared/media/errors.py:91-96；对照 src/shared/media/error_handling.py:157-164` | medium | PermissionError(MediaError) 遮蔽内建异常名，且与 error_handling 的模板键（内建 PermissionError）永不匹配 |
| AUD-34600 | Major | risk | `src/shared/media/factory.py:757` | medium | probe 完整回退路径异常时已打开的媒体源未关闭（PyAV 容器泄漏） |
| AUD-34601 | Major | bug | `src/shared/media/factory.py:314` | medium | create(auto_open=True) 打开失败后未清理已创建的 source 即切换下一后端 |
| AUD-34602 | Major | bug | `src/shared/media/factory.py:166` | medium | 类级探测/嗅探缓存与失败记录集合无上限、无淘汰策略（长会话内存增长） |
| AUD-34603 | Suggestion | doc-gap | `src/shared/media/factory.py:331` | medium | 无效 backend 名称被静默按 auto 处理，docstring 承诺的 ValueError 契约落空 |
| AUD-34604 | Suggestion | process | `src/shared/media/factory.py:524` | medium | toos_image 后端工厂静默丢弃 target_size 与 preserve_planes |
| AUD-34605 | Suggestion | process | `src/shared/media/factory.py:950` | medium | 硬解 open 失败回退 CPU 后 HardwareDecodeStatus.decode_path 残留硬解路径标识 |
| AUD-34606 | Suggestion | process | `src/shared/media/factory.py:975` | medium | create_with_hwaccel CPU 路径 auto_open 失败抛出原生异常，未包装为 MediaOpenError |
| AUD-34607 | Suggestion | process | `src/shared/media/factory.py:784` | medium | _probe_topos_native 中 ToposMediaSource 构造位于 try 之外，构造异常会逸出 probe API |
| AUD-34608 | Suggestion | process | `src/shared/media/factory.py:803` | medium | _classify_topos_open_failure 在 except 处理链内执行未受保护的绝对 import，可掩盖原始异常 |
| AUD-34609 | Suggestion | process | `src/shared/media/factory.py:1056` | medium | macOS VT 零拷贝路径：open 抛异常时已构造的 vt_source 未 close |
| AUD-34610 | Suggestion | process | `src/shared/media/factory.py:1194` | medium | 工厂直接写 FFmpegHwaccelSource 私有属性 _target_size 传递 target_size |
| AUD-34611 | Suggestion | process | `src/shared/media/factory.py:668` | medium | fps 探测失败时静默默认 24.0，元数据可能失真 |
| AUD-34612 | Suggestion | risk | `src/shared/media/factory.py:156` | medium | 类级可变状态与延迟后端注册无锁，跨线程并发依赖 GIL 原子性 |
| AUD-34700 | Major | bug | `src/shared/media/ffmpeg_source.py:792` | medium | total_frames 估算公式把 time_base 当除数，帧数虚高约 1/tb² 倍 |
| AUD-34701 | Major | bug | `src/shared/media/ffmpeg_source.py:1736（对照 _seek_gop_session:1645-1651 的正确处理）` | medium | _decode_to_target overshoot 静默返回，read_frame 交付错误帧内容且帧号错标 |
| AUD-34702 | Major | bug | `src/shared/media/ffmpeg_source.py:1658（写入后从不更新；另见 1660-1678、1680-1711）` | medium | _main_resync_pts 不随 seek 会话续读更新，会话耗尽/出错时主会话回卷重放已播帧 |
| AUD-34703 | Critical | bug | `src/shared/media/ffmpeg_source.py:2311（回退入口，另见 2336-2366、2576-2579、2664-2672）` | medium | planar proxy 回退到 packed RGB 后仍按平面布局切片，输出只含每行前 1/3 样本的错乱平面 |
| AUD-34704 | Major | bug | `src/shared/media/ffmpeg_source.py:392（声明支持；另见 2547-2550、2692-2699）` | medium | planar 模式对 nv12/p010 半平面格式按纯 planar 提取，色度错乱且缺 V 平面 |
| AUD-34705 | Suggestion | process | `src/shared/media/ffmpeg_source.py:682（对照 2943-2951、2988 的 open 路径）` | medium | probe() 构造的 VideoMetadata 漏传 chroma_siting，与 open 路径元数据不一致 |
| AUD-34706 | Suggestion | process | `src/shared/media/ffmpeg_source.py:1435` | high | _seek_intra seek 后无帧时谎报 current_frame，错误以 End-of-stream 呈现 |
| AUD-34707 | Suggestion | process | `src/shared/media/ffmpeg_source.py:2160（对照 read_frame:1177-1185）` | medium | read_frame_planar/_read_next_frame_planar 缺 P2-1 同步解码断言 |
| AUD-34708 | Suggestion | process | `src/shared/media/ffmpeg_source.py:2746` | medium | alpha 提取辅助函数按原始字节取数且全仓库无调用（死代码） |
| AUD-34709 | Suggestion | risk | `src/shared/media/ffmpeg_source.py:1527` | medium | GOP 索引后台线程跨线程写状态无同步，close() 不 join |
| AUD-34710 | Suggestion | process | `src/shared/media/ffmpeg_source.py:476（对照 1120-1123、3018-3020）` | medium | output_format 校验失败时 __del__→close() 访问未初始化属性抛 AttributeError |
| AUD-34711 | Suggestion | doc-gap | `src/shared/media/ffmpeg_source.py:1540` | high | warm_gop_index 文档承诺失败返回 None，实际异常直接上抛 |
| AUD-34712 | Suggestion | process | `src/shared/media/ffmpeg_source.py:1938（另见 1946-1948）` | medium | 最终兜底 to_ndarray 对 planar 源取 shape[:2] 得到错误宽高 |
| AUD-34713 | Suggestion | process | `src/shared/media/ffmpeg_source.py:2006（另见 748、2991-2995）` | high | 类型注解不精确与残留死 import |
| AUD-34800 | Suggestion | process | `src/shared/media/format_registry.py:43, 82, 192-194` | high | `.dng` 双重注册为视频与图像，`get_media_type` 恒返回 'video' |
| AUD-34801 | Suggestion | process | `src/shared/media/frame.py:404` | high | `has_alpha` 名称启发式对未注册格式误判，且含永不可达的死分支 |
| AUD-34802 | Suggestion | process | `src/shared/media/frame.py:357-360（对照 216-220 的格式定义注释）` | medium | `__post_init__` 从 planes[0] 推导宽高，对每平面 W/2×H/2 的 CFA/TRAW 格式得出半尺寸 |
| AUD-34803 | Suggestion | process | `src/shared/media/frame.py:463-465` | low | `to_rgb` 对 BGR 系返回负 stride 的 flip 视图 |
| AUD-34804 | Suggestion | process | `src/shared/media/frame.py:288, 344-350, 458` | high | 非 frozen dataclass 上使用 `object.__setattr__`，并冗余局部 `import numpy` |
| AUD-34805 | Major | bug | `src/shared/media/frame_cache.py:309-328` | medium | `put()` 更新已存在键时完全绕过内存上限与驱逐检查 |
| AUD-34806 | Suggestion | process | `src/shared/media/frame_cache.py:581-585` | high | `_evict_one` 驱逐原因统计为事后猜测，类目可能记错 |
| AUD-34807 | Suggestion | doc-gap | `src/shared/media/frame_cache.py:529-534, 561-569` | medium | `get_oldest_entries` 文档与实现不符；`_evict_one` 每次驱逐全表扫描 |
| AUD-34808 | Major | bug | `src/shared/media/frame_path.py:183-186（对照 201-206` | medium | `release()` 回调抛异常时状态不落位，重试将重复调用 `release_callback`（双释放风险） |
| AUD-34809 | Suggestion | process | `src/shared/media/frame_path.py:137-150, 197-200` | medium | DECODING 状态无异常恢复出口，异常路径可永久卡死 lease |
| AUD-34810 | Suggestion | process | `src/shared/media/frame_path.py:443-449, 401-407（gpu_frame.py:499` | medium | GBR/浮点平面 RGB 格式被归类为 YUV 渲染路径；`classify_decode_path` 不产出 CVMETAL 变体 |
| AUD-34811 | Suggestion | process | `src/shared/media/gop_index.py:297-299` | high | `build_gop_index` 宽泛 `except Exception` 仅 debug 级日志 |
| AUD-34812 | Suggestion | process | `src/shared/media/gop_index.py:316-324` | medium | `save_gop_index` 的 identity `setdefault` 为死代码；identity 不匹配时仍写永久 miss 的缓存文件 |
| AUD-34813 | Suggestion | risk | `src/shared/media/gpu_frame.py:16, 293-294` | high | `GpuDecodedFrame` 未声明 frozen，违背模块自述的线程安全设计原则 |
| AUD-34814 | Suggestion | risk | `src/shared/media/gpu_frame.py:421-426` | medium | `destructor_policy="enqueue"` 未实现，GC 时静默降级为泄漏告警 |
| AUD-34815 | Suggestion | process | `src/shared/media/gpu_frame.py:49-61, 63-70, 90-93, 112-113` | medium | `normalize_color_matrix` 未知串直通与 range 归一化行为不一致；数值 9 映射 'bt2020' 而非 'bt2020nc'；VALID_* 集合未被强制 |
| AUD-34900 | Critical | bug | `src/shared/media/hwaccel.py:866-878` | medium | 硬解顺序读路径忽略 seek 落地帧（_pending_av_frame）与 seek 会话/主会话回同步，seek 后返回错误帧 |
| AUD-34901 | Major | bug | `src/shared/media/hwaccel.py:779-792（关闭旧容器后重开）、719-730（异常吞掉并宣称回退 CPU）` | medium | hwaccel 重开失败后 CPU 回退失效：旧容器已关闭、_is_open 仍为 True，后续每次读取必抛 RuntimeError |
| AUD-34902 | Major | bug | `src/shared/media/hwaccel.py:884-891` | medium | 解码中途回退 CPU 是假回退：仍在 hwaccel 容器上解码，且丢弃迭代器导致丢帧/帧号漂移，残留 _pending_av_frame 会吐出陈旧帧 |
| AUD-34903 | Major | bug | `src/shared/media/hwaccel.py:483-491` | medium | `_check_vaapi` 对 `os.listdir('/dev/dri')` 无 FileNotFoundError/PermissionError 防护，无 DRM 设备的 Linux 环境上探测直接抛异常 |
| AUD-34904 | Major | bug | `src/shared/media/hwaccel.py:533-546（Linux 分支落空后落到 ffmpeg 检查）、529-531（Windows 分支宽泛 except）` | medium | Linux QSV 探测回退到 ffmpeg 构建项检查（'qsv' in -hwaccels 输出），非 Intel 平台误报 QSV 可用——与 W2-02 已修复的 NVDEC 误判同类 |
| AUD-34905 | Suggestion | process | `src/shared/media/hwaccel.py:369-386` | medium | `_check_videotoolbox` 恒真：PyAV 备用检查是死代码，且 macOS 上无条件 `return True`，探测失去判别力 |
| AUD-34906 | Suggestion | process | `src/shared/media/hwaccel.py:841-863（对照 ffmpeg_source.py:1227-1236）` | high | 覆盖的 `read_next_frame` 缺少父类的 P2-1 播放期同步解码断言 |
| AUD-34907 | Suggestion | process | `src/shared/media/hwaccel.py:920-922（对照 ffmpeg_source.py:1076-1079）` | medium | `_convert_hwaccel_frame` 的 planar 判定绕过 `_is_planar_mode`，缺 `_is_yuv_pixel_format` 门控，与父类路由不一致 |
| AUD-34908 | Suggestion | process | `src/shared/media/hwaccel.py:548-558（对照 47-71 的模块级缓存）` | high | `clear_cache()` 不清除 `_cached_hwaccels_output`，"重新探测"语义不完整 |
| AUD-34909 | Suggestion | risk | `src/shared/media/hwaccel.py:152-155、582-591` | medium | 类级探测缓存无锁，且 `HardwareDecodeConfig.__post_init__` 在构造期触发可能耗时数秒的子进程探测 |
| AUD-34910 | Suggestion | process | `src/shared/media/hwaccel.py:33、39、43、484、661/805/821/830` | high | 未使用导入与死状态：GOP_CODECS/INTRA_CODECS/check_ffmpeg_available/field 未被引用；`_check_vaapi` 内冗余 `import os`；`_hwaccel_context` 只写 |
| AUD-3500 | Major | bug | `src/app/main_window.py:1911-1946（交叉验证 src/app/controllers/file_controller.py:6738-6740）` | medium | closeEvent 在弹出"保存确认"之前就关闭全部后台服务，用户取消关闭后应用处于半瘫痪状态 |
| AUD-35001 | Suggestion | perf-opportunity | `src/shared/media/macos_vt/capability.py:435、362-385` | medium | `get_zero_copy_capability` 持 `_capability_lock` 运行最长 30s 的 probe 子进程，并发查询被串行阻塞 |
| AUD-35002 | Suggestion | process | `src/shared/media/macos_vt/capability.py:472、485` | high | `except (ImportError, Exception)` 冗余且过宽，吞掉领域实现缺陷 |
| AUD-35003 | Suggestion | process | `src/shared/media/macos_vt/capability.py:262-266、617-621` | high | `clear_capability_cache` 与 `invalidate_capability_cache` 完全重复 |
| AUD-35004 | Suggestion | process | `src/shared/media/macos_vt/capability.py:53-54` | high | `supported_codecs: set` / `supported_pixel_formats: set` 裸 set 注解 |
| AUD-35005 | Major | risk | `src/shared/media/macos_vt/cv_metal_bridge.py:401-403、424-430、384-386` | medium | `CVMetalVideoBridge.initialize` 无锁，跨线程首次导入竞态双建 CVMetalTextureCache，泄漏一个 CF 对象 |
| AUD-35006 | Major | risk | `src/shared/media/macos_vt/cv_metal_bridge.py:456、510-513、577-583` | medium | `destroy()` 与并发 `import_pixel_buffer` 竞态：已过检的 `self._cache` 可变为 None 并以 NULL 传入 CoreVideo FFI |
| AUD-35007 | Major | bug | `src/shared/media/macos_vt/cv_metal_bridge.py:288-304、313-325` | medium | `MetalVideoFrameRing` 按 frame_index 值匹配而非条目身份，重复 index 会跨提交提前置完成，导致 GPU 完成前提前释放 |
| AUD-35008 | Suggestion | process | `src/shared/media/macos_vt/cv_metal_bridge.py:610、649、658-660、667` | high | 诊断/转换函数直接访问 `metal_ctx._metal`/`_queue` 私有成员；`"np.ndarray"` 字符串注解无模块级 np 导入 |
| AUD-35009 | Suggestion | process | `src/shared/media/macos_vt/gpu_encode_surface.py:88-89、24-25` | medium | `acquire` 内 `import Quartz` 未纳入受控失败面，非 macOS 路径裸抛 ImportError 而非模块错误类型 |
| AUD-3501 | Major | bug | `src/app/main_window.py:744-750（创建与 start）；src/app/main_window.py:1903-1946（closeEvent 无对应清理）` | medium | UpdateChecker 为 QThread，closeEvent 关闭路径从不停止/等待它 |
| AUD-35010 | Suggestion | risk | `src/shared/media/macos_vt/gpu_export_bridge.py:124-127、111-116` | high | `close()` 的 `_closed` 防重入标志无锁；`samples: Any` 弱类型 |
| AUD-35011 | Suggestion | risk | `src/shared/media/macos_vt/hardware_capability.py:260-277、79-88` | medium | 持久化 verdict 的读-改-写在 `_hw_lock` 之外，并发探测丢失更新；`_machine_fingerprint` 全局写入亦无锁 |
| AUD-35012 | Suggestion | process | `src/shared/media/macos_vt/hardware_capability.py:311-312` | high | 跨模块导入私有函数 `_infer_bit_depth` |
| AUD-3502 | Major | bug | `src/app/main_window.py:1283-1288（创建）；src/app/main_window.py:1704-1705（存在 stop API）；src/app/main_window.py:1903-1946 与 file_controller.py closeEvent（均无停止调用）` | medium | script_engine 在后台 QThread 运行用户脚本，关闭路径未调用其 stop API |
| AUD-3503 | Major | risk | `src/app/main_window.py:1719-1724（信号连接显示其为异步引擎）；src/app/main_window.py:1903-1946（closeEvent 未处理）` | medium | denoise/deblur 处理线程（DenoiseWorker 为 QThread）无关闭停止路径 |
| AUD-3504 | Major | bug | `src/app/main_window.py:1509-1512（交叉验证 src/app/ui/library_panel.py:539-546、src/app/ui/files_panel.py:104,306-314）` | medium | Library ↔ Files 面板 directory_changed 双向互连，Library 侧 set_root_path 无同路径守卫，存在目录重载死循环风险 |
| AUD-3505 | Suggestion | process | `src/app/main_window.py:429` | high | 类型注解引用未导入的名称 TimelinePreviewRenderer |
| AUD-3506 | Suggestion | process | `src/app/main_window.py:504-508、586-590、598-602、615-619、727-731、754-759、798-815（部分）；典型如 615-619` | medium | 启动路径大量宽泛 `except Exception` 静默降级，吞掉配置/特性开关错误 |
| AUD-3507 | Suggestion | risk | `src/app/main_window.py:385-386、525-526` | medium | GPU/引擎初始化失败无降级处理，__init__ 直接崩溃 |
| AUD-3508 | Suggestion | process | `src/app/main_window.py:386、392、1274-1277` | medium | 数值型配置一律用 `or` 兜底，显式 0 值被静默覆盖 |
| AUD-3509 | Suggestion | process | `src/app/main_window.py:1145-1152` | medium | _create_pages_from_registry 中途失败时，已成功创建的页面 widget 成为孤儿 |
| AUD-3510 | Suggestion | process | `src/app/main_window.py:334-335` | high | __init__ 签名注解与 None 默认值不符 |
| AUD-35101 | Major | bug | `src/shared/media/macos_vt/packet_demux.py:249-252` | medium | packets() 的 max_packets 守卫在包已取出后才判断，多消耗并永久丢弃一个压缩包 |
| AUD-35102 | Suggestion | process | `src/shared/media/macos_vt/packet_demux.py:187-190` | medium | 像素格式→位深启发式把 12-bit 与 8-bit 4:2:2 流标记为 10-bit |
| AUD-35103 | Suggestion | doc-gap | `src/shared/media/macos_vt/packet_demux.py:204-217` | medium | _ffmpeg_colorspace_name 把 AVCOL_SPC_BT2020_CL(10) 映射为 'bt2020nc'，与自身 docstring 矛盾 |
| AUD-35104 | Suggestion | process | `src/shared/media/macos_vt/packet_demux.py:155` | high | open() 的 codec_name 回退分支引用刚判定不存在的属性 |
| AUD-3511 | Suggestion | process | `src/app/main_window.py:981、1047、1068-1073` | medium | 快捷键处理中跨对象读取多个私有属性，破坏封装 |
| AUD-35110 | Suggestion | process | `src/shared/media/macos_vt/metal_capability_report.py:120-128` | medium | _atomic_write_text 写失败时残留临时文件，replace 前无 fsync |
| AUD-3512 | Suggestion | process | `src/app/main_window.py:681、693、735、737、741、756、764` | medium | 启动期 singleShot 定时器在窗口关闭后仍会触发，操作半关闭 UI |
| AUD-35120 | Major | risk | `src/shared/media/macos_vt/iosurface_gl_bridge.py:1152-1163（bridge.destroy）、490-505（ring.destroy 的` | medium | IOSurfaceGLBridge.destroy() 无属主线程守卫，跨线程调用会在无 GL 上下文线程上执行 glDeleteTextures 并静默泄漏 |
| AUD-35121 | Suggestion | process | `src/shared/media/macos_vt/iosurface_gl_bridge.py:344（` | medium | get_next_textures 游标取模用初始 _ring_size，与运行期扩容后的 ring 不一致 |
| AUD-3513 | Suggestion | risk | `src/app/main_window.py:764、798-806` | medium | 音频后端诊断在 UI 线程同步执行，可能阻塞启动后的交互 |
| AUD-35130 | Major | bug | `src/shared/media/macos_vt/source.py:648-659（单包迭代与 continue 放弃）、499-501（` | medium | _read_next_decoded_pixel_buffer 单包提交且把"VT 暂无输出"当作 EOF，错误/重排延迟路径提前终止解码并叠加丢包 |
| AUD-35131 | Suggestion | process | `src/shared/media/macos_vt/source.py:968-983（顺序分支无 try）、对照 989-1007（seek 分支 try/except 返回 None）` | medium | read_frame_pixel_buffer 顺序/快路径无异常兜底，与 seek 分支策略不一致 |
| AUD-35132 | Suggestion | process | `src/shared/media/macos_vt/source.py:1084-1086，对照基类 177 行` | high | MacOSVtPlanarSource.open 签名与基类不兼容（LSP 违反） |
| AUD-35133 | Suggestion | process | `src/shared/media/macos_vt/source.py:638（` | medium | 关键解码函数缺返回类型注解；deferred 帧契约靠动态属性传递 |
| AUD-3514 | Suggestion | process | `src/app/main_window.py:817-838` | high | _show_audio_backend_warning 的 caps 参数未使用；告警构建失败被静默吞掉 |
| AUD-35200 | Major | bug | `src/shared/media/macos_vt/topos_surface.py:113` | medium | IOSurface 以 kIOSurfaceIsGlobal=True 创建，向全系统暴露共享表面 |
| AUD-35201 | Major | bug | `src/shared/media/macos_vt/topos_surface.py:98-107,144-151` | medium | 以请求值假定实际行距，未回查 IOSurfaceGetBytesPerRowOfPlane |
| AUD-35202 | Suggestion | process | `src/shared/media/macos_vt/topos_surface.py:167-172` | medium | release_surface 在 mapped 状态只记日志不回收槽位，防御路径下环形槽永久流失 |
| AUD-35203 | Suggestion | process | `src/shared/media/macos_vt/topos_surface.py:192-203` | high | close()/__exit__ 对在租槽位抛 RuntimeError，可能掩盖原始异常 |
| AUD-35204 | Suggestion | risk | `src/shared/media/macos_vt/topos_surface.py:159-165` | medium | unmap_surface 先清 mapped_buffers 再检查解锁状态，失败时状态不一致且 lease 卡死 |
| AUD-35205 | Critical | risk | `src/shared/media/macos_vt/vt_compression.py:185-198` | medium | _native_pointer 以 hash() 兜底伪造原生指针送入 VideoToolbox，可致进程崩溃 |
| AUD-35206 | Major | risk | `src/shared/media/macos_vt/vt_compression.py:521-542,558-567,595-602` | medium | submit/complete 在锁外调用原生 API，与 close() 的 invalidate/清空 session 存在 TOCTOU 竞态 |
| AUD-35207 | Major | risk | `src/shared/media/macos_vt/vt_compression.py:759-775` | medium | Muxer 构造先 av.open/add_stream 后校验 pixel_format，失败路径泄漏容器与输出文件句柄 |
| AUD-35208 | Major | bug | `src/shared/media/macos_vt/vt_compression.py:638-643` | medium | 回调内丢包仅记 last_error，导出静默缺帧且 last_error 无任何消费者 |
| AUD-35209 | Suggestion | risk | `src/shared/media/macos_vt/vt_compression.py:471-481` | medium | _set_profile_level 创建的 CFString 从未 CFRelease，每会话原生堆泄漏 |
| AUD-35210 | Suggestion | process | `src/shared/media/macos_vt/vt_compression.py:336-351` | high | _length_prefixed_to_annex_b 为死代码 |
| AUD-35211 | Suggestion | process | `src/shared/media/macos_vt/vt_compression.py:214-215,221-222,237-238,281-284` | medium | _extract_parameter_sets 静默返回空集，把"能力缺失"推迟成首包处的误导性失败 |
| AUD-35212 | Suggestion | process | `src/shared/media/macos_vt/vt_compression.py:892-901（对照 855-866）` | medium | write_audio_samples 的帧布局回退可能与 add_audio_stream 的流布局不一致（>2 声道） |
| AUD-35301 | Major | risk | `src/shared/media/macos_vt/vt_session.py:1839-1842` | medium | VTDecompressionSession 原生对象销毁时只 Invalidate 从不 CFRelease，每个 session 泄漏一个原生解码器 |
| AUD-35302 | Major | bug | `src/shared/media/macos_vt/vt_session.py:775-779, 706-715, 842-854` | medium | create() 从不重置 `_destroyed`，destroy 后同一实例再 create 返回 True 但会话永久不可用 |
| AUD-35303 | Major | bug | `src/shared/media/macos_vt/vt_session.py:1746-1773, 1726-1731` | medium | 异步 API `submit_packet` 的 `pts` 参数被完全忽略，异步取帧路径丢失时间戳 |
| AUD-35304 | Major | bug | `src/shared/media/macos_vt/vt_session.py:1327-1329, 1195-1202, 1223-1230` | medium | avcC 解析出的 NALU 长度字段宽度被丢弃，格式描述硬编码 4 字节长度前缀 |
| AUD-35305 | Suggestion | process | `src/shared/media/macos_vt/vt_session.py:1679-1688` | medium | 用 `hash(sample_buffer)` 提取 CF 原始指针，依赖 PyObjC `hash==指针` 的实现细节，与同文件 `objc.pyobjc_id()` 用法不一致 |
| AUD-35306 | Suggestion | process | `src/shared/media/macos_vt/vt_session.py:427-469` | medium | `put_pixel_buffer` 在 retain 成功后、入队前的异常路径不释放已 retain 的 buffer |
| AUD-35307 | Suggestion | process | `src/shared/media/macos_vt/vt_session.py:1426-1435` | medium | `_parse_hvcc_extradata` 读取 numNalus 时缺少 `idx+1` 越界检查，靠上层宽泛 except 兜底 |
| AUD-35308 | Suggestion | process | `src/shared/media/macos_vt/vt_session.py:1495-1500` | medium | `_parse_annex_b_extradata` 计算当前 NAL 结束位置用的是当前 start code 长度而非下一个的 |
| AUD-35309 | Suggestion | risk | `src/shared/media/macos_vt/vt_session.py:380-384, 453-454, 463` | medium | `VtDecodeOutputQueue._lock` 创建后从未使用，跨线程计数器无保护 |
| AUD-35310 | Suggestion | process | `src/shared/media/macos_vt/vt_session.py:36-42, 529-605, 694` | medium | 死代码与未使用导入：PyObjC NSObject 回调类从未实例化、`_callback` 属性从未赋值、`time/field/Callable/List/Dict/Any` 未使用 |
| AUD-35311 | Suggestion | process | `src/shared/media/macos_vt/vt_session.py:1832-1856` | medium | destroy() 中 Step 1 等待失败时 Step 4 仍在 try 块外无条件释放 pending refs，保活内存块可能被提前撤除 |
| AUD-35312 | Major | risk | `src/shared/media/macos_vt/vt_zero_copy_probe.py:566, 713-722` | medium | decode-only 阶段解码帧从不调用 release_decoded_frame，每帧泄漏一个 retain 的 CVPixelBuffer/IOSurface |
| AUD-35313 | Major | bug | `src/shared/media/macos_vt/vt_zero_copy_probe.py:498-502` | high | VT session 创建失败的早退路径不清理 bridge 与 standalone GL context |
| AUD-35314 | Major | bug | `src/shared/media/macos_vt/vt_zero_copy_probe.py:812-816` | medium | `--output-json` 传纯文件名（无目录部分）时 `os.makedirs('')` 抛 FileNotFoundError |
| AUD-35315 | Major | bug | `src/shared/media/macos_vt/vt_zero_copy_probe.py:540-554` | medium | 循环内 IOSurface 检测硬编码绝对导入 `src.shared...`，相对导入环境下被静默吞掉导致 probe 误判非零拷贝 |
| AUD-35316 | Suggestion | process | `src/shared/media/macos_vt/vt_zero_copy_probe.py:774-780` | high | 错误清理路径使用两个裸 `except:`，会吞掉 KeyboardInterrupt/SystemExit |
| AUD-35317 | Suggestion | process | `src/shared/media/macos_vt/vt_zero_copy_probe.py:207-223, 271-285` | high | 两份 shader 中 `BT601/BT709/BT2020` mat3 常量为死代码，且 BT2020 数值是 BT.709 的复制品 |
| AUD-35318 | Suggestion | process | `src/shared/media/macos_vt/vt_zero_copy_probe.py:332-351, 354-373, 376-383` | medium | 辅助函数缺少返回值/参数类型注解 |
| AUD-35319 | Suggestion | perf-opportunity | `src/shared/media/macos_vt/vt_zero_copy_probe.py:556-563, 671` | medium | `verify_color` 时每帧都执行 CPU 端 reference 读取（Lock/Unlock + 裸指针读），但结果只在第一帧渲染后使用一次 |
| AUD-35320 | Suggestion | process | `src/shared/media/macos_vt/vt_zero_copy_probe.py:649-661` | medium | rectangle 分支未检查 `len(gpu_planes) > 1` 即访问 `gpu_planes[1]` |
| AUD-35400 | Major | bug | `src/shared/media/metadata.py:202-207` | medium | Rational.from_string 不校验外部字符串：多余分段被静默丢弃、畸形输入抛裸 ValueError |
| AUD-35401 | Major | risk | `src/shared/media/metadata.py:279-290, 300-304, 317-326` | medium | VideoMetadata 未验证 fps/SAR 为正，duration_seconds 与 display_size 存在除零崩溃路径 |
| AUD-35402 | Suggestion | process | `src/shared/media/metadata.py:146-155` | medium | Rational 算术不完备：缺 `__rmul__`/`__rtruediv__`，float 乘法用 int() 截断 |
| AUD-35403 | Suggestion | process | `src/shared/media/metadata.py:277` | high | audio_tracks 使用裸 tuple 注解，丢失元素结构类型 |
| AUD-35404 | Major | risk | `src/shared/media/performance_monitor.py:443-454（及 464/481/497/509/526/537/551/567/577/590/604/614/624/634 各 append 点），对比 849-852 唯一有上限的列表` | medium | 会话内统计列表无界增长：长播放会话内存持续膨胀且每秒聚合排序在 RLock 内执行 |
| AUD-35405 | Major | bug | `src/shared/media/performance_monitor.py:1416-1439（clear）、1441-1476（reset），对照 start_session 308-364；早退点 728-729` | medium | clear()/reset() 清理不完整：清空后聚合统计返回陈旧数据，降级级别与早退返回值不一致 |
| AUD-35406 | Suggestion | process | `src/shared/media/performance_monitor.py:725, 744-746` | high | check_tick_performance_and_degrade 存在未使用变量 current_time，注释阈值与实现不符 |
| AUD-35407 | Suggestion | doc-gap | `src/shared/media/performance_monitor.py:899-915` | medium | check_proxy_ladder 含永不可达的 elif 分支与死变量 frame_budget_ms，docstring 判定口径与实现不符 |
| AUD-35408 | Suggestion | process | `src/shared/media/performance_monitor.py:681-684` | high | track_zero_copy_frame 中 `fallback_reason or "non_zero_copy_path"` 恒取右值，or 表达式冗余 |
| AUD-35409 | Suggestion | risk | `src/shared/media/performance_monitor.py:1059-1062, 1069-1072（另 1081-1082/1089-1090/1094-1095/1100-1112 同模式）` | medium | get_aggregate_stats 对同一数据重复执行 list 拷贝与排序（decode/upload 各 4 次），且每秒在锁内运行 |
| AUD-35410 | Suggestion | process | `src/shared/media/performance_monitor.py:963-975，对照 387-389` | high | record_playback_stats/get_playback_stats 为全库无调用方的死 API，且与 track_decode_time 双重累计 _total_frames |
| AUD-35411 | Suggestion | process | `src/shared/media/performance_monitor.py:1601, 1624, 1651, 1674, 1698, 1721；1506` | high | 六个计时上下文管理器缺返回类型注解；_calculate_stats 返回注解与 'count' 实际类型不符 |
| AUD-35500 | Major | bug | `src/shared/media/pixel_format.py:67-72` | medium | Subsampling.NONE 与 YUV_444 值相同，成为枚举别名，4:4:4 语义与 RGB/GRAY 混淆 |
| AUD-35501 | Suggestion | process | `src/shared/media/pixel_format.py:680-698` | high | 注册表缺口：yuva444p10le 等带 Alpha 的专业格式未注册，消费方被迫用名称启发式兜底 |
| AUD-35502 | Suggestion | doc-gap | `src/shared/media/pixel_format.py:64 / :265 / :677,690-698` | high | 多处注释/文档与实现不一致（YUV_410 垂直子采样、chroma_bits 算术注释、ProRes 4444 Alpha 表述） |
| AUD-35503 | Suggestion | process | `src/shared/media/probe.py:191-199` | medium | check_codec_supported 别名表把解码器名（libdav1d）当作编码支持依据 |
| AUD-35504 | Major | bug | `src/shared/media/proxy.py:715-729` | medium | generate_proxy_key 未纳入 topos_bit_depth/topos_keep_444/topos_qp，Topos 代理参数变更后旧缓存不失效 |
| AUD-35505 | Major | bug | `src/shared/media/proxy.py:186-194（HALF/QUARTER）、230-232（AUTO）、503-505（ProxyResolution.calculate_size）` | medium | 最小分辨率钳制按轴独立作用，破坏宽高比并会上采样小尺寸源（内存代理与磁盘代理两处同模式） |
| AUD-35506 | Major | bug | `src/shared/media/proxy.py:590-593` | medium | calculate_target_size 奇数高度未取偶，H264/JPEG 代理编码失败风险 |
| AUD-35507 | Suggestion | process | `src/shared/media/proxy.py:413-416` | high | 文件中部重复导入（enum/typing 顶部已导入，中部再导入一次） |
| AUD-35508 | Suggestion | process | `src/shared/media/proxy.py:116-120` | high | ProxyConfig.scale_factor 对 CUSTOM/AUTO 恒返回 1.0 的占位实现 |
| AUD-35509 | Suggestion | process | `src/shared/media/proxy.py:538-540` | high | DiskProxyConfig 可变、参数名 format 遮蔽内置函数，与模块内 frozen dataclass 风格不一致 |
| AUD-35600 | Major | bug | `src/shared/media/proxy_generator.py:1643` | medium | 成功路径末尾对源文件重复探测两次，且 fps=0 或两次探测结果不一致时把成功误报为失败 |
| AUD-35601 | Major | perf-opportunity | `src/shared/media/proxy_generator.py:1538-1548` | medium | CLI 路径进度监控按 `\n` 分行——ffmpeg 统计行以 `\r` 结尾，编码期间 readline 阻塞，帧数解析与取消轮询全部停摆 |
| AUD-35602 | Major | risk | `src/shared/media/proxy_generator.py:1549-1552` | medium | Windows 上 CLI 路径 stderr 永不排水——管道缓冲写满后 ffmpeg 阻塞，监控循环死锁 |
| AUD-35603 | Major | bug | `src/shared/media/proxy_generator.py:973、988（配合 1017-1018 的 reformat 未传 color_range）` | medium | `_run_pyav_to_topos` 硬编码 limited range：全量程源被错标 limited，代理预览对比度/颜色错误 |
| AUD-35604 | Suggestion | process | `src/shared/media/proxy_generator.py:775、829-842` | medium | `_run_topos` 对 total_frames=0 的退化源产出 0 帧空代理并返回成功 |
| AUD-35605 | Suggestion | risk | `src/shared/media/proxy_generator.py:200-201（对照 proxy_manager.py:591-594 与 316-318）` | high | 取消标志竞态：generate() 入口重置 `_cancelled`，可吞掉启动瞬间发出的取消 |
| AUD-35606 | Suggestion | process | `src/shared/media/proxy_generator.py:1180-1184` | high | `_probe_source_with_pyav` 宽泛 except 把"无视频流"等领域错误吞成 None，根因丢失 |
| AUD-35607 | Major | risk | `src/shared/media/proxy_manager.py:180-181、254-261` | medium | `max_concurrent` 从未被实施——每任务即时起独立线程，并发编码数无上限 |
| AUD-35608 | Major | risk | `src/shared/media/proxy_manager.py:612-635（` | medium | on_complete/on_error 在持有 `self._lock` 时调用——与 DiskProxyService 形成锁序倒置死锁风险 |
| AUD-35609 | Major | bug | `src/shared/media/proxy_manager.py:280-284（对照 proxy_generator.py:386-395 与 1494-1500）` | medium | `pause_all_generation` 对主编码路径无效——PyAV/topos 进程内编码不设 `self._process`，pause()/resume() 恒返回 False |
| AUD-35610 | Major | bug | `src/shared/media/proxy_manager.py:229-235` | medium | 同源重复入队时静默丢弃后来调用方的全部回调 |
| AUD-35611 | Suggestion | risk | `src/shared/media/proxy_manager.py:251、634-635` | high | 完成任务永不清理：`_tasks` 字典无界增长 |
| AUD-35612 | Suggestion | risk | `src/shared/media/proxy_manager.py:391-393` | high | `get_valid_proxy_path` 在锁外读取 `_proxy_cache`，校验与取值之间存在失效窗口 |
| AUD-35613 | Suggestion | risk | `src/shared/media/proxy_manager.py:361-374、443-449、484-494` | high | 持管理器锁执行磁盘 IO：is_proxy_valid/get_stats/cleanup_* 在 `self._lock` 内 stat/remove 文件 |
| AUD-35614 | Suggestion | process | `src/shared/media/proxy_manager.py:132-138、430-452` | high | `ProxyStats.total_assets`/`cache_hit_rate` 恒为 0——统计字段未实现 |
| AUD-35615 | Suggestion | process | `src/shared/media/proxy_manager.py:522-547、549-572` | high | `asset: Any` 弱类型并直接假设 `.path/.asset_id/.with_proxy_path` 接口 |
| AUD-35616 | Major | bug | `src/shared/media/proxy_manifest.py:261-266、268-286（对照 204-210 的 load 重建）` | medium | `_source_index` 每源仅一条索引：多策略/版本条目被挤出，invalidate/remove 漏处理孤儿条目，重载后陈旧条目可重新可见 |
| AUD-35617 | Suggestion | risk | `src/shared/media/proxy_manifest.py:243-250、261-265` | medium | get_entry/upsert_entry 直接暴露内部可变 entry，绕过"所有查询/修改加锁"的封装承诺 |
| AUD-35618 | Suggestion | risk | `src/shared/media/proxy_manifest.py:234-237` | high | `ensure_loaded` 检查-加载竞态：`_loaded` 在锁外读取 |
| AUD-35619 | Suggestion | process | `src/shared/media/proxy_manifest.py:149、157` | high | `from_dict` 对 `proxy_path`/`error` 未做类型规整——外部 JSON 数据验证缺口 |
| AUD-35620 | Suggestion | process | `src/shared/media/proxy_manifest.py:218-232` | high | `save()` 吞掉全部异常仅 warning，持久化失败对调用方不可见 |
| AUD-35621 | Suggestion | process | `src/shared/media/proxy_manifest.py:292-304` | high | `entry_count`/`stats()` 不走 `ensure_loaded`：加载前查询得到空统计 |
| AUD-35701 | Major | bug | `src/shared/media/seek.py:342-352` | medium | estimate_seek_cost 对反向小距离跳转系统性低估 GOP seek 成本，且分支注释与条件矛盾 |
| AUD-35702 | Suggestion | process | `src/shared/media/seek.py:127-128、163-164` | medium | frame_to_pts / pts_to_frame 用浮点除法做整数 PTS 换算，存在 off-by-one 风险 |
| AUD-35703 | Suggestion | process | `src/shared/media/seek.py:233-243` | medium | find_keyframe_before 的 GOP 分支把 target PTS 本身当作 keyframe 返回，语义错误且无任何调用方 |
| AUD-35704 | Suggestion | doc-gap | `src/shared/media/seek.py:110-114` | high | frame_to_pts docstring 示例输出错误 |
| AUD-35711 | Major | bug | `src/shared/media/thumbnail_provider.py:719-727` | medium | RAW 位图缩略图分支缺少 RGB→BGR 转换，缩略图红蓝通道互换 |
| AUD-35712 | Major | bug | `src/shared/media/thumbnail_provider.py:603、628、640、724` | medium | 多处裸 `except:` 违反「不得宽泛 except」铁律，把任意异常当作"目录不可写"改写缓存路径 |
| AUD-35713 | Major | bug | `src/shared/media/thumbnail_provider.py:613-641` | medium | _get_cache_path 每次请求对缓存目录做 O(变体数×文件数) 全列扫描并逐文件 NFC 规范化 |
| AUD-35714 | Suggestion | risk | `src/shared/media/thumbnail_provider.py:27-30` | high | _cleanup_file_lock 定义后从未调用，per-file 锁字典 _cv_locks 只增不减 |
| AUD-35715 | Suggestion | process | `src/shared/media/thumbnail_provider.py:732-738` | medium | EXR/HDR 缩略图对 scene-linear 数据直接 ×255 clip，无显示编码，预览近乎全黑 |
| AUD-35716 | Suggestion | process | `src/shared/media/thumbnail_provider.py:1015-1035、1122-1124` | medium | 解码失败的缩略图无负缓存，损坏文件每次刷新都重新入队解码 |
| AUD-35717 | Suggestion | risk | `src/shared/media/thumbnail_provider.py:143-154` | medium | _get_bridge 跨线程无锁重建存在竞态，重建后会脱离 provider 已连接的 bridge |
| AUD-35718 | Suggestion | risk | `src/shared/media/thumbnail_provider.py:888-892、468-478` | high | ThumbnailProvider.instance() / ClipThumbnailCache 单例无锁，且构造函数创建 QTimer/QThreadPool 要求主线程 |
| AUD-35719 | Suggestion | process | `src/shared/media/thumbnail_provider.py:40/52、567-571、76-77、86-97、1178、1511、1936` | medium | 类型注解缺失与死代码：Optional[callable] 非法类型、QMetaObject/Q_ARG 死导入、ThumbnailImageResult 死 DTO、callback 参数从未使用 |
| AUD-35800 | Major | bug | `src/shared/media/timecode.py:471-482` | medium | `__eq__` 忽略 tc_type 而 `__hash__` 包含 tc_type，违反哈希/相等契约 |
| AUD-35801 | Suggestion | process | `src/shared/media/timecode.py:114-123` | medium | DF 类型未与帧率交叉验证，非丢帧帧率可构造 DF 时间码并输出 ';' 分隔符 |
| AUD-35802 | Suggestion | process | `src/shared/media/timecode.py:219-231 与 247` | high | NDF 帧号超过 24 小时抛 ValueError，而 DF 路径静默按天取模回绕，行为不对称 |
| AUD-35803 | Suggestion | process | `src/shared/media/timecode.py:251` | high | `from_frame_number` DF 路径中 `remaining` 赋值后从未使用（死代码） |
| AUD-35804 | Major | bug | `src/shared/media/timeline.py:732-735` | high | `Timeline.with_track_updated` 对不存在的 track_id 静默无操作，更新被丢弃 |
| AUD-35805 | Major | risk | `src/shared/media/timeline.py:89-92、377-378` | medium | NaN 的 speed/opacity 可通过 `__post_init__` 验证，后续帧映射/渲染再崩溃 |
| AUD-35806 | Major | bug | `src/shared/media/timeline.py:212-215（另见 173、135、244、279 的同型` | medium | 速度帧映射的浮点除法截断在 0.3 等步长上产生 off-by-one |
| AUD-35807 | Suggestion | process | `src/shared/media/timeline.py:243-245` | high | `with_speed`/`with_trim` 在高速短素材上生成零长剪辑，报错信息误导 |
| AUD-35808 | Suggestion | process | `src/shared/media/topos_frame_extract.py:41、87` | high | `_image_profile_for_movie` / `_detect_interframe` 的 `summary` 参数无类型注解 |
| AUD-35809 | Major | bug | `src/shared/media/topos_image_sequence.py:498-503（` | medium | hold 模式"文件消失保留前驱帧"分支不可达——`_nearest_before(slot.frame)` 恒返回 slot.frame 自身 |
| AUD-35810 | Suggestion | risk | `src/shared/media/topos_image_sequence.py:505-508` | high | `_read_slot` 在无锁状态下回写共享 `self._by_frame`（预取线程与前台线程并发） |
| AUD-35811 | Suggestion | perf-opportunity | `src/shared/media/topos_image_sequence.py:177-183` | high | `PlaneCache.put` 的字节记账未防护 None 平面，而同函数冻结循环防护了——防御不一致 |
| AUD-35812 | Suggestion | process | `src/shared/media/topos_image_sequence.py:212-213` | high | `ToposImageSequenceSource.__init__` 的 `codec` 参数无类型注解 |
| AUD-35813 | Major | process | `src/shared/media/topos_image_source.py:134-152（校验）、64-73（命名）` | medium | 预留/未知 pixel_format（≥4）叠加 Alpha 时绕过 open 校验并被误标为 YUV 422 命名 |
| AUD-35814 | Suggestion | process | `src/shared/media/topos_image_source.py:201-205、212-213` | medium | `close()` 后 `read_frame`/`probe` 会静默自动重开，与 `is_open` 属性语义矛盾 |
| AUD-35900 | Critical | risk | `src/shared/media/topos_source.py:1309（调用点）与 :1996（替换点）` | medium | 池化路径 alpha 移位原地替换 `entry.arrays[3]`，原始 alpha 平面被释放而 `entry.views` 仍持其裸地址——下一帧复用池条目时原生解码写入已释放内存（use-after-free 堆损坏） |
| AUD-35901 | Suggestion | process | `src/shared/media/topos_source.py:343-346` | medium | `_pixel_format_name` alpha 分支不校验未知 (pf, bit_depth, alpha_bit_depth) 组合，与无 alpha 分支的 W1 显式失败姿态不对称，且会生成注册表中不存在的格式名 |
| AUD-35902 | Suggestion | process | `src/shared/media/topos_source.py:1216-1222` | medium | `read_frame_metal` 最外层 `except Exception` 把方法内任何编程错误也吞成 info 级"unavailable"+None，回归缺陷会静默降级为 CPU 路径 |
| AUD-35903 | Suggestion | process | `src/shared/media/topos_source.py:760-763` | medium | decode worker 预算租约以 `id(self)` 作键，键冲突时 `ValueError` 从 open() 裸抛（未包装为 MediaOpenError） |
| AUD-35904 | Suggestion | perf-opportunity | `src/shared/media/topos_source.py:939-940` | high | V9/GOP 读路径每帧全新分配全分辨率平面（无池化），且交付可写平面，违反池/旧路径的 read-only 交付契约 |
| AUD-35905 | Suggestion | process | `src/shared/media/topos_source.py:1136-1140` | high | `read_frame_metal` 直接读取 `ToposIOSurfacePool._slots[0].row_bytes` 私有成员，且取不到时静默传 stride 0 |
| AUD-35906 | Suggestion | process | `src/shared/media/topos_source.py:1367-1368` | low | `read_audio_samples` 未验证 `ai.channel_count >= 1`，channel_count=0 时 `frame_bytes=0` 导致 ZeroDivisionError |
| AUD-35907 | Suggestion | process | `src/shared/media/topos_source.py:1330-1334（另 1357` | medium | 音频读路径的原生调用异常未包装为 `MediaDecodeError`，与视频路径错误语义不一致 |
| AUD-35908 | Suggestion | process | `src/shared/media/topos_source.py:1865-1866` | high | `_audio_codec_display_name` 用魔数 `== 1` 判定 float32，未用已导入的 `TC_AUDIO_FMT_FLOAT32` 常量 |
| AUD-35909 | Suggestion | process | `src/shared/media/topos_source.py:749、795、799、804-807、1755、1465` | high | 多处跨对象私有属性访问：`self._codec._has_decoder_ctx`/`_has_decoder_batch` 等、`self._plane_pool._max` |
| AUD-35910 | Major | bug | `src/shared/media/topos_surface_capability.py:139-156（证据点 :155）` | medium | darwin 分支仅凭"平台判断 + PyObjC 导入成功"即返回 `zero_copy=True, evidence="probe"`，违反模块自身"零拷贝判定必须由持有已映射资源的进程内探测背书"的契约 |
| AUD-35911 | Suggestion | process | `src/shared/media/topos_surface_capability.py:124-137` | medium | `TOPOS_SURFACE_BACKEND` 未知取值（拼写错误等）被静默当作 "auto" 落入平台分支，无任何告警 |
| AUD-3600 | Major | bug | `src/app/menu/menu_builder.py:96-103` | medium | 顶层菜单未注册时整组 action 仅以 DEBUG 级静默跳过，实际已导致 5 个 View/Timeline 菜单项从未出现 |
| AUD-36000 | Major | bug | `src/shared/media/windows_hw/d3d11_device.py:59-61, 68-71` | medium | VTABLE_SLOT_GET_IMMEDIATE_CONTEXT=27 指向 CreateDeferredContext；GetImmediateContext 实际槽位为 40，注释声称的「真机同源验证」并未覆盖此槽 |
| AUD-36001 | Major | risk | `src/shared/media/windows_hw/d3d11_export_capability.py:121-124（创建点；函数内 8 个 return 均未触碰 device）` | medium | probe_d3d11_export_capability 自动创建的 D3D11Device 在所有返回路径均未 release，每次探测泄漏一份 device+immediate context 的 COM 引用 |
| AUD-36002 | Major | bug | `src/shared/media/windows_hw/d3d11_decoder.py:84-86（异常源）与 224-228（捕获缺口）` | medium | from_native 对 Mapping 直接下标取必需键，缺键抛 KeyError，decode_next 的 except (TypeError, ValueError) 捕不住，异常逃逸到调用方 |
| AUD-36003 | Major | bug | `src/shared/media/windows_hw/d3d11_decoder.py:269-274（及 open() 首行 163 的 self.close()）` | medium | close() 对 close_decoder 无异常保护且状态清理不在 finally，异常后残留半开状态并污染 open() 复用路径 |
| AUD-36004 | Suggestion | process | `src/shared/media/windows_hw/d3d11_decoder.py:249` | high | dxgi_format 用魔数 100/65 硬编码，绕开 d3d11_device 已导出的格式常量 |
| AUD-36005 | Suggestion | process | `src/shared/media/windows_hw/capability.py:40-41` | medium | supported_codecs/supported_pixel_formats 为硬编码声明而非探测结果，且与解码器支持集不一致（缺 av1） |
| AUD-36006 | Suggestion | doc-gap | `src/shared/media/windows_hw/capability.py:5-8, 47, 121, 165` | medium | 文档声明 TOPOS_VIDEO_ZERO_COPY=auto\|off\|force，但 force 既未实现也未拒绝，未知值静默按 auto 处理 |
| AUD-36007 | Suggestion | process | `src/shared/media/windows_hw/capability.py:84-85` | high | _detect_wgl_nv_dx_interop 宽泛 except 静默返回 False 且无任何日志 |
| AUD-36008 | Suggestion | process | `src/shared/media/windows_hw/d3d11_format_contract.py:42-44, 71-73` | medium | color_matrix/color_transfer/color_primaries 为自由字符串，未校验已知词表；拼错的 HDR transfer 会静默落入 SDR bt709 路径 |
| AUD-36009 | Suggestion | process | `src/shared/media/windows_hw/d3d11_frame.py:68-69` | medium | __post_init__ 校验高为偶数但未校验宽为偶数，奇数宽度的 4:2:0 帧可通过 Python 契约层 |
| AUD-3601 | Major | bug | `src/app/menu/menu_builder.py:163` | medium | 动态创建的中间子菜单标题未经过 i18n 翻译，中文界面显示英文 "Timeline" |
| AUD-3602 | Suggestion | process | `src/app/menu/menu_builder.py:115-126` | medium | clear() 清理不彻底：中间子菜单、`_menus` 缓存与 ShortcutManager 内的 QAction 引用均未处理 |
| AUD-3603 | Suggestion | doc-gap | `src/app/menu/menu_builder.py:30, 41` | high | I18N 导入未使用，且类 docstring 声称 "translated label" 与实现不符 |
| AUD-3604 | Suggestion | process | `src/app/menu/menu_builder.py:52, 57` | high | 构造参数 menu_bar 被存储但从未使用 |
| AUD-3605 | Suggestion | process | `src/app/menu/menu_builder.py:55` | high | 构造参数 shortcut_manager 缺少类型注解 |
| AUD-3606 | Suggestion | process | `src/app/menu/menu_builder.py:145-147` | high | `_resolve_menu` 中 `if not parts` 为不可达死代码 |
| AUD-3607 | Suggestion | process | `src/app/menu/menu_builder.py:256-259` | medium | `_derive_default_shortcut` 硬编码单条默认快捷键映射，与模块侧注册存在漂移风险 |
| AUD-36100 | Major↓ | risk | `src/shared/media/windows_hw/d3d11_pipeline.py:104-120（及 169）` | medium | done-callback 异常逃逸会泄漏信号量槽且 outcome 永不完成，run() 无超时等待导致导出线程永久死锁 |
| AUD-36101 | Major | risk | `src/shared/media/windows_hw/d3d11_pipeline.py:117-120` | medium | render 结果"target 非空但带 reason"时 target 的 lease 不释放，渲染目标池槽位泄漏 |
| AUD-36102 | Major | bug | `src/shared/media/windows_hw/d3d11_pipeline.py:84-85, 91-94` | high | 遥测缺陷：render_waits 恒为 0，encode_waits 实际统计的是 surface 池超时 |
| AUD-36103 | Suggestion | risk | `src/shared/media/windows_hw/d3d11_pipeline.py:88-102` | medium | `submit()` 的 closed 检查与 executor 提交之间存在 TOCTOU 竞态 |
| AUD-36104 | Major | risk | `src/shared/media/windows_hw/d3d11_passthrough_chain.py:175-180` | medium | `release_after` 异常路径泄漏共享 lease（decode 池槽位永久损失） |
| AUD-36105 | Major | risk | `src/shared/media/windows_hw/d3d11_passthrough_chain.py:126-149` | medium | `record_stage` 未防御 trail 重复登记 ValueError：同一 trail 二次 run() 即抛出，且已读 frame 泄漏 |
| AUD-36106 | Suggestion | process | `src/shared/media/windows_hw/d3d11_passthrough_chain.py:126-136` | high | `record_stage` 参数 `gpu_ms: object` 弱类型且 `float(gpu_ms)` 无类型守卫 |
| AUD-36107 | Major | risk | `src/shared/media/windows_hw/d3d11_render_graph.py:273-293, 301-314` | medium | `D3D11TimelineRenderer` 状态无锁：release() 与 render_frame() 并发时 AttributeError 逃出声明的异常契约 |
| AUD-36108 | Suggestion | process | `src/shared/media/windows_hw/d3d11_render_graph.py:127-134, 198-204` | medium | `D3D11RenderTarget` 校验不全；transition 词表与 shader 注册表双重维护 |
| AUD-36109 | Major | risk | `src/shared/media/windows_hw/d3d11_texture_pool.py:126-137（External 变体同构 178-189）` | medium | fence 回收线程在 `wait` 返回 False 或抛出 OSError 时静默丢失池槽；fence 永不 signal 时阻塞线程无限堆积 |
| AUD-36110 | Suggestion | process | `src/shared/media/windows_hw/d3d11_texture_pool.py:133-137, 185-189` | high | 每次 fence 归还新建一个 Thread，违背"逐帧零分配"的池设计精神 |
| AUD-36111 | Suggestion | risk | `src/shared/media/windows_hw/d3d11_texture_pool.py:361-369` | high | `_return_texture` 在持有 `self._condition` 时调用外部 releaser，与 `close()` 的锁外释放策略不一致 |
| AUD-36112 | Suggestion | process | `src/shared/media/windows_hw/d3d11_texture_pool.py:100-108, 191-196, 227-237` | high | `D3D11TextureLease` 与 `D3D11ExternalTextureLease` 防御不一致；`_PoolState` 字段缺类型参数 |
| AUD-36113 | Suggestion | process | `src/shared/media/windows_hw/d3d11_texture_pool.py:451-467` | medium | `create_d3d11_texture_pool` 只捕 RuntimeError/ValueError，注入工厂抛 OSError 时违反 `(None, reason)` 契约 |
| AUD-36114 | Major | risk | `src/shared/media/windows_hw/d3d11_gl_bridge.py:76-94, 110-114` | medium | `destroy()` 清空 `_active_slots` 与 in-flight `bind_texture` 竞态，可致同一 ring 槽双重占用 |
| AUD-36115 | Suggestion | risk | `src/shared/media/windows_hw/d3d11_gl_bridge.py:62-65, 75, 117-126` | high | 诊断状态锁纪律松散：`_last_fallback_reason` 锁外写、`fail_count`/`_initialized` 无锁读；`frame_index` 未验证 |
| AUD-36116 | Suggestion | process | `src/shared/media/windows_hw/gpu_capability.py:270-275` | high | `check_adapter_match` 把两个空 LUID 判为匹配（"" == ""） |
| AUD-36117 | Suggestion | process | `src/shared/media/windows_hw/gpu_capability.py:187-197` | medium | `_probe_driver_version_nvml` 宽 `except Exception` 且 NVML 返回码全部未检查 |
| AUD-36118 | Suggestion | process | `src/shared/media/windows_hw/gpu_capability.py:213-234` | high | 适配器枚举中途异常时仍报 `probe_ok=True`，与 `factory_failed` 的语义不一致 |
| AUD-36119 | Suggestion | process | `src/shared/media/windows_hw/gpu_capability.py:80, 84, 63, 224, 235` | high | 类型注解缺失与 logger 即时 f-string 格式化 |
| AUD-36200 | Critical | perf-opportunity | `src/shared/media/windows_hw/hw_planar_source.py:268-272` | medium | 每帧硬编码 bt709/limited 色彩标签并写入动态属性，屏蔽真实流色彩元数据，非 BT.709/full-range/HDR10 素材渲染颜色错误 |
| AUD-36201 | Major | risk | `src/shared/media/windows_hw/hw_planar_source.py:102-104,143-149,159` | medium | open() 失败路径不清理已打开的 hwaccel 容器，`_is_open` 状态失真，消费方捕获异常后泄漏容器句柄 |
| AUD-36202 | Suggestion | process | `src/shared/media/windows_hw/hw_planar_source.py:135-139` | high | 重开容器时用裸 `except Exception: pass` 吞掉 close 错误 |
| AUD-36203 | Major | bug | `src/shared/media/windows_hw/windows_zero_copy_probe.py:82,91-94（根因 src/shared/media/windows_hw/d3d11_device.py:51）` | medium | "格式支持层/纹理池层"实际检测与创建的是 A8_UNORM(65) 而非 NV12，分层证据失真且可能误放行 |
| AUD-36204 | Suggestion | process | `src/shared/media/windows_hw/windows_zero_copy_probe.py:70,87,96,120,134,142` | medium | 分层 reason 以 "xxx_layer:" 作前缀，破坏 reasons.py "前缀即词表原子 reason" 的归类约定 |
| AUD-36205 | Suggestion | process | `src/shared/media/windows_hw/windows_zero_copy_probe.py:19` | high | 未使用的导入 `asdict` |
| AUD-36206 | Major | bug | `src/shared/media/windows_hw/gpu_encode_surface.py:94,155,259-267（根因 src/shared/media/windows_hw/d3d11_device.py:51-52）` | medium | DXGI 格式码沿用包内错误常量（NV12=65/P010=100），将错值传过原生桥 ABI 边界 |
| AUD-36207 | Suggestion | process | `src/shared/media/windows_hw/gpu_encode_surface.py:155,209,213` | high | P010 判定用魔法数字面量 100，未用已导入的命名常量 DXGI_FORMAT_P010 |
| AUD-36208 | Major | doc-gap | `src/shared/media/windows_hw/native_bridge.py:188-194` | medium | `load_windows_native_bridge` 对 `factory()`/`capabilities()` 的异常不做防护，异常逃逸并可使 probe 崩溃，与自身 docstring 承诺矛盾 |
| AUD-36209 | Suggestion | process | `src/shared/media/windows_hw/vendor_policy.py:59,64,69,77,93,104` | medium | 多个失败 reason 为词表外裸字符串，违反 reasons.py 宣称的"禁止裸字符串散落"硬规则 |
| AUD-36301 | Suggestion | process | `src/shared/node_canvas/graph_ui_protocol.py:154-185` | medium | `GraphUiSignals` 以 `@dataclass(frozen=True)` 实现常量命名空间，`ALL` 被注解后意外成为实例字段 |
| AUD-36302 | Major | bug | `src/shared/node_canvas/undo_stack.py:97-119` | medium | `undo_fn` / `redo_fn` 抛错被宽泛 `except Exception` 吞掉后动作仍在双栈间转移，栈状态与模型状态可能失配 |
| AUD-36303 | Suggestion | process | `src/shared/node_canvas/undo_stack.py:91-95` | high | 撤销栈无深度上限，也无 move 类动作合并（coalesce）机制 |
| AUD-36304 | Suggestion | process | `src/shared/node_canvas/undo_stack.py:51-53` | high | `EditAction` 的 `undo_fn` / `redo_fn` 默认为无操作闭包，漏传时静默无错误 |
| AUD-36401 | Suggestion | doc-gap | `src/shared/node_help/__init__.py:85` | high | 语言读取失败时直接放弃，未按文档声明的解析顺序回退 en_US |
| AUD-36402 | Suggestion | process | `src/shared/node_help/__init__.py:57` | high | `_current_app_locale` 使用宽泛 `except Exception`，可能掩盖 I18N 模块的真实缺陷 |
| AUD-36403 | Suggestion | doc-gap | `src/shared/node_help/__init__.py:120` | high | `has_node_help` 为返回布尔值读取并解码整个文档文件 |
| AUD-36404 | Suggestion | process | `src/shared/node_help/__init__.py:81` | low | `node_type` 未做路径安全校验即拼入文件路径 |
| AUD-36405 | Suggestion | process | `src/shared/node_help/dialog.py:61` | high | `__init__` 的 `parent` 参数缺少类型注解 |
| AUD-36500 | Suggestion | process | `src/shared/panels/registry.py:86` | medium | register() 不校验 category/work_mode 枚举值，未知值导致面板被静默排除出 Window > Panels 菜单 |
| AUD-36501 | Suggestion | process | `src/shared/panels/registry.py:25-26` | medium | 未使用的导入 field/FrozenSet；typing 注解风格混用 |
| AUD-36502 | Suggestion | process | `src/shared/panels/registry.py:175` | medium | validate_dock_integrity 的 window 参数缺类型注解；返回的 issues 混合错误与信息性条目 |
| AUD-36601 | Major | bug | `src/shared/paths/paths.py:24（直接影响 39、88、110 行的 lut/models/cache 拼接）` | medium | get_project_root 回溯层级少一层，返回 <root>/src 而非项目根，luts/models/cache 全部解析到源码树内 |
| AUD-36602 | Major | bug | `src/shared/paths/paths.py:42-45、69-72、94-97、113-116（get_lut_dir / get_hdri_dir / get_models_dir / get_cache_dir 各一处，代码完全相同）` | high | 四处 `except Exception: pass` 吞掉目录创建错误，违反“不得宽泛 except 掩盖领域错误”铁律 |
| AUD-36603 | Suggestion | process | `src/shared/paths/__init__.py:1-2` | high | __all__ 未导出 get_hdri_dir，包表面不完整 |
| AUD-36604 | Suggestion | process | `src/shared/paths/paths.py:21-22` | low | frozen 模式以 dirname(sys.executable) 为根，PyInstaller onedir/.app 布局下资源目录未必在此处 |
| AUD-36700 | Major | bug | `src/shared/preferences/__init__.py:15（本包全部导入区，无该符号）；消费方 src/features/composite/render/disk_cache_store.py:143、:465` | medium | 包未导出消费方导入的 `get_config_manager`，磁盘缓存偏好设置全部静默失效 |
| AUD-36701 | Suggestion | process | `src/shared/preferences/__init__.py:120-121` | medium | `register()` 对重复 page_id 静默丢弃，无日志无告警 |
| AUD-36702 | Suggestion | process | `src/shared/preferences/__init__.py:92-99；消费方 src/app/ui/preferences_dialog.py:270-273` | medium | descriptor 允许 `factory` 与 `page_object` 同时为 None，故障推迟到 UI 构建期才暴露 |
| AUD-36703 | Suggestion | process | `src/shared/preferences/__init__.py:55、92、95、97、102、115` | medium | 类型注解缺失或弱化，`page_object` 丢失协议约束 |
| AUD-36800 | Suggestion | process | `src/shared/project/color_stack.py:308-310` | medium | from_dict 未对外部数值字段做类型校验/归一 |
| AUD-36801 | Suggestion | process | `src/shared/project/color_stack.py:59-105, 196-198` | medium | frozen dataclass 的 dict 字段经 replace/from_dict 后为共享可变引用 |
| AUD-36802 | Suggestion | process | `src/shared/project/color_stack.py:60-73` | medium | 调色参数默认值混用 int 字面量，违反"浮点字面量显式 .0"铁律 |
| AUD-36803 | Suggestion | process | `src/shared/project/layer_adapter.py:20, 29` | high | `_get_layer_class` 与 `layer_from_dict` 类型注解不完整 |
| AUD-36804 | Suggestion | process | `src/shared/project/layer_adapter.py:23-25` | medium | 懒加载 import_module 使 shared→features 运行时依赖推迟到首次反序列化才暴露 |
| AUD-36805 | Major | bug | `src/shared/project/library.py:407-408（对照 373-379, 445）` | medium | read_project_meta 的 float() 转换位于受保护 try 之外，单个畸形档案使整库扫描失败 |
| AUD-36806 | Major | process | `src/shared/project/library.py:152` | medium | get_project_backup_dir 的 mkdir 无 OSError 防护，扫描/列表/重命名路径直接抛异常 |
| AUD-36807 | Major | bug | `src/shared/project/library.py:522-525, 579-587` | medium | rename_project 失败回滚丢失"部分搬移"状态，伴随文件滞留新名称下 |
| AUD-36808 | Suggestion | process | `src/shared/project/library.py:498-501, 605-610` | medium | duplicate_project 在归档缺 project_id 键时副本与原件共享 ID，破坏"独立副本"契约 |
| AUD-36809 | Suggestion | risk | `src/shared/project/library.py:323-339` | medium | restore_project_backup 仅捕获 OSError，非 OSError 失败泄漏自身 mkstemp 临时文件 |
| AUD-36810 | Suggestion | process | `src/shared/project/library.py:616-625` | high | _move_to_trash 缺 send2trash 时静默退化为永久删除 |
| AUD-36811 | Suggestion | process | `src/shared/project/library.py:86-89（同型：106-109, 116-119, 143-147, 347-354）` | medium | config 访问处多处宽泛 `except Exception` 掩盖配置层错误 |
| AUD-36812 | Suggestion | process | `src/shared/project/manifest.py:133-135, 146-147` | medium | from_dict 静默接受高于当前版本的 schema_version，且版本/时间戳字段无类型校验 |
| AUD-36813 | Suggestion | process | `src/shared/project/manifest.py:202-240` | medium | with_modified/with_active_timeline 手工逐字段重建，存在新增字段漏带风险 |
| AUD-36814 | Major | risk | `src/shared/project/offline_asset_manager.py:601-619` | medium | update_offline_status 重建 ToposProject 时遗漏 revision 字段，乐观锁计数静默归零 |
| AUD-36815 | Major | bug | `src/shared/project/offline_asset_manager.py:313-316, 588-589（对照 216-219）` | medium | get_offline_asset_list / update_offline_status 缺 is_virtual_asset_path 防护，topos:// 虚拟素材被误判离线 |
| AUD-36816 | Major | bug | `src/shared/project/offline_asset_manager.py:248-249（对照 353-354, 401-402）` | high | check_offline_assets 对未知工程类型静默返回空报告，与 relink/batch_relink 的显式报错不一致 |
| AUD-36817 | Suggestion | risk | `src/shared/project/offline_asset_manager.py:557-563（对照 495-533）` | medium | auto_search_for_missing 同步递归扫描家目录大路径，UI 线程调用会长时间阻塞 |
| AUD-36818 | Suggestion | process | `src/shared/project/offline_asset_manager.py:346, 582, 215, 233` | high | 未使用的导入与循环变量（MediaAssetRef、asset_id） |
| AUD-36901 | Major | bug | `src/shared/project/payload_cache.py:99` | medium | 保存时 assets/ 大二进制全量读入内存并整包重写，内存峰值与逐次全量重压缩随工程规模线性放大 |
| AUD-36902 | Major | bug | `src/shared/project/payload_cache.py:148-156` | medium | load_from_file 把"文件存在但读取失败"与"文件不存在"同化为空 payload，后续保存会静默丢弃其他产品的模块数据 |
| AUD-36903 | Suggestion | process | `src/shared/project/payload_cache.py:104-116, 169-181` | medium | manifest.json/project.json 解析失败时 write_to_zip 产出重复同名 zip 条目 |
| AUD-36904 | Suggestion | process | `src/shared/project/payload_cache.py:106-108, 121-123, 129-134` | medium | 解析失败分支对同一 zip 条目重复 zf.read；未知 .json 解析结果未校验顶层类型为 dict |
| AUD-36910 | Major | bug | `src/shared/project/project_manager.py:446-463, 588-597` | medium | 写路径 save_project/save_project_to_path 非原子直接覆写 .topos，宽泛 except 吞掉全部失败且无成败返回值（潜伏数据丢失模式；全仓库当前无调用方） |
| AUD-36911 | Major | risk | `src/shared/project/project_manager.py:608-623, 348, 389-401` | medium | schedule_autosave 用 threading.Timer 在后台线程遍历 engine.layers 并改写 plugin dict，无任何锁（潜伏数据竞争） |
| AUD-36912 | Suggestion | process | `src/shared/project/project_manager.py:306-463, 465-597` | high | save_project 与 save_project_to_path 约 130 行近乎完全复制 |
| AUD-36913 | Suggestion | process | `src/shared/project/project_manager.py:270, 599, 625` | high | 多处函数签名缺类型注解，新旧注解风格混用 |
| AUD-36914 | Suggestion | process | `src/shared/project/project_manager.py:156-157, 165-166, 177-178` | medium | 烘焙缓存命中判断仅比较文件大小，同名同大小不同内容会误判为已最新 |
| AUD-36915 | Suggestion | process | `src/shared/project/project_manager.py:273-297` | low | _save_preview_cache 以 os.getcwd() 为缓存根目录，且缩略图数据格式约定不明 |
| AUD-36920 | Critical | bug | `src/shared/project/save_router.py:916-930` | medium | Composite 保存用显式字段列表重建 EditProject，漏传 smart_bins/render_cache_manifests/render_cache_settings/proxy_policy/revision，一次保存即 |
| AUD-36921 | Major | bug | `src/shared/project/save_router.py:751-756, 790-822, 852` | medium | 自动新建调色栈后未把 stack_id 同步回会话/clip（对比 composite 路径的 on_composite_id_created），无 stack_id 的保存路径反复保存会堆叠孤儿栈 |
| AUD-36922 | Suggestion | risk | `src/shared/project/save_router.py:1167-1186` | medium | backup_project 存在恒假死条件与重复检查，且在调用线程同步执行完整 save()（UI 阻塞风险） |
| AUD-36923 | Suggestion | process | `src/shared/project/save_router.py:1136-1151` | medium | _backup_in_progress 检查-置位非原子，且未在 __init__ 声明 |
| AUD-36924 | Suggestion | process | `src/shared/project/save_router.py:944-946` | high | _save_composite_to_edit_project 异常分支未记录堆栈，排障信息弱于同类路径 |
| AUD-37001 | Major | bug | `src/shared/project/schema_v1_spec.py:868-875` | medium | validate_json 在 jsonschema 缺失时静默放行（fail-open），外部数据验证被无声绕过 |
| AUD-37002 | Suggestion | process | `src/shared/project/schema_v1_spec.py:128, 215-218, 310-313` | medium | schema v1 枚举与 v2/v3 数据模型漂移，spec 无法通过校验接纳现行数据 |
| AUD-37003 | Major | risk | `src/shared/project/topos_project.py:666-686（with_name；688-842 的 with_asset_added/with_color_stack_added/with_timeline_added/with_composition_added/with_active_timeline/with_ui_state 同样缺失）` | medium | 全部 7 个 with_* 工厂方法丢失 revision 字段，任何一次派生都会把乐观锁 revision 静默重置为 0 |
| AUD-37004 | Major | risk | `src/shared/project/topos_project.py:393, 432-441（对照模块 docstring :8-10 "不可变数据类（frozen=True）确保线程安全"）` | medium | frozen 数据类持有可变 dict 字段且 ui_state/settings 跨实例共享引用，"不可变确保线程安全"的设计承诺不成立 |
| AUD-37005 | Major | bug | `src/shared/project/topos_project.py:379-380` | high | DeliverPresetRef.from_dict 对长度不足的 resolution 抛 IndexError，单个坏预设字段导致整个工程加载失败 |
| AUD-37006 | Suggestion | process | `src/shared/project/topos_project.py:690-703` | medium | with_asset_added 的 root bin 解析取"第一个无父 bin"，且重复添加同 asset_id 仍递增 asset_count 导致计数漂移 |
| AUD-37007 | Suggestion | process | `src/shared/project/topos_project.py:496-521` | high | ToposProject.from_dict 对字段类型不合规的外部数据缺乏校验，异常类型不可控 |
| AUD-37008 | Major | bug | `src/shared/project/topos_project_controller.py:584-590（对照 start_auto_save :468-475、set_auto_save_interval :489-499）` | medium | from_dict 毫秒→分钟整除截断可产生 0 间隔自动保存定时器，且间隔参数全程无下界校验 |
| AUD-37009 | Major | risk | `src/shared/project/topos_project_controller.py:146-148（docstring 声明）对照 :243/:254/:291/:327（直接` | medium | "线程安全，所有状态变更通过信号"的契约与实现不符：方法体直接原地改 dict，无任何锁 |
| AUD-3701 | Major | bug | `src/app/module_loader.py:70-82` | medium | post_register_hook 抛异常时模块已注册成功，`_register_spec` 却返回 (False, error)，状态与报告矛盾 |
| AUD-37010 | Suggestion | risk | `src/shared/project/topos_project_controller.py:521-537` | medium | _on_auto_save_timer 先发射 auto_save_triggered 再无条件同步 save()：潜在双重保存，且保存回调在 UI 线程定时器内同步执行 |
| AUD-37011 | Suggestion | process | `src/shared/project/topos_project_controller.py:427-430` | high | save() 以宽泛 except Exception 吞掉领域错误且仅记录消息字符串（无堆栈） |
| AUD-37012 | Suggestion | doc-gap | `src/shared/project/topos_project_controller.py:432-457` | high | save_page 成功分支为空操作死代码，且文档语义（清除该页 dirty）与实现（save() 清除全部页面 dirty）不符 |
| AUD-37013 | Suggestion | process | `src/shared/project/topos_project_controller.py:23, 25（:232/:277/:315/:393 重复` | high | 未使用的导入（Optional、field）与方法内重复局部 import time |
| AUD-3702 | Suggestion | process | `src/app/module_loader.py:163-166` | high | `register_preset_modules` 丢弃 `_register_spec`/`register_color_module` 的失败结果，产品入口无 R2-8 失败提示 |
| AUD-3703 | Suggestion | process | `src/app/module_loader.py:85-97, 169-181` | medium | `create_registry` 与 `discover_and_register_modules` 为全库无调用的死代码，与"唯一注册实现"的收敛意图相悖 |
| AUD-3704 | Suggestion | process | `src/app/module_loader.py:108-111` | high | `register_color_module` 忽略 `register_module` 的布尔返回值，注册未生效时仍返回 True 并打 "registered" 日志 |
| AUD-3705 | Suggestion | process | `src/app/module_loader.py:54, 120, 134-138, 155` | medium | 类型注解缺失（flags/preset/_import_feature_class 返回值），且跨模块读 `registry._context` 私有字段 |
| AUD-3706 | Suggestion | process | `src/app/page_contract.py:2, 20, 25-27` | medium | `factory: Callable[[Any], Any]` 弱类型，context 与页面 widget 均失去静态检查 |
| AUD-37101 | Major | bug | `src/shared/project/topos_project_io.py:380-391` | medium | manifest 合并方向错误：每次保存重置 created_at/created_by/required_features/optional_features |
| AUD-37102 | Major | risk | `src/shared/project/topos_project_io.py:585-595（另见 :369、:514-526）` | medium | load() 缓存目录替换非原子且有竞态；save() 读-改-写在并发下丢失更新 |
| AUD-37103 | Major | bug | `src/shared/project/topos_project_io.py:368-369` | medium | 既有 .topos 不可读时静默按空 payload 覆盖保存，跨产品模块数据丢失 |
| AUD-37104 | Major | bug | `src/shared/project/topos_project_io.py:290-291` | medium | 解压无总大小/条目数上限，恶意或损坏的 .topos 可耗尽磁盘（zip bomb） |
| AUD-37105 | Suggestion | process | `src/shared/project/topos_project_io.py:534-538、808-812` | high | save()/load() 顶层宽泛 except Exception 吞掉领域错误，traceback 走 stderr 而非 logger |
| AUD-37106 | Suggestion | process | `src/shared/project/topos_project_io.py:1020-1026（另见 :993-1000、调用点 :400-404）` | medium | _relativize_*_paths 原地修改调用方传入的 dict，隐式副作用契约 |
| AUD-37107 | Suggestion | process | `src/shared/project/topos_project_io.py:930-954（配置定义 :129-131）` | medium | _write_json 为死代码；serialization_format/compress_project_file 配置在 ZIP 管线中无效 |
| AUD-37108 | Suggestion | process | `src/shared/project/topos_project_io.py:1104-1108（路径入口 :487-503）` | medium | 缓存资产复制路径为不可达死路，且启用后存在整体保存失败与陈旧资产永不清理两个缺陷 |
| AUD-37109 | Suggestion | process | `src/shared/project/topos_project_io.py:420-426` | medium | composite/viewer_state.json 仅在传入 compositions 数据时刷新，否则保持陈旧 |
| AUD-37110 | Suggestion | process | `src/shared/project/topos_project_io.py:498-503（另见 :369）` | medium | 保存全程整包驻留内存，大工程内存峰值约为包体量级 |
| AUD-37111 | Suggestion | process | `src/shared/project/topos_project_io.py:788-797` | medium | teaching_animation 模块缺失回退报告中 cache_preserved 检查的是全局 assets/ 目录 |
| AUD-37112 | Major | bug | `src/shared/project/topos_project_read_service.py:150-156（佐证 project_manager.py:94-97）` | medium | 回退路径将不可信 ZIP 路由进未加固的 extractall |
| AUD-37113 | Suggestion | process | `src/shared/project/topos_project_read_service.py:131-148、86` | medium | "无调色/合成数据的合法新格式工程"被等同于"新格式解析失败"，触发多余的旧格式二次解压 |
| AUD-37114 | Suggestion | process | `src/shared/project/topos_project_read_service.py:193-214` | medium | 恒取字典序第一个 color stack，忽略 primary_color_stack_id/激活状态 |
| AUD-37115 | Suggestion | process | `src/shared/project/topos_project_read_service.py:146-148` | high | _load_from_topos_io 捕获全部异常仅记 debug 级日志 |
| AUD-37116 | Suggestion | process | `src/shared/project/topos_project_read_service.py:209-212` | medium | active_layer_index / raw_params 从外部 JSON 直接透传，无类型与范围验证 |
| AUD-37117 | Suggestion | process | `src/shared/project/topos_project_read_service.py:35-36` | high | ToposProjectBundle 弱类型：project: Any、layers: list 无元素类型 |
| AUD-37200 | Suggestion | process | `src/shared/render_diagnostics.py:45-47（定义）、src/shared/render_diagnostics.py:171-172（未校验的写入）` | medium | `_VALID_SOURCES` 定义后从未使用，`record()` 对 `source`/`severity` 不做任何枚举校验 |
| AUD-37201 | Suggestion | process | `src/shared/render_diagnostics.py:163-167（subjects 处理）、src/shared/render_diagnostics.py:173-178（其他字段均已规整）` | medium | `subjects` 只做 str 化去重，未走 `_normalize_context_identifier`，与其余 ID 字段的防路径规整不对称 |
| AUD-37202 | Suggestion | process | `src/shared/render_diagnostics.py:168` | medium | `message` 未强制 `str()` 转换，非字符串真值会破坏 `message: str` 类型契约或直接抛 TypeError |
| AUD-37203 | Suggestion | process | `src/shared/render_diagnostics.py:200-202（clear 无调用方）、src/shared/render_diagnostics.py:215-226（全局单例）` | medium | 全局诊断单例从不 `clear()`，跨工程/会话的陈旧编译失败仍会返回给 AI 工具 |
| AUD-37300 | Major | bug | `src/shared/tracking/coordinate_map.py:126` | medium | ClipFrameMapper.timeline_to_source_frame 边界闭区间与 TimelineClip 语义不一致，可返回越界 source frame |
| AUD-37301 | Suggestion | process | `src/shared/tracking/coordinate_map.py:28` | medium | 归一化→像素方向的两个函数不做尺寸正数校验，与反向函数不对称 |
| AUD-37302 | Major | bug | `src/shared/tracking/corner_pin.py:311` | medium | build_corner_pin_export 缺少矩阵有限性防护，单个坏帧的 ValueError 会让整次导出失败 |
| AUD-37303 | Suggestion | process | `src/shared/tracking/corner_pin.py:158` | high | export_corner_pin_json 不创建父目录，与 write_corner_pin_export 行为不一致 |
| AUD-37304 | Suggestion | process | `src/shared/tracking/corner_pin.py:258` | medium | validate_corner_pin 在参考四角退化时报出误导性原因"透视过度" |
| AUD-37305 | Suggestion | process | `src/shared/tracking/corner_pin.py:72` | high | 公共导出函数的 binding 参数缺类型注解 |
| AUD-37306 | Major | bug | `src/shared/tracking/feature_detector.py:121` | medium | 分桶均匀化后的全局截断按桶序而非响应序，会整桶丢弃点、破坏"均匀分布"目标 |
| AUD-37307 | Suggestion | doc-gap | `src/shared/tracking/feature_detector.py:91` | medium | mask 参数文档语义（替代 region）与实现（仍夹回 region 矩形）矛盾 |
| AUD-37308 | Suggestion | process | `src/shared/tracking/feature_detector.py:69` | medium | mask 仅校验尺寸与非空，未校验 dtype/通道数 |
| AUD-37309 | Suggestion | process | `src/shared/tracking/frame_sampler.py:183` | medium | scale 只按宽度计算，代理尺寸各边独立取偶导致 y 向存在系统性映射误差 |
| AUD-37310 | Suggestion | process | `src/shared/tracking/homography_stability.py:160` | medium | summarize_diagnostics 参数类型为 Dict[int, object]，靠 getattr 鸭子取值且重复求值 |
| AUD-37311 | Suggestion | process | `src/shared/tracking/merge.py:202` | medium | merge_into_binding 无条件置 has_forward/has_backward=True，与实际方向无关 |
| AUD-37312 | Major | risk | `src/shared/tracking/point_manager.py:133` | medium | redetect_reference_points 重新从 0 编号 feature_id，锁定/排除集合仍指向旧 ID，语义错位 |
| AUD-37400 | Suggestion | process | `src/shared/tracking/shape_mask.py:67-69, 82-87, 106, 110-116` | medium | base_geometry 字段缺长度/类型校验，畸形持久化数据会让跟踪任务 IndexError |
| AUD-37401 | Suggestion | process | `src/shared/tracking/track_data.py:255` | medium | from_dict 枚举值不容错，未知 mode 会让整个工程加载失败 |
| AUD-37402 | Suggestion | process | `src/shared/tracking/track_data.py:74-75` | high | locked/excluded 集合类型注解未参数化 |
| AUD-37403 | Major | bug | `src/shared/tracking/tracker_2d.py:865-870, 883-889, 572` | medium | 参考模板为低纹理（近常量）时外观门控静默失效，且诊断恒报满分相似度 |
| AUD-37404 | Suggestion | perf-opportunity | `src/shared/tracking/tracker_2d.py:518-527（对比 535-544 等其余调用点均传` | medium | 有效点不足失败路径漏传 ref_template，模板重捕获前每帧重复重建参考模板 |
| AUD-37405 | Suggestion | process | `src/shared/tracking/tracker_2d.py:282-284（及 414-415 同样表述）` | medium | anchor_H 方向注释与实际矩阵语义相反，误导维护 |
| AUD-37406 | Suggestion | process | `src/shared/tracking/tracker_2d.py:521, 537, 555, 576, 606, 639` | high | fb_weak 阈值 0.5 为散落的魔法数字，违反 TrackerConfig 自身声明 |
| AUD-37407 | Suggestion | process | `src/shared/tracking/tracker_2d.py:962-978` | medium | RANSAC 未播种，同输入的分析结果不保证可复现 |
| AUD-37408 | Suggestion | process | `src/shared/tracking/tracker_2d.py:1071-1079, 1022-1031` | medium | 预测区域完全出画时失败原因被误判为 BLUR 而非 OUT_OF_BOUNDS |
| AUD-37409 | Suggestion | process | `src/shared/tracking/tracker_2d.py:1142（` | high | 内部数据结构与参数存在弱类型注解 |
| AUD-37410 | Major | bug | `src/shared/tracking/tracking_cache.py:48-59, 63-75` | medium | 缓存键未包含 shape_geometry，遮罩形状变化后返回陈旧跟踪结果 |
| AUD-37411 | Suggestion | risk | `src/shared/tracking/tracking_cache.py:115-129` | medium | compute_media_fingerprint 在调用线程做容器探测，无超时保护 |
| AUD-37412 | Suggestion | process | `src/shared/tracking/types.py:193-202` | medium | from_dict 必填键与枚举值不容错，损坏/新版本数据反序列化直接抛异常 |
| AUD-37501 | Suggestion | process | `src/shared/ui/__init__.py:1-2` | high | 缺少 __all__，与兄弟包导出风格不一致 |
| AUD-37600 | Suggestion | process | `src/shared/ui/audio_mixer/__init__.py:2-17` | high | 包级 __all__ 未导出 MeterMode，公共 API 类型不可从包导入 |
| AUD-37601 | Major | bug | `src/shared/ui/audio_mixer/audio_fader.py:647-662` | medium | `_value_from_position` 在滑轨长度为 0（widget 高/宽 == knob 高/宽）时除零，鼠标按下/拖动路径可触发异常终止 |
| AUD-37602 | Major | bug | `src/shared/ui/audio_mixer/audio_fader.py:209-213, 337-356, 309-335` | medium | 无值变化的点击释放与逐格滚轮均触发提交通道，叠加 controller 无去重导致 undo 栈被空/碎片命令污染 |
| AUD-37603 | Suggestion | process | `src/shared/ui/audio_mixer/audio_fader.py:268-283` | medium | sliderChange 钳制分支为不可达死代码，且一旦触发会用 blockSignals 吞掉 valueChanged |
| AUD-37604 | Suggestion | process | `src/shared/ui/audio_mixer/audio_fader.py:98-108` | medium | `set_tick_positions([])` 不清理旧的 `_max_label_width` 与固定宽度 |
| AUD-37605 | Suggestion | process | `src/shared/ui/audio_mixer/audio_fader.py:144, 159, 195, 209, 215, 230, 236, 249, 285, 290, 337` | high | 事件处理器参数普遍缺类型注解 |
| AUD-37611 | Major | bug | `src/shared/ui/audio_mixer/audio_level_meter.py:346-349` | high | `set_meter_mode` 用宽泛 `except Exception: pass` 吞掉信号发射中的任意异常 |
| AUD-37612 | Suggestion | perf-opportunity | `src/shared/ui/audio_mixer/audio_level_meter.py:599-606, 948-957` | medium | 渐变填充在 compact 与横向分支每帧新建 QLinearGradient+QColor，绕过 A8 缓存优化 |
| AUD-37613 | Suggestion | process | `src/shared/ui/audio_mixer/audio_level_meter.py:319-324` | high | `set_level_style(GRADIENT)` 强制关闭块线，切回 SOLID 不恢复用户偏好，菜单勾选态与渲染脱节 |
| AUD-37614 | Suggestion | process | `src/shared/ui/audio_mixer/audio_level_meter.py:150-159, 575, 1164-1167` | medium | `channels` 构造参数未验证，0/负值在横向布局与 compact 绘制路径引发除零 |
| AUD-37615 | Suggestion | process | `src/shared/ui/audio_mixer/audio_level_meter.py:772, 793` | high | 垂直与水平刻度标签符号不一致（abs vs 带负号） |
| AUD-37616 | Suggestion | process | `src/shared/ui/audio_mixer/audio_level_meter.py:472-473, 489-493, 497-515` | high | 事件处理器参数缺类型注解 |
| AUD-37621 | Major | perf-opportunity | `src/shared/ui/audio_mixer/audio_mixer_panel.py:432-446` | medium | 每帧 LUFS 后端探测：热路径内逐轨 import + 宽泛 except，失败时默认“后端可用”→ 显示伪造 LUFS 读数 |
| AUD-37622 | Major | bug | `src/shared/ui/audio_mixer/audio_mixer_panel.py:353-364` | medium | additive（Shift）solo 将轨道移出 `_solo_muted` 记账却不解除其联动静音，轨道保持“solo 且静音”且永远脱离恢复名单 |
| AUD-37623 | Major | bug | `src/shared/ui/audio_mixer/audio_mixer_panel.py:178-209（对照 set_narrow_layout 507-527）` | medium | `register_track` 不应用当前 `_narrow_layout`，窄布局下新增 strip 以宽模式注册 |
| AUD-37624 | Major | bug | `src/shared/ui/audio_mixer/audio_mixer_panel.py:599-602（记账仅在 set_track_solo 327-381 维护）` | medium | `sync_tracks` 静默回填 solo/mute 不维护 `_solo_tracks`/`_solo_muted` 记账，undo/外部恢复 solo 后面板状态机脱节 |
| AUD-37625 | Major | bug | `src/shared/ui/audio_mixer/audio_mixer_panel.py:590-600（联动静音来源 set_track_solo 373-377）` | medium | solo 激活期间 sync_tracks 用模型 is_muted=False 复位被联动静音的 strip，显示状态与实际听感相反 |
| AUD-37626 | Major | bug | `src/shared/ui/audio_mixer/audio_mixer_panel.py:460-464（布局结构 84-130）` | medium | `collapse_mixers` 对嵌套布局中的 widget 调 `setAlignment`，按 Qt 语义为无效 no-op，折叠居中不生效 |
| AUD-37627 | Suggestion | process | `src/shared/ui/audio_mixer/audio_mixer_panel.py:211-236（对照 clear_all_tracks 238-248 有清理）` | medium | `deregister_track` 不清理 `_hidden_track_ids` 与 `_armed_track`，残留过期 id |
| AUD-37628 | Suggestion | process | `src/shared/ui/audio_mixer/audio_mixer_panel.py:125, 187, 259, 424, 566, 588, 601, 642, 655` | high | 大量跨模块私有成员访问，破坏封装边界 |
| AUD-37629 | Suggestion | process | `src/shared/ui/audio_mixer/audio_mixer_panel.py:294, 405, 539` | high | 公共方法参数类型注解缺失/不完整 |
| AUD-37701 | Major | bug | `src/shared/ui/audio_mixer/automation_lane.py:211-224（另见 371-374）` | medium | 拖动关键帧会在模型中留下"关键帧轨迹"并按鼠标移动刷爆撤销栈 |
| AUD-37702 | Major | bug | `src/shared/ui/audio_mixer/automation_lane.py:266、379-380（消费端缺失：src/app/controllers/edit_controller.py:3217-3220）` | medium | 双击切换插值类型的信号全库无消费者，功能静默失效 |
| AUD-37703 | Suggestion | process | `src/shared/ui/audio_mixer/automation_lane.py:359-361` | high | AutomationLane 直接改写子控件私有字段 |
| AUD-37704 | Suggestion | process | `src/shared/ui/audio_mixer/compressor_dialog.py:149-209（同构代码还存在于 delay_dialog.py:143-202、imager_dialog.py:123-182、dynamics_dialog.py:51-118）` | medium | `_make_knob_spin` 旋钮工厂在 4 个对话框间复制粘贴，靠动态属性传递状态 |
| AUD-37705 | Suggestion | process | `src/shared/ui/audio_mixer/delay_dialog.py:143-202（167-170 行` | high | 旋钮工厂复制品：decimals 硬编码 0，与 compressor 版本行为分叉 |
| AUD-37706 | Major | doc-gap | `src/shared/ui/audio_mixer/dynamics_dialog.py:177-182、219（模块 docstring 10-11 行承诺"找不到则用默认参数显示"）` | medium | 面板打开时无实例的 tab 显示"范围最小值"而非默认参数，与文档承诺相反 |
| AUD-37707 | Suggestion | process | `src/shared/ui/audio_mixer/dynamics_dialog.py:268-275` | high | 外部删除 DY 实例后 `load_params(None)` 完全不回退，tab 显示陈旧参数 |
| AUD-37708 | Suggestion | process | `src/shared/ui/audio_mixer/dynamics_dialog.py:427-440` | medium | enabled 双真相源：tab 开关只写 params["enabled"]，实例级 enabled 恒为 old.enabled/True |
| AUD-37709 | Suggestion | process | `src/shared/ui/audio_mixer/dynamics_dialog.py:51-54、152-161` | high | `_DynamicsTab` 构造回调与 `_make_knob` 参数缺类型注解 |
| AUD-37710 | Suggestion | process | `src/shared/ui/audio_mixer/eq_presets.py:56-58` | high | `get_presets_for_mode` 返回模块级可变列表的引用，调用方可污染全局预设 |
| AUD-37711 | Suggestion | process | `src/shared/ui/audio_mixer/imager_dialog.py:123-182` | high | 旋钮工厂第三份复制（同 AUD-37704） |
| AUD-37712 | Major | perf-opportunity | `src/shared/ui/audio_mixer/limiter_dialog.py:197-206` | medium | `_on_reset` 未阻塞信号：threshold 变化时 release 不会被重置，且一次点击多次 committed |
| AUD-37800 | Suggestion | process | `src/shared/ui/audio_mixer/mixer_separator.py:15,26` | high | 公共方法参数缺类型注解（parent/event） |
| AUD-37801 | Suggestion | process | `src/shared/ui/audio_mixer/noise_gate_dialog.py:174-177,202-205` | medium | QDial 滚轮/键盘调整绕过三段式节流，每个步进发一次 committed |
| AUD-37802 | Suggestion | process | `src/shared/ui/audio_mixer/noise_gate_dialog.py:127-130,182-186,192` | high | 用动态私有属性在 QWidget 上传递控件状态，类型不安全 |
| AUD-37803 | Critical | bug | `src/shared/ui/audio_mixer/preset_dialog.py:98,147-153` | medium | Apply 按钮把 clicked(bool) 的 False 当 QListWidgetItem 传入槽，必崩 AttributeError |
| AUD-37804 | Major | bug | `src/shared/ui/audio_mixer/preset_dialog.py:134-142` | medium | 保存预设丢弃 AM-13 补齐的 effect_chain/output_bus_id/sends/is_bus 字段 |
| AUD-37805 | Suggestion | risk | `src/shared/ui/audio_mixer/preset_dialog.py:112-119,143-145` | high | UI 线程同步文件 IO，且保存失败静默 |
| AUD-37806 | Suggestion | process | `src/shared/ui/audio_mixer/reverb_dialog.py:179-182,208-211` | medium | QDial 滚轮/键盘调整绕过三段式节流（同 AUD-37801） |
| AUD-37807 | Suggestion | process | `src/shared/ui/audio_mixer/reverb_dialog.py:132-135,187-191` | high | 动态私有属性模式 + 与 noise_gate_dialog 大段复制 |
| AUD-37808 | Critical | bug | `src/shared/ui/audio_mixer/routing_dialog.py:212,261-270,303-310` | medium | 打开对话框即触发 committed 且此时 sends 列表为空，消费端按替换语义删除该轨全部 Send |
| AUD-37809 | Critical | risk | `src/shared/ui/audio_mixer/routing_dialog.py:78,136-141,283-287；src/features/edit/core/timeline_v2.py:1602-1604` | medium | Send 空 target_bus_id 构造抛 ValueError 未捕获，多条用户路径触发崩溃 |
| AUD-37810 | Major | bug | `src/shared/ui/audio_mixer/routing_dialog.py:290-297,327-335` | medium | 环检测告警结果被丢弃，校验失败仍提交；且图简化使 Bus-Bus 环实际不可检测 |
| AUD-37811 | Major | bug | `src/shared/ui/audio_mixer/routing_dialog.py:153,112-121` | high | routing_preview 信号声明但从未发射，拖动 send level 期间无任何实时反馈 |
| AUD-37812 | Major | bug | `src/shared/ui/audio_mixer/routing_dialog.py:123-129,272-278` | high | 删除 Send 后槽位控件不移除不隐藏，UI 与模型状态不一致 |
| AUD-37813 | Suggestion | process | `src/shared/ui/audio_mixer/routing_dialog.py:29,169,345-349,70/91/135/284/322` | high | 杂项：closeEvent 冗余提交、未用导入、死属性、函数内 import |
| AUD-37814 | Major | bug | `src/shared/ui/audio_mixer/routing_matrix_dialog.py:126,213-214,237-240` | medium | Spill 交互未接线：无表头点击连接，spill()/spill_requested 无法由用户触发 |
| AUD-37815 | Suggestion | process | `src/shared/ui/audio_mixer/routing_matrix_dialog.py:231-235,277-279` | high | routing_changed 直接外泄内部可变 dict，get_routing 仅浅拷贝 |
| AUD-37816 | Suggestion | process | `src/shared/ui/audio_mixer/routing_matrix_dialog.py:27-33,178-181,249` | high | 外部 routing/sends 结构未验证；存在未用导入 |
| AUD-37817 | Suggestion | process | `src/shared/ui/audio_mixer/saturation_dialog.py:163-166,189-192` | medium | QDial 滚轮/键盘调整绕过三段式节流（同 AUD-37801） |
| AUD-37818 | Suggestion | process | `src/shared/ui/audio_mixer/saturation_dialog.py:171-175,181` | high | 动态私有属性模式 + 与 noise_gate/reverb 大段复制 |
| AUD-37819 | Suggestion | perf-opportunity | `src/shared/ui/audio_mixer/strip_eq_dialog.py:124-141` | medium | 频响曲线在 paintEvent 中每次重绘都重算 scipy 频响，无缓存 |
| AUD-37820 | Suggestion | process | `src/shared/ui/audio_mixer/strip_eq_dialog.py:230-231,584-587` | medium | band 推子滚轮/键盘绕过三段式节流（同 AUD-37801 机制） |
| AUD-37821 | Suggestion | process | `src/shared/ui/audio_mixer/strip_eq_dialog.py:217,344-345,493-495` | medium | 跨对象访问私有成员、回调无类型注解、backend 探测异常被静默吞掉 |
| AUD-37900 | Critical | bug | `src/shared/ui/audio_mixer/track_mixer_widget.py:1166-1168, 1174-1191` | medium | Limiter 参数编辑只走 preview 出口，永不进命令栈，用户修改不持久化、不可撤销 |
| AUD-37901 | Major | bug | `src/shared/ui/audio_mixer/track_mixer_widget.py:591-599, 1638-1650` | medium | balance 拖动期经 spinbox 直连泄露 balance_changed（commit 信号），每次拖动向 undo 栈灌入大量命令且松手重复提交 |
| AUD-37902 | Major | bug | `src/shared/ui/audio_mixer/track_mixer_widget.py:1045-1061, 1180-1187, 1230-1242` | medium | 删除效果器实例不处理其打开中的编辑对话框：后续编辑被静默丢弃并向命令栈发出无变化链的重复提交 |
| AUD-37903 | Major | bug | `src/shared/ui/audio_mixer/track_mixer_widget.py:803-807, 809-821, 1748-1755` | medium | 三处 `except Exception: pass` 掩盖错误，且 EQ 后端探测失败默认"可用"，SciPy 缺失时降级警示（橙色 badge）永不出现 |
| AUD-37904 | Major | bug | `src/shared/ui/audio_mixer/track_mixer_widget.py:1113-1121, 1136-1141` | medium | `_sync_dialog_toggle` 的注释断言"setChecked 不发 toggled"与 Qt 语义相反，程序化 setChecked 会触发对话框处理器，造成级联重复 preview/commit |
| AUD-37905 | Major | bug | `src/shared/ui/audio_mixer/track_mixer_widget.py:313-318, 1662-1689` | medium | 标签右键菜单把 tag 标签坐标系的 pos 统一用 `_track_label.mapToGlobal` 映射，右键 tag 时菜单弹出位置偏移 |
| AUD-37906 | Suggestion | process | `src/shared/ui/audio_mixer/track_mixer_widget.py:267-270, 1819-1822` | medium | EQ 胶囊 tooltip 在构造期即被 `_update_badges_style` 清空，"Open Graphic EQ" 提示永不显示 |
| AUD-37907 | Suggestion | process | `src/shared/ui/audio_mixer/track_mixer_widget.py:150-152` | high | 模块声明的 `strip_eq_preview` / `strip_eq_committed` 信号从未 emit（死 API） |
| AUD-37908 | Suggestion | process | `src/shared/ui/audio_mixer/track_mixer_widget.py:189, 1169-1171, 1219-1221 等` | medium | 8 类效果器对话框字典无任何清理机制，已关闭对话框被 widget 永久引用 |
| AUD-37909 | Suggestion | process | `src/shared/ui/audio_mixer/track_mixer_widget.py:36-45, 840-843` | high | FX 列表项显示名使用硬编码英文 `EFFECT_SHORT_NAMES`，与菜单/对话框的 i18n 显示名不一致 |
| AUD-37910 | Suggestion | process | `src/shared/ui/audio_mixer/track_mixer_widget.py:1606-1609, 953, 1691, 189, 51` | medium | 拖动热路径内重复 import；`pos`/`event` 参数与多个集合缺类型注解 |
| AUD-3800 | Critical | bug | `src/app/ui/library_panel.py:599-603` | medium | `rename_item` 目标已存在时 `os.rename` 静默覆盖同名文件，无冲突检查 |
| AUD-38000 | Critical | bug | `src/shared/ui/shortcut_manager.py:399-411（配合 464-482 的` | medium | import_preset 对外部预设的 per-value 类型零验证，非字符串/不可哈希值可致未处理 TypeError 使应用中止 |
| AUD-38001 | Major | bug | `src/shared/ui/column_grid.py:182-206（关键在 203-206；触发源 96-102 的 0ms retile 定时器与 107 的 slider.valueChanged）` | medium | apply_columns 每次执行都同步写 QSettings，窗口拖拽缩放期间产生高频磁盘写入 |
| AUD-38002 | Major | risk | `src/shared/ui/effect_degradation_indicator.py:306-309（对照同文件正确范式 164-166）` | medium | EffectDegradationPanel.refresh_settings 程序化 setChecked 未加信号锁，触发伪信号/manager 写入甚至递归回路 |
| AUD-38003 | Major | bug | `src/shared/ui/playback_hud.py:788-802（对照 950-955 的 hide_hud 与 932-941 的 toggle_visibility）` | medium | HUD 关闭按钮直连 QWidget.hide，绕过 hide_hud 状态同步：_visible 残留 True、定时器不停、visibility_changed 不发射 |
| AUD-38004 | Major | bug | `src/shared/ui/playback_hud_integration.py:299-317` | medium | on_playback_started 深入访问 source_manager 私有属性且无 hasattr 防御 |
| AUD-38005 | Suggestion | process | `src/shared/ui/effect_degradation_indicator.py:25,33,36` | high | 多个导入符号未使用（List/QToolTip/QFont/QColor/QPalette） |
| AUD-38006 | Suggestion | process | `src/shared/ui/effect_degradation_indicator.py:67-69` | high | 更新定时器无条件常驻运行（500ms），控件隐藏时不暂停 |
| AUD-38007 | Suggestion | risk | `src/shared/ui/playback_hud.py:283-292, 557-597` | medium | record_sample 样本列表无界增长且每样本全量重排序，_max_history/_history 声明后从未使用 |
| AUD-38008 | Suggestion | process | `src/shared/ui/playback_hud.py:693-702（配合 1103-1110 调用方）` | medium | export_log 用宽泛 except Exception 吞掉全部失败且不给用户任何反馈 |
| AUD-38009 | Suggestion | doc-gap | `src/shared/ui/playback_hud.py:49-51` | high | 模块 docstring 使用示例错误：用 PlaybackHUDPanel 构造"数据模型" |
| AUD-3801 | Major | bug | `src/app/ui/library_panel.py:59-60、93-94、164、222、251（放大器：34-36）` | medium | paint 热路径每项执行 stat 调用并每次新建 SQLite 连接查询评分，缩略图风暴放大为整屏重绘 |
| AUD-38010 | Suggestion | process | `src/shared/ui/playback_hud_integration.py:198, 221-228` | high | 硬编码中文字符串与快捷键绕过 I18N/ShortcutManager 体系 |
| AUD-38011 | Suggestion | process | `src/shared/ui/shortcut_manager.py:416-420（` | medium | import/replace 路径每条目触发一次全量 config 落盘（O(N) 次写盘） |
| AUD-38012 | Suggestion | process | `src/shared/ui/shortcut_manager.py:122-126（配合 469-471）` | medium | 从 config 加载的 custom_shortcuts 值类型未验证，损坏配置可在注册阶段抛异常 |
| AUD-38013 | Suggestion | process | `src/shared/ui/shortcut_manager.py:413-414` | high | import_preset 的 mode 参数未验证，未知值静默按 merge 处理 |
| AUD-38014 | Suggestion | process | `src/shared/ui/panel_divider.py:27,52,56` | high | Qt 事件覆盖方法的 event 参数缺少类型注解 |
| AUD-3802 | Major | perf-opportunity | `src/app/ui/library_panel.py:328-343` | high | 按 Rating 排序时 `lessThan` 每次比较新建 SQLite 连接，大目录排序冻结 UI |
| AUD-3803 | Major | process | `src/app/ui/library_panel.py:599-609` | medium | 重命名/删除不迁移 `.topos` 工程边车文件与 DB 评分记录，编辑关联静默断裂 |
| AUD-3804 | Major | bug | `src/app/ui/files_panel.py:170-178（配合 180-188）` | medium | 依赖缺失时 `_load_url` 无限重试，错误页加载成功会重置重试计数，绕过 [CRASH-6] 的有上限退避设计 |
| AUD-3805 | Major | bug | `src/app/ui/info_panel.py:160-176` | medium | `update_metadata` 不清空缺失字段，切换图片后残留上一张的 EXIF 值 |
| AUD-3806 | Suggestion | process | `src/app/ui/files_panel.py:185-188（配合 296-304）` | medium | `_pending_path` 一次性消费：前端 API 未就绪时目录同步静默丢失且不重试 |
| AUD-3807 | Suggestion | process | `src/app/ui/files_panel.py:79-369（如` | high | 多数方法缺类型注解 |
| AUD-3808 | Suggestion | process | `src/app/ui/info_panel.py:119-125` | high | 用户勾选的可选字段（visible_keys）不持久化 |
| AUD-3809 | Suggestion | process | `src/app/ui/library_panel.py:604-609` | high | `delete_item` 用 `shutil.rmtree` 永久删除目录，确认框只显示 basename |
| AUD-3810 | Suggestion | process | `src/app/ui/library_panel.py:540-545` | medium | `set_root_path` 未校验 isdir，且每次导航同步写配置文件 |
| AUD-38100 | Suggestion | doc-gap | `src/shared/ui/video_control_panel.py:20-26 与 29-34` | medium | 类文档声明的信号契约与实际 pyqtSignal 声明不一致（frame_changed 不存在、play_toggled 缺 bool 参数） |
| AUD-38101 | Suggestion | process | `src/shared/ui/video_control_panel.py:202（对照 187）` | high | 滑块拖动预览硬编码中文 "帧:" 文案，绕过 I18N |
| AUD-38102 | Suggestion | process | `src/shared/ui/video_control_panel.py:158-171` | medium | set_total_frames / set_current_frame 不做输入验证，负值与越界帧直接进入状态 |
| AUD-38103 | Suggestion | process | `src/shared/ui/video_control_panel.py:7-12` | high | 未使用的导入 QStyle / QFrame / QIcon |
| AUD-38104 | Suggestion | process | `src/shared/ui/video_control_panel.py:36` | high | __init__ 的 parent 参数缺类型注解 |
| AUD-3811 | Suggestion | process | `src/app/ui/library_panel.py:415、575-579、595、600、605` | high | 排序下拉、上下文菜单与对话框字符串硬编码英文，绕过 I18N |
| AUD-38110 | Suggestion | process | `src/shared/ui/style_sheet.py:26-34（承诺），1028-1031、1241-1248（违反），1667-1722（TAB_STYLE/LAYER_LIST_STYLE 同样硬编码）` | medium | C 颜色常量承诺"修改此处即可全局生效"，但最大的消费方 DARK_THEME 等模板全部硬编码十六进制色值未引用 C |
| AUD-38111 | Suggestion | process | `src/shared/ui/style_sheet.py:16-23、1443、1661` | medium | checkmark 图标路径在 import 期解析但未校验文件存在，缺失时静默降级 |
| AUD-3812 | Suggestion | process | `src/app/ui/library_panel.py:611-612；51-609（如` | high | 模块尾部残留 `__main__` 直跑测试代码；大量方法缺类型注解 |
| AUD-38200 | Suggestion | process | `src/shared/ui/widgets/chevron_combo.py:28,50,74,78,82,116` | high | 公共方法普遍缺少类型注解 |
| AUD-38201 | Suggestion | perf-opportunity | `src/shared/ui/widgets/cie_scope_widget.py:418-435, 499-503` | medium | 光谱轨迹与色温曲线每次重绘全量重算，静态几何未预计算 |
| AUD-38202 | Suggestion | process | `src/shared/ui/widgets/cie_scope_widget.py:205-210` | high | `_px_to_xy` 为死代码 |
| AUD-38203 | Suggestion | process | `src/shared/ui/widgets/clickable_slider.py:18-32` | medium | `set_gradient_stops` 对 stops 无输入验证，异常输入生成非法 QSS 被静默忽略 |
| AUD-38204 | Suggestion | process | `src/shared/ui/widgets/clickable_slider.py:10,14,18` | high | 方法与参数缺少类型注解 |
| AUD-38205 | Critical | risk | `src/shared/ui/widgets/color_picker_control.py:145-150, 155-159, 169-170` | medium | 吸管覆盖层在自身事件处理栈内丢失最后一个 Python 引用被立即析构（use-after-free 风险） |
| AUD-38206 | Suggestion | process | `src/shared/ui/widgets/color_picker_control.py:22-30, 73, 112-119` | medium | scene-linear RGBA 在构造/刷新时即被量化为 8-bit sRGB，无用户交互也会损失 HDR 精度 |
| AUD-38207 | Suggestion | process | `src/shared/ui/widgets/color_warper.py:28-31, 68-84` | medium | `reset_grid`/`set_grid_size` 程序化路径同步 emit changed，控件不提供无信号变体 |
| AUD-38208 | Suggestion | process | `src/shared/ui/widgets/color_warper.py:196-199 vs 261` | high | 拖拽幅值上限 1.1 与 `apply_deltas` 的 sat 钳制 1.0 不一致，拖拽状态不能完整往返 |
| AUD-38209 | Suggestion | test-gap | `src/shared/ui/widgets/color_warper.py:92 vs 152, 186` | high | `_padding` 在 paintEvent 赋值（35），命中测试的 fallback 默认却是 30，首帧前几何不一致 |
| AUD-38210 | Suggestion | process | `src/shared/ui/widgets/color_warper.py:13,28,33,40,68,86,149,172,180,231,240,247,251` | high | 全文件方法缺少类型注解 |
| AUD-38211 | Major | bug | `src/shared/ui/widgets/curves_widget.py:355-363 vs 422-425` | medium | 左键点击空白处（未加点）时 interaction_started 无配对 interaction_ended，undo/空闲渲染状态被破坏 |
| AUD-38212 | Major | bug | `src/shared/ui/widgets/curves_widget.py:588-640（关键 632-638）` | medium | set_lut 的 `emit_changed=True` 路径被自身 Qt 级 blockSignals 完全吞掉，信号永不送达 |
| AUD-38213 | Major | bug | `src/shared/ui/widgets/curves_widget.py:428-434` | medium | mouseDoubleClickEvent 未检查鼠标按钮，右键/中键双击会意外重置当前通道曲线 |
| AUD-38214 | Major | doc-gap | `src/shared/ui/widgets/curves_widget.py:573-575 vs 591, 604` | high | set_lut 文档声明支持 (256, 3) 形状但代码未实现，旧工程恢复静默丢失曲线 |
| AUD-38215 | Major | bug | `src/shared/ui/widgets/curves_widget.py:336-337` | high | calculate_curve 结尾裸 `except:` 掩盖插值领域错误并静默降级 |
| AUD-38216 | Suggestion | process | `src/shared/ui/widgets/curves_widget.py:547-551` | medium | set_curves_data 不验证外部工程数据 |
| AUD-38217 | Suggestion | process | `src/shared/ui/widgets/curves_widget.py:685` | high | add_point_from_color 内 `import colorsys` 未使用 |
| AUD-38218 | Suggestion | process | `src/shared/ui/widgets/curves_widget.py:411-414` | medium | 拖拽的相邻点约束可矛盾，后写入的 min 覆盖 max，点序可被瞬时破坏 |
| AUD-38219 | Suggestion | perf-opportunity | `src/shared/ui/widgets/curves_widget.py:166-172, 226` | high | paintEvent 每帧对 4 个通道全量重算 calculate_curve，未复用未脏通道缓存 |
| AUD-38220 | Suggestion | process | `src/shared/ui/widgets/curves_widget.py:119,123,127,133,294,339,384,422,428,528,535 等` | high | 多数交互/数据方法缺少类型注解 |
| AUD-38221 | Suggestion | process | `src/shared/ui/widgets/drag_value.py:199-202` | high | blockSignals 覆盖遮蔽 QWidget.blockSignals，语义与 Qt 契约不符 |
| AUD-38222 | Suggestion | process | `src/shared/ui/widgets/drag_value.py:206-213` | high | setRange/setSingleStep 不验证退化输入 |
| AUD-38300 | Suggestion | process | `src/shared/ui/widgets/gpu_scope_widget.py:241, 263-268` | high | blit/chrome 失败仅以 debug 级宽泛捕获，teardownGL 裸 pass，生产环境难以诊断 |
| AUD-38301 | Suggestion | process | `src/shared/ui/widgets/gpu_scope_widget.py:338-346, 387-413` | medium | `_paint_waveform_scale` 的 nits / code10_video 两分支为逐行重复代码；`_legend_strip` 属性未在 `__init__` 声明 |
| AUD-38302 | Major | bug | `src/shared/ui/widgets/hsl_curves_widget.py:832-864` | medium | `set_lut(emit_changed=True)` 的 changed 信号被自身 blockSignals 吞掉，永不送达接收方 |
| AUD-38303 | Major | bug | `src/shared/ui/widgets/hsl_curves_widget.py:553-562, 582` | medium | 点击 hue 曲线端点的 wrap-around 同步修改了曲线数据，但未置脏、未重建 LUT，UI 显示与渲染状态发散 |
| AUD-38304 | Major | bug | `src/shared/ui/widgets/hsl_curves_widget.py:527-528` | high | `calculate_curve` 使用裸 `except:` 兜底，违反 .clinerules 并掩盖插值错误 |
| AUD-38305 | Suggestion | process | `src/shared/ui/widgets/hsl_curves_widget.py:774-795` | medium | `set_curves_data` 内联复制 update_lut 的重建逻辑，且对恢复数据无验证/钳制 |
| AUD-38306 | Suggestion | process | `src/shared/ui/widgets/hsl_curves_widget.py:112` | high | `hsl_color_received` 信号全库无 emit、无 connect，为死代码 |
| AUD-38307 | Suggestion | process | `src/shared/ui/widgets/hsl_curves_widget.py:246-258, 484, 535, 603, 641, 647, 755, 762 等` | high | 大量公开/事件方法缺类型注解 |
| AUD-38308 | Major | bug | `src/shared/ui/widgets/lut_gallery.py:236-253` | medium | LUT 导入复制失败后仍把指向不存在路径的条目加入图库 |
| AUD-38309 | Suggestion | perf-opportunity | `src/shared/ui/widgets/lut_gallery.py:2` | high | 顶层 `import cv2` 未使用，重依赖拖慢模块导入 |
| AUD-38310 | Suggestion | process | `src/shared/ui/widgets/lut_gallery.py:269-270, 306-308` | high | `_on_zoom_changed` 为无连接的死代码；tooltip 探测用裸 `except Exception: pass` |
| AUD-38311 | Suggestion | process | `src/shared/ui/widgets/param_slider_row.py:65, 84-87, 126, 238-240` | high | int() 截断造成 slider/drag 初始与步进量化偏差；部分方法缺注解 |
| AUD-38312 | Suggestion | process | `src/shared/ui/widgets/pref_drag_value.py:16` | high | 跨层导入 export_panel 的私有常量，导入期拉起整个导出面板模块链 |
| AUD-38313 | Suggestion | process | `src/shared/ui/widgets/qualifier_range_bar.py:15-17, 37-48` | medium | `type` 参数遮蔽内置名且无合法值验证；setter 不校验外部传入的取值范围 |
| AUD-38314 | Suggestion | process | `src/shared/ui/widgets/qualifier_range_bar.py:162, 197, 219, 233, 240` | high | 事件处理器与公共方法缺类型注解；使用 PyQt6 已弃用的 `event.pos()` |
| AUD-38315 | Suggestion | process | `src/shared/ui/widgets/realtime_fps_indicator.py:63-73, 119-135` | high | 过期判定逻辑在 `is_realtime` 与 `_refresh_display` 重复；`_refresh_display` 的 now 参数无调用方使用 |
| AUD-38400 | Suggestion | process | `src/shared/ui/widgets/toggle_switch.py:30-55` | medium | 无键盘访问性：无 keyPressEvent、未设置 focusPolicy，无法用 Tab/Space 切换 |
| AUD-38401 | Suggestion | process | `src/shared/ui/widgets/toggle_switch.py:30,51,57` | high | `parent` / `event` 参数缺类型注解 |
| AUD-38402 | Suggestion | process | `src/shared/ui/widgets/topos_controls.py:219-226` | medium | mousePressEvent 双重设值：自定义跳转计算后又交 super()，依赖硬编码 14px 手柄宽度与样式一致 |
| AUD-38403 | Suggestion | process | `src/shared/ui/widgets/topos_controls.py:76` | high | `mouseDoubleClickEvent` 等参数缺类型注解 |
| AUD-38404 | Critical | bug | `src/shared/ui/widgets/vectorscope_widget.py:845-853` | medium | HistogramWidget 对 NaN 输入无防护：np.bincount 抛 ValueError 未捕获，PyQt6 槽内未处理异常默认 qFatal 终止进程 |
| AUD-38405 | Major | bug | `src/shared/ui/widgets/vectorscope_widget.py:182-187` | medium | Vectorscope 将 HDR 输入 clip(0,1)（附 >1.5 除 255 启发式），超白像素色度被非线性扭曲 |
| AUD-38406 | Major | risk | `src/shared/ui/widgets/vectorscope_widget.py:161-169` | medium | np.random.choice(replace=False) 对全像素索引全量置换：4K 实测 349ms/次 UI 线程卡顿，且逐帧重随机致迹线闪烁 |
| AUD-38407 | Suggestion | process | `src/shared/ui/widgets/vectorscope_widget.py:590-598,691-700` | medium | 波形/Parade/Luma 密度路径对 NaN 无防护，NaN 像素静默落入顶行产生假迹线 |
| AUD-38408 | Suggestion | risk | `src/shared/ui/widgets/vectorscope_widget.py:200-229,244-321` | medium | 800×800 热图全量重算在 UI 线程，多次 640k 元素幂/对数运算吃帧预算 |
| AUD-38409 | Suggestion | process | `src/shared/ui/widgets/vectorscope_widget.py:344-357,392-402,427-436` | medium | zoom=2 时 graticule 刻度圈/靶框固定不缩放，与数据错位导致饱和度读数错误 |
| AUD-38410 | Suggestion | process | `src/shared/ui/widgets/vectorscope_widget.py:868-869` | high | HistogramWidget.paintEvent 死代码：`rect.height() and rect.width() or (0, 0)` 立即被下一行覆盖 |
| AUD-38411 | Suggestion | process | `src/shared/ui/widgets/vectorscope_widget.py:892-901` | medium | HistogramWidget 每次重绘逐点 drawEllipse（3×256 个椭圆），无 QImage 缓存 |
| AUD-38412 | Suggestion | perf-opportunity | `src/shared/ui/widgets/vectorscope_widget.py:1433,1489-1495` | medium | FalseColor LUT 与图例条每帧重建，未按 (mode, transfer) 缓存 |
| AUD-38413 | Suggestion | process | `src/shared/ui/widgets/vectorscope_widget.py:585-603,691-704` | medium | `_waveform_density` 与 `WaveformWidget.update_waveform` 内嵌 `get_waveform_channel` 逻辑重复 |
| AUD-38414 | Suggestion | process | `src/shared/ui/widgets/vectorscope_widget.py:1128-1135` | medium | CieChromaticityWidget 对 ndim==2 输入 `image.shape[2]` 直接 IndexError；RGB→BGR→RGB 往返反转无实际意义 |
| AUD-38415 | Suggestion | process | `src/shared/ui/widgets/vectorscope_widget.py:1142-1143` | medium | 'gamma22' 传输特性用 2.4 幂（名义 2.2），'bt709' 用纯幂近似分段函数 |
| AUD-38416 | Suggestion | process | `src/shared/ui/widgets/vectorscope_widget.py:71,323,538,619,714,780,839,1084,1211,1362,1458 等` | high | 各 widget `__init__(self, parent=None)`、`paintEvent(self, event)` 等参数缺类型注解 |
| AUD-38417 | Suggestion | risk | `src/shared/ui/widgets/viewport_toolbar_base.py:146,176,233-243` | medium | mask/split 按钮程序化 setChecked 会再次触发 toggled 并重发 mask_toggled/split_toggled，基类未提供状态锁辅助 |
| AUD-38418 | Suggestion | process | `src/shared/ui/widgets/viewport_toolbar_base.py:78,288-292` | high | `parent` 参数缺类型注解；`set_timeline_choices(timelines: Any)` 弱类型 |
| AUD-38501 | Critical | bug | `src/shared/utils/geometry_math.py:67-75（0° 分支）、77-83（90° 分支，同一互换错误）` | medium | 0°/90° 边界分支的宽高约束互换，返回的“安全区域”可大于原图，裁切框获准越出图像边界 |
| AUD-38502 | Suggestion | process | `src/shared/utils/geometry_math.py:15` | high | 类型注解不正确：aspect_ratio: float = None 应为 Optional[float]，返回类型 tuple 未参数化 |
| AUD-38503 | Suggestion | doc-gap | `src/shared/utils/geometry_math.py:46-52` | high | docstring 示例数值与实际返回不符，误导维护者 |
| AUD-38600 | Major | bug | `src/shared/vault/bridge.py:57` | medium | 运算符优先级缺陷：clip_id 为空时调用方显式传入的 segment_id 被静默丢弃 |
| AUD-38601 | Major | bug | `src/shared/vault/bridge.py:74、213、227` | medium | relevance → confidence 换算未验证外部值：合法 0.0 被 `or` 误当缺失置为 1.0，越界/非数值直接抛 ValueError 穿透 |
| AUD-38602 | Suggestion | process | `src/shared/vault/bridge.py:50-53（98-101、147-150 同型）` | high | 秒→帧换算用 `or` 兜底且静默钳制，掩盖 vault 时间数据错误 |
| AUD-38603 | Suggestion | process | `src/shared/vault/bridge.py:63-64、199` | high | `.get(key, default)` 挡不住 JSON null，tags/keyframes 可能以 None 进入 payload |
| AUD-38604 | Suggestion | process | `src/shared/vault/bridge.py:57、108、154、203、217` | medium | 回退 segment_id 易碰撞，可能被下游按 ID 归并时静默丢段 |
| AUD-38605 | Suggestion | process | `src/shared/vault/bridge.py:126` | medium | face_track 置信度硬编码 0.9，未读取 provider 实际值 |
| AUD-38606 | Suggestion | process | `src/shared/vault/client.py:77-78、104-106、118-120` | medium | 三处宽泛 `except Exception`，is_available() 连 debug 日志都没有 |
| AUD-38607 | Suggestion | process | `src/shared/vault/client.py:113-116` | high | VaultStats 字段的 `.get` 默认值同样挡不住 JSON null，破坏声明类型 |
| AUD-38608 | Suggestion | process | `src/shared/vault/__init__.py:1-2` | high | 包级入口缺少 DORMANT 标记头，与两个子模块不一致 |
| AUD-38700 | Major | bug | `src/shared/video/camera_gamut_matrices.py:249-267` | medium | resolve_debayer_params 整对象优先级：RawParams 中性默认静默覆盖 TRWM as-shot WB/黑位 |
| AUD-38701 | Suggestion | process | `src/shared/video/camera_gamut_matrices.py:203-212` | medium | resolve_gamut_id 对"显式但未知"的 color_space 静默落到 TRWM id |
| AUD-38702 | Major | bug | `src/shared/video/cfa_cpu_debayer.py:104-109（对照 src/shared/video/cfa_gpu_debayer.py:150-153）` | medium | black_level 应用点与 GPU shader 不一致：逐平面"先减后平均" vs GPU"先平均后减"，阴影区两链可见漂移 |
| AUD-38703 | Suggestion | process | `src/shared/video/cfa_cpu_debayer.py:118（对照 src/shared/video/cfa_gpu_debayer.py:295-296）` | medium | 位深分界谓词与 GPU 不一致：CPU `>12` 走线性、GPU `<16` 走 LOG0，13–15 位输入两链完全分歧 |
| AUD-38704 | Suggestion | process | `src/shared/video/cfa_cpu_debayer.py:33-40, 43-79` | medium | _assemble_rgb 每次组装触发 12 次全平面 np.pad 拷贝 |
| AUD-38705 | Major | bug | `src/shared/video/cfa_gpu_debayer.py:410-418` | medium | upload_and_render 的 viewport 参数被 fbo.use() 覆盖，形同虚设 |
| AUD-38706 | Suggestion | process | `src/shared/video/cfa_gpu_debayer.py:278-281, 329-331` | medium | dtype 构造参数为死代码；_write_plane 对非 uint16 输入静默截断 |
| AUD-38707 | Suggestion | process | `src/shared/video/color_frame_provider_adapter.py:287, 68-76` | medium | request_proxy 返回注解引用了从未导入的 "GpuDecodedFrame" |
| AUD-38708 | Suggestion | process | `src/shared/video/color_frame_provider_adapter.py:232-256` | medium | open() 宽泛 except 掩盖领域错误；失败路径状态残留 |
| AUD-38709 | Suggestion | process | `src/shared/video/color_frame_provider_adapter.py:337-341, 355-357` | high | request_master 的 timeout_ms 参数完全未使用 |
| AUD-38710 | Suggestion | process | `src/shared/video/color_frame_provider_adapter.py:440-456` | high | get_stats 返回注解与实际结构不符（嵌套 dict） |
| AUD-38801 | Major | risk | `src/shared/video/decode_worker.py:661-679（配合 481-491、1057-1064）` | medium | 帧去重标记的检查与置位不原子，同帧可被重复解码（check-then-act 竞态） |
| AUD-38802 | Major | risk | `src/shared/video/decode_worker.py:341-382（配合 310-339）` | medium | stop() 超时后状态为 ERROR，再次 start() 会清除停止事件并产生双工作线程 |
| AUD-38803 | Suggestion | risk | `src/shared/video/decode_worker.py:134-146、299-302、1497-1515` | high | DecodeWorkerStats 跨线程读写无锁，与模块"所有状态变更加锁"的声明不符 |
| AUD-38804 | Suggestion | risk | `src/shared/video/decode_worker.py:943-944、951-959、914、1183` | high | `_last_clock_frame`、`_prefetch_lookahead`、`_current_decode_frame` 跨线程读写无同步 |
| AUD-38805 | Suggestion | process | `src/shared/video/decode_worker.py:896、1165` | high | 用 `object.__setattr__` 动态注入 `is_proxy` 属性，对 GpuDecodedFrame 是未声明字段 |
| AUD-38806 | Suggestion | process | `src/shared/video/decode_worker.py:1391-1431` | medium | DecodeWorkerManager.update_generation 不同步帧队列的 generation，与 increment_generation 不对称 |
| AUD-38807 | Suggestion | process | `src/shared/video/decode_worker.py:698-700、916-918、1185-1187、1300-1302、1379-1388` | high | 多处宽泛 `except Exception` 且 error 日志不带堆栈，掩盖领域错误根因 |
| AUD-38808 | Suggestion | process | `src/shared/video/decode_worker.py:450-479、1034-1075（对照 548）` | medium | request_frame / request_master_frame 不校验 frame_index 上界 |
| AUD-38809 | Suggestion | process | `src/shared/video/decode_worker.py:95-98、690-696` | high | 空闲期 100Hz 轮询唤醒，且 DECODE_INTERVAL_SECONDS 常量从未使用 |
| AUD-38810 | Suggestion | process | `src/shared/video/decode_worker.py:1304-1327、1340-1369` | medium | start_workers 部分失败不回滚；stop_workers 串行 join 最坏耗时 2×timeout，超出 2 秒退出设计指标 |
| AUD-38811 | Suggestion | process | `src/shared/video/decode_worker.py:1066-1067、632-640（对照 1097-1114）` | high | MasterDecodeWorker API 一致性：优先级硬编码 SEEK 忽略 reason；has_pending_requests 不考虑 master 专用槽位 |
| AUD-38901 | Suggestion | process | `src/shared/video/denoise/frame_types.py:53-59` | medium | `frame_indices` 回退分支的距离与顺序计算错误 |
| AUD-38902 | Suggestion | risk | `src/shared/video/denoise/frame_types.py:75-216` | high | `TemporalFrameWindowCache` 全库无消费者，锁/LRU 逻辑零投产验证 |
| AUD-38903 | Suggestion | process | `src/shared/video/denoise/params.py:145（对照 :129）` | high | `blend` 非法值回退默认 0.5 与字段默认 0.8 不一致 |
| AUD-38904 | Suggestion | process | `src/shared/video/denoise/params.py:236-238` | high | `for_quality` 对未知 quality_mode 静默落入 export 最高成本分支 |
| AUD-38905 | Major | risk | `src/shared/video/denoise/window_provider.py:193-205（对照 :88-91、:175-191）` | medium | `shutdown()` 全库无调用点，卸载视频后独立解码会话泄漏 |
| AUD-38906 | Suggestion | process | `src/shared/video/denoise/window_provider.py:121-126` | medium | token 校验与 emit 之间存在 TOCTOU，且消费端不校验 center_frame |
| AUD-38907 | Suggestion | doc-gap | `src/shared/video/denoise/window_provider.py:6（对照 :30）` | high | 模块 docstring 称宽度上限 1920，实际常量为 2560 |
| AUD-38908 | Suggestion | process | `src/shared/video/denoise/window_provider.py:44、78、90` | high | `_debounce_token` 只写不读，属死状态 |
| AUD-38909 | Suggestion | process | `src/shared/video/denoise/window_provider.py:75-76、179-181、201-202` | medium | 多处宽泛 except 静默吞错，违反"不得宽泛 except 掩盖领域错误"铁律 |
| AUD-38910 | Suggestion | process | `src/shared/video/denoise/window_provider.py:212-218、294` | medium | `_MATRIX_MAP` 未覆盖 bt2020c，未知矩阵静默按 BT.709 转换 |
| AUD-38911 | Suggestion | process | `src/shared/video/denoise/window_provider.py:319-322` | medium | `_packed_to_rgb` 对未知 4/3 通道格式按 BGRA/BGR 猜测 |
| AUD-38912 | Suggestion | process | `src/shared/video/denoise/window_provider.py:167` | high | `_ensure_clip` 的 `service` 参数缺类型注解 |
| AUD-39000 | Major | bug | `src/shared/video/external_yuv_renderer.py:150-152, 226-233, 257-258` | medium | 宽泛 `except Exception` 静默吞掉 GL/着色器领域错误，且 render 失败路径不恢复 GL 状态 |
| AUD-39001 | Suggestion | process | `src/shared/video/external_yuv_renderer.py:55-56` | medium | 着色器输出无条件 `clamp(0.0, 1.0)` 将 SDR unorm 假设固化在通用 pass 中 |
| AUD-39002 | Suggestion | process | `src/shared/video/external_yuv_renderer.py:117-119` | medium | GL 上下文失效后资源缓存永不重建，pass 永久静默退化为 CPU 路径 |
| AUD-39003 | Critical | risk | `src/shared/video/fallback_types.py:363-371（锁声明 :270，二次加锁 :326）` | medium | `export_to_json` 持有非重入锁时再次加锁，调用即确定性死锁 |
| AUD-39004 | Major | doc-gap | `src/shared/video/fallback_types.py:301-312` | medium | `get_recent` 实际返回时间正序，与文档承诺"最新的在前"相反 |
| AUD-39005 | Suggestion | process | `src/shared/video/fallback_types.py:312` | high | `get_recent(limit=0)` 因 `[-0:]` 切片语义返回全部事件 |
| AUD-39006 | Suggestion | doc-gap | `src/shared/video/fallback_types.py:26` | high | 模块 docstring 示例 `log_fallback(event)` 用法错误，照抄会抛 AttributeError |
| AUD-39007 | Suggestion | process | `src/shared/video/fallback_types.py:47, 264-268` | high | 单例 `__init__` 的 `_initialized` 检查-设置非原子；`import time` 未使用 |
| AUD-39008 | Suggestion | process | `src/shared/video/fallback_types.py:490-492, 518-521, 550-551, 499-500, 606-611` | medium | extract_* 辅助函数提前返回阻断回退链，且依赖私有属性、可能返回字符串 "None" |
| AUD-39009 | Suggestion | process | `src/shared/video/frame_provider_protocol.py:190-199` | high | `FrameProvider.get_frame` 返回类型 `object` 过弱 |
| AUD-39100 | Critical | risk | `src/shared/video/frame_provider_service.py:1230、2043-2073（对照 2118-2122）` | medium | 关闭剪辑会话不持有 decode_lock 释放媒体源，与在途解码构成 use-after-close 竞态 |
| AUD-39101 | Major | bug | `src/shared/video/frame_provider_service.py:388-391、407-410（对照 291-294、140）` | medium | AUTO 质量缓存桶错位：proxy 源解码的帧写入 master 缓存，被后续 MASTER 请求命中 |
| AUD-39102 | Major | risk | `src/shared/video/frame_provider_service.py:851-867（对照 1833、1842、1904-1911、1924）` | medium | open_clip 打开失败清理只移除映射，不释放已打开的源 → master/VT 源泄漏 |
| AUD-39103 | Major | bug | `src/shared/video/frame_provider_service.py:753-767（对照 779、831、851-861）` | medium | 并发 open_clip 共享路径在源打开完成前返回 clip_id，打开失败时吞掉引用并留下死 clip_id |
| AUD-39104 | Major | bug | `src/shared/video/frame_provider_service.py:1472-1477（对照 1274-1294）` | medium | request_gpu_frame 的 CPU 回退不转发 allow_master_fallback，播放热路径可被同步母版解码拖死 |
| AUD-39105 | Major | bug | `src/shared/video/frame_provider_service.py:1485-1514（对照 1968-1973）` | medium | 导出路径 request_metal_frame 优先使用 VT 源，违反同文件"export 恒走 CPU 全质量"策略，ProRes 色度下采样可进导出 |
| AUD-39106 | Major | bug | `src/shared/video/frame_provider_service.py:1002-1060、1086-1111` | medium | update_disk_proxy_info/_create_memory_proxy_source 对可能已被关闭的会话赋新源，新源永不释放 |
| AUD-39107 | Major | perf-opportunity | `src/shared/video/frame_provider_service.py:2231-2275（对照 1005-1017、1842-1872、1022-1024）` | medium | _on_proxy_complete 持有 self._lock 执行媒体源打开（阻塞 I/O），且跳过磁盘代理校验、不关闭被替换的内存代理 |
| AUD-39108 | Suggestion | risk | `src/shared/video/frame_provider_service.py:832、1232、1330-1331、1357、1379-1381` | high | 全局统计计数无锁递增，并发下丢失更新 |
| AUD-39109 | Suggestion | process | `src/shared/video/frame_provider_service.py:1397、1434、1499` | high | has_vt_source/request_gpu_frame/request_metal_frame 未持 self._lock 访问 _clips，且不检查 is_open |
| AUD-39110 | Suggestion | process | `src/shared/video/frame_provider_service.py:2107-2110` | medium | _decode_frame 将越界帧号静默钳制，调用方映射错误被掩盖 |
| AUD-39111 | Suggestion | doc-gap | `src/shared/video/frame_provider_service.py:1569-1636` | medium | prefetch 实为同步执行且范围期间缓存无界增长，docstring 与行为不符 |
| AUD-39112 | Suggestion | process | `src/shared/video/frame_provider_service.py:1400-1410、1485-1490、1642-1652` | medium | 类型注解缺失/不准确（request_gpu_frame、request_metal_frame、request_frame_window） |
| AUD-39200 | Major | risk | `src/shared/video/frame_queue.py:445-468（路由依据 344-348）` | medium | master 队列容量上限永不可能生效，可无界增长 |
| AUD-39201 | Major | risk | `src/shared/video/frame_queue.py:309-316（同类调用另见 249-250、340-341、373-374、392-393、451-452、651-652、773-781、795-800）` | medium | 在任意调用线程同步触发 GPU 帧 release 回调，违反渲染线程释放契约 |
| AUD-39202 | Major | bug | `src/shared/video/frame_queue.py:569-573（peek）、589-595（get_latest）、711-714（get_latest_before_or_at）` | medium | peek/get_latest/get_latest_before_or_at 返回仍在队列中的 entry 引用，无所有权保障 |
| AUD-39203 | Suggestion | process | `src/shared/video/frame_queue.py:135-142` | high | QueueEntry.is_stale 恒返回 False，是误导性死代码 |
| AUD-39204 | Suggestion | perf-opportunity | `src/shared/video/frame_queue.py:306-316、647-652` | high | 过期帧/更旧帧清除使用 list.remove，O(n²) 复杂度 |
| AUD-39205 | Suggestion | process | `src/shared/video/frame_request.py:135-140` | medium | priority 与 reason 无一致性校验，可构造自相矛盾的请求 |
| AUD-39206 | Suggestion | process | `src/shared/video/frame_trace_schema.py:98-99、143-148` | medium | segment_budgets_ms 返回裸 dict 注解，priority/backend 为自由字符串未对齐词表 |
| AUD-39207 | Major | bug | `src/shared/video/performance_budgets.py:159-169` | medium | 多个 media 文件匹配同一预算 glob 时只裁决第一个，其余文件逃过门禁 |
| AUD-39208 | Suggestion | process | `src/shared/video/performance_budgets.py:94-114` | high | iter_budgets 未校验 soft_max_ms <= hard_max_ms |
| AUD-39209 | Major | perf-opportunity | `src/shared/video/monitor_output.py:643-649（读取方缺失于整个 _run 循环 656-666）` | medium | ffmpeg 子进程 stderr=PIPE 从不读取，stderr 写满管道缓冲后监控输出冻结 |
| AUD-39210 | Major | risk | `src/shared/video/monitor_output.py:561-572（` | medium | start() 在调用线程同步执行最长约 10-15 秒的 ffmpeg 子进程探测，阻塞 UI 线程 |
| AUD-39211 | Major | bug | `src/shared/video/monitor_output.py:439-452` | medium | YUV→BGR 转换硬编码 BT.601 全量程系数，HD 素材外部监看颜色偏移 |
| AUD-39212 | Suggestion | process | `src/shared/video/monitor_output.py:231-238、299-302、365-368、402-409、453-454` | high | 多处宽泛 except 静默吞掉转换错误，无任何日志 |
| AUD-39213 | Suggestion | process | `src/shared/video/monitor_output.py:83-94` | high | DeckLinkMonitorConfig.from_config 对非数值配置值直接抛未捕获 ValueError |
| AUD-39214 | Suggestion | risk | `src/shared/video/monitor_output.py:892-897（提交线程）与 933-937（Native 提交线程）` | high | _dropped_frames 跨线程非原子自增，计数可能丢失 |
| AUD-39215 | Suggestion | process | `src/shared/video/monitor_output.py:822-827` | medium | Native 后端 start() 失败时已创建的 output 未调用 stop() 清理 |
| AUD-39216 | Suggestion | process | `src/shared/video/monitor_output.py:294-302` | high | ndarray 路径无 cv2 且需缩放时直接返回黑帧，与 QImage 路径回退不对称 |
| AUD-39300 | Suggestion | process | `src/shared/video/pipeline_decision.py:513-517` | high | fallback_to_v2_or_fail 的 fatal+allow_fallback 分支消息仍写 "Fallback to v2 allowed"，与 should_fallback 恒为 False 矛盾 |
| AUD-39301 | Suggestion | process | `src/shared/video/pipeline_decision.py:394-401, 162-167` | medium | resolve_video_pipeline_mode 在 AUTO+fatal 分支返回 reason="fallback"、fallback_reason=force_reason，__str__ 会输出 "(fallback from |
| AUD-39302 | Suggestion | process | `src/shared/video/pipeline_decision.py:291-295` | high | should_allow_fallback_for_reason 在 AUTO+致命+allow_fallback 时返回 True（暗示可回退 v2），与 v2 移除后的现实矛盾；且生产代码无调用者 |
| AUD-39303 | Suggestion | process | `src/shared/video/pipeline_decision.py:85-86` | high | 未使用导入 dataclasses.field 与 enum.auto |
| AUD-39304 | Suggestion | doc-gap | `src/shared/video/pixel_copy_counters.py:58-68` | high | add_pixel_copy 对负数字节静默忽略，与 docstring "负数/未知 kind 被拒绝" 不符（未知 kind 抛 ValueError，负数直接 return） |
| AUD-39305 | Critical | bug | `src/shared/video/playback_clock.py:365` | medium | 使用 os.path.isfile 但未 import os——磁盘代理 READY 路径抛 NameError，导致 SourceManager.open() 整体失败 |
| AUD-39306 | Major | bug | `src/shared/video/playback_clock.py:479, 686-702, 719（对照 611, 741-742）` | medium | 共享源（无需代理）时 master/proxy 双游标各自记账，顺序播放免 seek 优化会读到并缓存错误的 proxy 帧 |
| AUD-39307 | Major | bug | `src/shared/video/playback_clock.py:1364, 1404（对照 1021-1023, 1040）` | medium | FrameScheduler 未适配 V-P0-09"未知时长"：total_frames=0 时预读被整体禁用 |
| AUD-39308 | Suggestion | process | `src/shared/video/playback_clock.py:1266, 1277（对照 tick 的锚点公式 :1185-1189）` | high | should_drop_frame/get_lag 仍用旧的全量 `clock_time × speed` 公式，与 V-P0-02 速率锚点公式不一致 |
| AUD-39309 | Suggestion | process | `src/shared/video/playback_clock.py:748-768（对照 :673` | medium | seek_proxy 只 seek `_proxy_source`，不处理磁盘代理激活（`_proxy_source is None`）的场景；且全库无调用者 |
| AUD-39310 | Suggestion | process | `src/shared/video/playback_clock.py:714-715` | high | `hasattr(frame, 'proxy_resolution')` 恒为 False——DecodedFrame 无该字段，磁盘代理分辨率标注永不生效（死代码） |
| AUD-39311 | Suggestion | risk | `src/shared/video/playback_clock.py:1025, 1082, 1130, 1255` | medium | fps=0 时 start()/seek_to_frame()/get_time_for_frame() 除零崩溃（构造器只保护了 _frame_duration） |
| AUD-39312 | Suggestion | process | `src/shared/video/playback_clock.py:1170-1178` | medium | tick(dt) 显式传入 dt 时仍写 `last_frame_time = time.time()`，dt 与 wall-clock 时间源混用 |
| AUD-39313 | Suggestion | process | `src/shared/video/playback_clock.py:1195-1202` | high | tick() 的 lag<=0 分支内 `if ideal_frame > self._state.current_frame` 恒为假（死代码） |
| AUD-39314 | Suggestion | process | `src/shared/video/playback_clock.py:63, 82` | high | 未使用导入 numpy（np）与 CachePolicy |
| AUD-39315 | Major | bug | `src/shared/video/playback_frame_trace.py:159` | medium | is_full_gpu 的 decode 白名单与全库实际产出的 decode_path 词表不符，GPU/硬解帧被误判为非全 GPU |
| AUD-39316 | Suggestion | process | `src/shared/video/playback_frame_trace.py:334-350（对照非空分支 :383-410）` | medium | get_summary 空会话返回的键形状与非空分支不一致，但注释声称"键形状一致（消费方无需判空）" |
| AUD-39317 | Suggestion | risk | `src/shared/video/playback_frame_trace.py:541-549` | high | export_json 中 get_summary 与 get_traces 是两次独立加锁调用，summary 与 traces 可能不是同一快照 |
| AUD-39400 | Major | risk | `src/shared/video/playback_state.py:403-423（load）、459-464（unload）、490-495（start_playback）` | medium | 状态回调在持有 `_session_lock` 时同步触发，Qt 直连槽一旦重入协调器属性即自死锁 |
| AUD-39401 | Major | bug | `src/shared/video/playback_state.py:288-294` | medium | 加载失败后 `is_loaded` 仍返回 True（ERROR 状态未被排除） |
| AUD-39402 | Suggestion | perf-opportunity | `src/shared/video/playback_state.py:604-638（blocking seek →` | high | `_session_lock` 在阻塞操作全程被持有（300ms 轮询 / 最长 2s） |
| AUD-39403 | Suggestion | risk | `src/shared/video/playback_state.py:258-260、937-945（set_tick_callback）、1003-1013（cleanup）、919-926（set_on_frame_ready，` | medium | `_tick_callback`/`_tick_lock` 为死代码；`cleanup()` 不清空 tick 回调且回调字段写入不持锁 |
| AUD-39404 | Suggestion | process | `src/shared/video/playback_state.py:59（` | high | 未使用导入与弱类型注解；`CoordinatorStats` 无锁更新 |
| AUD-39405 | Major | bug | `src/shared/video/session.py:1189-1238（重点 1223-1225），对照 1085-1092` | medium | `get_frame_at(is_master=True)` 缺 PLAYING 态同步解码防护且共享帧服务模式下无 fallback（与 `get_master_frame_blocking` 不对称） |
| AUD-39406 | Major | bug | `src/shared/video/session.py:728-824（756-758 仅 clamp 帧号，824 无条件` | medium | `seek_to_frame` 无状态校验且恒返回 True（UNLOADED/ERROR/LOADING 下皆"成功"） |
| AUD-39407 | Suggestion | process | `src/shared/video/session.py:408-416（锁内仅置 LOADING）、417-533（锁外执行 open/queue/worker 创建）、544-580（unload 对 LOADING 态不设 STOPPING 直接清理）` | medium | `load()` 两段式状态切换非原子，与并发 `unload()` 存在中间态窗口 |
| AUD-39408 | Suggestion | doc-gap | `src/shared/video/session.py:1336-1342，对照调用点 416、526、541、577、643、682、710、822（均为 V-P0-11 锁外调用）` | high | `_notify_state_change` docstring 与全部调用点矛盾（"Must be called while _state_lock is held"） |
| AUD-39409 | Suggestion | process | `src/shared/video/session.py:566-580` | high | `unload()` 忽略 `stop_workers` 返回值，恒返回 True |
| AUD-39410 | Suggestion | process | `src/shared/video/session.py:1494-1498` | high | `_get_performance_monitor` 宽泛 `except Exception` 静默吞错，无任何日志 |
| AUD-39411 | Suggestion | process | `src/shared/video/session.py:1381-1401、1485` | high | `get_stats` 返回类型注解与实际结构不符；`_get_performance_monitor` 缺返回注解 |
| AUD-39412 | Suggestion | doc-gap | `src/shared/video/session.py:404-407、421-424（"默认使用 ColorFrameProviderAdapter"/"共享帧服务模式（默认）"），对照 231-252（"V-P1-4: 协议测试全绿前默认禁用"、` | high | `load()` 文档/注释与配置默认值矛盾（"默认共享帧服务" vs 实际默认 False） |
| AUD-39500 | Critical | risk | `src/shared/video/video_async_renderer.py:277` | medium | 预分配转换缓冲跨线程别名：队列中待上传帧的引用会被生产者线程复用改写 |
| AUD-39501 | Critical | bug | `src/shared/video/video_async_renderer.py:817` | medium | YUV 渲染路径失效：UPLOAD_YUV_FRAME 从不设置 current_frame_tex，RENDER_FRAME 渲染的是过期 RGB 帧 |
| AUD-39502 | Major | bug | `src/shared/video/video_async_renderer.py:642` | medium | 非 float32 图像原样写入 f2 纹理：字节被当作 float16 解释，画面错乱 |
| AUD-39503 | Major | bug | `src/shared/video/video_async_renderer.py:443` | medium | HDR float 帧按逐帧最大值归一化：亮度逐帧重缩放，违反 HDR 不得无理由归一化的铁律 |
| AUD-39504 | Major | risk | `src/shared/video/video_async_renderer.py:660` | medium | 纹理池 acquire 失败的 fallback 每帧新建纹理且不释放上一张 fallback 纹理：GPU 内存泄漏 |
| AUD-39505 | Major | risk | `src/shared/video/video_async_renderer.py:151` | medium | 构造函数对主 GL 上下文做 doneCurrent/makeCurrent 干预：在其他页面上下文活跃期构造曾致崩溃（仓库内有规避注释佐证） |
| AUD-39506 | Major | risk | `src/shared/video/video_async_renderer.py:246` | medium | 线程退出不做 doneCurrent，moderngl ctx 不 release 仅置 None：GL 对象可能在错误线程被销毁 |
| AUD-39507 | Major | bug | `src/shared/video/video_async_renderer.py:173` | medium | GC 后备释放机制是空操作：_gc_resources 列表从未被填充 |
| AUD-39508 | Major | risk | `src/shared/video/video_async_renderer.py:849` | medium | frame_ready 发出的纹理 glo 在视口变化重建 FBO 时被释放：跨线程纹理 ID 生命周期竞态 |
| AUD-39509 | Major | bug | `src/shared/video/video_async_renderer.py:590` | medium | 着色器加载失败被静默吞掉：清屏后的空白帧仍按成功 emit frame_ready |
| AUD-39510 | Major | bug | `src/shared/video/video_async_renderer.py:242` | high | 命令循环宽泛 except 吞掉命令级异常：既不上报也不重试，消费方对丢帧无感知 |
| AUD-39511 | Major | perf-opportunity | `src/shared/video/video_async_renderer.py:904` | high | _render_quad 每帧创建并销毁 VBO/IBO/VAO：与纹理池化（P4.1）理念相悖的每帧 GPU 对象churn |
| AUD-39512 | Suggestion | perf-opportunity | `src/shared/video/video_async_renderer.py:849` | high | 每帧 ctx.finish() 强制全流水线同步，可用 fence sync 替代 |
| AUD-39513 | Suggestion | doc-gap | `src/shared/video/video_async_renderer.py:525` | high | _get_texture_format 全部分支返回 'f2'：死分支且 float32 HDR 被无条件降为 float16 而文档未言明 |
| AUD-39514 | Suggestion | risk | `src/shared/video/video_async_renderer.py:220` | medium | 命令合并直接 peek queue.Queue 内部 deque，越过 Queue 的锁协议 |
| AUD-39515 | Suggestion | risk | `src/shared/video/video_async_renderer.py:331` | medium | upload_yuv_and_render_frame 在调用线程无锁读 current_frame_index（渲染线程并发写） |
| AUD-39516 | Suggestion | process | `src/shared/video/video_async_renderer.py:250` | medium | stop 后 submit 触发僵尸重启；quit() 对自定义 run() 无效 |
| AUD-39517 | Suggestion | process | `src/shared/video/video_async_renderer.py:88` | medium | 多个核心方法缺少参数/返回类型注解 |
| AUD-39518 | Suggestion | process | `src/shared/video/video_async_renderer.py:505` | medium | 预分配缓冲在 RGB uint16 路径被整块替换；UPLOAD_FRAME 的 frame_size 参数被忽略 |
| AUD-39600 | Critical | bug | `src/shared/video/video_playback.py:3499` | medium | 高位深 BGR packed 帧转 uint8 缺少 *255 缩放，16-bit BGR 帧显示为全黑 |
| AUD-39601 | Major | bug | `src/shared/video/video_playback.py:2254, 2278, 2291, 2331, 2345-2346, 2228` | medium | 基类多个公共方法引用从未在基类初始化的 `self._playback_clock`（及 `_proxy_prefetch_worker`），直接实例化基类即 AttributeError |
| AUD-39602 | Major | bug | `src/shared/video/video_playback.py:2294` | medium | `set_playback_speed` 未验证 speed：传 0 触发 ZeroDivisionError，负值导致 8ms 高频定时器 |
| AUD-39603 | Major | risk | `src/shared/video/video_playback.py:2859-2864` | medium | `_init_dual_path_components` 异常路径丢弃已 open() 的 SourceManager 而不 close()，泄漏解码器与文件句柄 |
| AUD-39604 | Major | bug | `src/shared/video/video_playback.py:3550-3559` | medium | Enhanced.unload_video 中 `coordinator.unload()`/`source_manager.close()` 无异常保护，一旦抛出将跳过后续纹理清空与标志复位 |
| AUD-39605 | Major | bug | `src/shared/video/video_playback.py:2688-2690, 4213-4215, 4430-4432, 3430-3431, 4277-4282` | medium | 多处宽泛 `except Exception` + pass/仅 debug 日志，掩盖领域错误（违反铁律） |
| AUD-39606 | Major | risk | `src/shared/video/video_playback.py:3579-3581（unload_video）` | medium | UI 线程置空 `renderer.current_video_texture/current_video_planes` 可能成为 ModernGL 纹理的最后引用，使释放发生在 UI 线程 |
| AUD-39607 | Suggestion | process | `src/shared/video/video_playback.py:63-64 vs 117-118；121-131 vs 134-146` | medium | 模块级重复导入：`Enum`/`dataclass` 各导入两次，`VideoPipelineFallbackReason` 从两个模块各导入一次 |
| AUD-39608 | Suggestion | process | `src/shared/video/video_playback.py:3021-3026` | medium | `load_video` 尾部存在不可达代码 |
| AUD-39609 | Suggestion | process | `src/shared/video/video_playback.py:3214-3220, 3148, 211-215` | medium | `_show_first_frame_via_coordinator` 恒假死分支；`FRAME_PATH_DEGRADED` 最终状态不可达但被 `is_displayable` 计为可显示 |
| AUD-39610 | Suggestion | risk | `src/shared/video/video_playback.py:601-649, 575-580, 619-628` | medium | `ProxyPrefetchWorker` 多数互斥锁的 lock/unlock 无 try/finally，锁内抛异常将永久锁死队列 |
| AUD-39611 | Suggestion | process | `src/shared/video/video_playback.py:2786-2834（L2797-2799 与 L2822-2834）` | medium | 已加载视频时 `enable_dual_path` 会另建并打开第二个 SourceManager，与 coordinator 的重复 |
| AUD-39612 | Suggestion | process | `src/shared/video/video_playback.py:718, 2403, 1277, 1293, 1032, 1017, 2095, 2104, 2258-2281 等` | medium | 类型注解缺失：`engine` 参数无类型、多个方法缺返回注解 |
| AUD-39613 | Suggestion | process | `src/shared/video/video_playback.py:3883, 2040-2042` | medium | 每次平面帧转换新建 ColorConverter；活跃图层循环内重复赋值不 break |
| AUD-39700 | Major | bug | `src/shared/video/video_texture_pool.py:194-226` | medium | 池满换尺寸时新建纹理失败会留下「已释放纹理 + 旧元数据」的损坏槽位，后续 acquire 将返回已释放纹理 |
| AUD-39701 | Major | risk | `src/shared/video/video_texture_pool.py:99-103, 361-378` | medium | weakref.finalize 后备释放在任意触发 GC 的线程上直接调用 GL release，违反「GL 仅渲染线程」铁律 |
| AUD-39702 | Suggestion | process | `src/shared/video/video_texture_pool.py:317-323` | high | resize_pool 从尾部 pop 释放的是「最新」槽位且不检查 is_available，与 LRU 语义相悖 |
| AUD-39703 | Suggestion | process | `src/shared/video/video_texture_pool.py:46, 95` | high | 类型注解与死代码：texture 默认 None 未标 Optional；_current_size 赋值后从未使用 |
| AUD-39710 | Major | bug | `src/shared/video/video_upload.py:355-360, 380-382` | medium | upload_planes 8-bit 路径向 f1(R8) 纹理写入 float32 数据，字节数与像素格式不匹配，8-bit planar 上传结果必然错误 |
| AUD-39711 | Suggestion | process | `src/shared/video/video_upload.py:335-390` | medium | upload_planes 每次调用都创建并释放 3 张纹理，无池化，与项目视频纹理池设计初衷相悖 |
| AUD-39712 | Major | bug | `src/shared/video/video_upload.py:410-414, 436-438` | medium | create_yuv_program 用 CWD 相对路径加载外部 shader，启动目录不同时静默降级到内置 shader |
| AUD-39713 | Suggestion | process | `src/shared/video/video_upload.py:277-296` | high | 类常量 BT709/BT2020/BT601_MATRIX 按行向量存放，与实际转换（R=1.0*y+1.5748*v）不一致，作为矩阵使用会得到错误颜色 |
| AUD-39714 | Suggestion | process | `src/shared/video/video_upload.py:786` | high | create_yuv_frame_from_planes 位深探测硬编码 '10le'，12/16-bit 格式误标 bit_depth=8 |
| AUD-39715 | Suggestion | risk | `src/shared/video/video_upload.py:379-395` | medium | upload_planes 中途异常时已创建纹理未登记进 _textures，异常路径泄漏 |
| AUD-39716 | Suggestion | process | `src/shared/video/video_upload.py:496-499` | medium | 内置 shader limited-range 常数按 8-bit 标度硬编码，10/12-bit 归一化输入存在约 0.3% 系统性色度偏差 |
| AUD-39720 | Major | risk | `src/shared/video/yuv_gpu_self_test.py:171-194, 249-272` | medium | run_yuv_gpu_self_test 外层异常路径不清理 output_tex/fbo，GPU 资源泄漏 |
| AUD-39721 | Suggestion | process | `src/shared/video/yuv_gpu_self_test.py:179-184` | medium | 自测永久修改 ctx.viewport（4x4）与 uploader 的矩阵/范围状态，不保存恢复 |
| AUD-39722 | Suggestion | process | `src/shared/video/yuv_gpu_self_test.py:130-133, 289-292, 309, 323` | medium | run_r16ui_gpu_self_test 内 import moderngl 未使用；两个入口函数参数缺类型注解；getattr 访问私有属性 |
| AUD-39800 | Critical | bug | `src/shared/video/yuv_gpu_upload.py:1998（副本: 2147、2320、2485）` | medium | BT.601 矩阵系数误用 PAL 模拟 YUV 值，SD 内容颜色系统性错误 |
| AUD-39801 | Major | risk | `src/shared/video/yuv_gpu_upload.py:1610` | medium | `_upload_planar_r8` 回退丢失 subsampling，R8 池不可用时 8-bit 422/444 崩溃 |
| AUD-39802 | Major | bug | `src/shared/video/yuv_gpu_upload.py:2980` | medium | `run_r16ui_self_test` 覆盖共享的 yuv_matrix/color_range 且不恢复，首帧颜色错误 |
| AUD-39803 | Major | risk | `src/shared/video/yuv_gpu_upload.py:3073` | medium | `run_r16ui_self_test` 异常路径泄漏 FBO 与输出纹理 |
| AUD-39804 | Major | bug | `src/shared/video/yuv_gpu_upload.py:324` | medium | 纹理池在容量未满时优先抢占 busy 槽位，稳态下每 (plane_type,尺寸) 仅 1 张纹理 |
| AUD-39805 | Major | bug | `src/shared/video/yuv_gpu_upload.py:2615（同型: 2793）` | medium | `_last_upload_mode` 全局单值决定渲染 shader 分发，混合位深多轨道时采样器类型不匹配 |
| AUD-39806 | Major | risk | `src/shared/video/yuv_gpu_upload.py:2834` | medium | `render_nv12_to_rgb` 每帧创建/销毁临时 VBO/IBO/VAO，且异常路径泄漏 |
| AUD-39807 | Suggestion | process | `src/shared/video/yuv_gpu_upload.py:929` | medium | 输入验证依赖 assert（-O 下被剥离），且部分路径无校验 |
| AUD-39808 | Suggestion | process | `src/shared/video/yuv_gpu_upload.py:84` | high | 类型注解不精确（None 默认值非 Optional、裸 tuple、返回类型含 str 却标注 float） |
| AUD-39809 | Suggestion | process | `src/shared/video/yuv_gpu_upload.py:683` | high | 死代码与未使用导入 |
| AUD-39810 | Suggestion | risk | `src/shared/video/yuv_gpu_upload.py:2575（同类: 2255、2413）` | medium | 三处局部 IBO 未跟踪，release() 不释放（GL 对象泄漏） |
| AUD-39811 | Suggestion | risk | `src/shared/video/yuv_gpu_upload.py:781` | medium | `enable_pbo` 含 GL 对象创建/销毁却无线程契约守卫，与并发上传存在竞态 |
| AUD-39812 | Suggestion | process | `src/shared/video/yuv_gpu_upload.py:1496` | high | `_upload_alpha_plane` 宽泛 except 吞掉领域错误 |
| AUD-39813 | Suggestion | process | `src/shared/video/yuv_gpu_upload.py:2645` | high | `render_yuv_to_rgb` float 路径硬编码 source_uv (1,1)/(0,0)，与 r16ui 路径 API 不对称 |
| AUD-40001 | Major | risk | `src/app/ui/project_manager_dialog.py:268, 745-746, 626, 639-643` | medium | 搜索/排序每次变更都在 UI 线程逐项目同步解压 .topos 封面，抵消 [R0-7] 异步扫描优化 |
| AUD-40002 | Major | risk | `src/app/ui/project_manager_dialog.py:577-578, 593` | medium | `_apply_filter` 无条件信任 `ProjectMeta.name` 为 str，畸形 project.json 可致启动屏列表构建崩溃 |
| AUD-40003 | Suggestion | process | `src/app/ui/project_manager_dialog.py:1047-1066` | medium | 删除项目后清理 recent/favorites/tags 用裸 `except Exception: pass` 静默吞掉持久化错误 |
| AUD-40004 | Suggestion | process | `src/app/ui/project_manager_dialog.py:536-540, 1048, 1054` | medium | `favorite_projects`/`recent_projects` 配置值未做类型校验，字符串值会被按字符迭代并回写垃圾数据 |
| AUD-40005 | Suggestion | process | `src/app/ui/project_manager_dialog.py:308-311, 481-485` | high | `last_project` 读取两处裸 `except Exception: pass`，无日志 |
| AUD-40006 | Suggestion | process | `src/app/ui/project_manager_dialog.py:89-91, 597-600` | high | 库扫描失败在 UI 上呈现为"暂无项目"，无任何错误反馈 |
| AUD-40007 | Suggestion | process | `src/app/ui/project_manager_dialog.py:692-696, 719-721` | high | 死代码：`_elide_name` 与 `_mode_tag` 无任何调用且 `_mode_tag` 忽略参数恒返回 "Video" |
| AUD-40008 | Suggestion | process | `src/app/ui/project_manager_dialog.py:826, 865-868` | high | 多选状态下右键菜单 "Open" 静默无效 |
| AUD-40009 | Suggestion | process | `src/app/ui/project_manager_dialog.py:1013` | high | 计数文案 `f"{count} projects"` 绕过 I18N |
| AUD-40010 | Suggestion | process | `src/app/ui/project_manager_dialog.py:178 (` | high | 多个方法/参数缺类型注解 |
| AUD-40011 | Suggestion | process | `src/app/ui/resize_dialog.py:54-66` | high | 宽高联动用 `int()` 截断，往返修改产生 1px 漂移 |
| AUD-40012 | Suggestion | process | `src/app/ui/resize_dialog.py:7, 54, 61, 68` | high | 方法缺类型注解（`__init__` 参数、`on_width_changed`、`get_dimensions`） |
| AUD-40013 | Major | bug | `src/app/ui/script_panel.py:884-887` | medium | 内置示例目录指向不存在的 `src/app/scripting/examples`，Scripts 菜单 "Examples" 永远为空（实测复现） |
| AUD-40014 | Major | bug | `src/app/ui/script_panel.py:871-881` | medium | `_get_project_root` 少一级 `..`，用户 Scripts 目录错误落在源码树 `src/Scripts` 而非项目根（实测复现） |
| AUD-40015 | Major | risk | `src/app/ui/script_panel.py:1296-1301（配合 1237-1240, 1252, 1270）` | medium | Scripts 菜单每次打开都新建 QMenu 且旧菜单不销毁，无限累积泄漏（实测复现） |
| AUD-40016 | Suggestion | process | `src/app/ui/script_panel.py:1095-1096, 1296-1301` | medium | 菜单"每次打开前重建"实际展示的是上一代内容，新保存脚本需开两次才可见 |
| AUD-40017 | Suggestion | process | `src/app/ui/script_panel.py:10, 17, 23` | high | 未使用的导入：`glob as glob_mod`、`QToolBar`、`QSizePolicy`、`QBrush` |
| AUD-40018 | Suggestion | process | `src/app/ui/script_panel.py:1059, 1067, 1104` | high | 工具栏提示承诺的快捷键未实现：F5 运行、Ctrl+N 新建 |
| AUD-40019 | Suggestion | process | `src/app/ui/script_panel.py:1059, 1067, 1075, 1091-1096, 1113, 1189, 1318, 1482-1489, 1504-1505, 1518-1523 等` | high | 大量用户可见字符串绕过 I18N 硬编码英文 |
| AUD-40020 | Suggestion | process | `src/app/ui/script_panel.py:1462, 1466, 1469` | high | `logger.debug(f"...")` 无条件做字符串格式化 |
| AUD-40021 | Suggestion | process | `src/app/ui/script_panel.py:1237, 1335, 1364, 1381-1385, 1517 等` | high | 类型注解缺失（`_build_scripts_menu`、`_do_search`、`_do_replace`、`_on_save`、`_add_recent_file` 等大量槽与辅助方法） |
| AUD-4100 | Major | bug | `src/app/ui/shortcut_dialog.py:264-271` | medium | Cancel 按钮无法回退：所有修改经 ShortcutManager 即时写入 config.json 并生效于 QAction |
| AUD-4101 | Major | risk | `src/app/ui/shortcut_dialog.py:101-104` | medium | 未设置 WA_DeleteOnClose，父窗口存活期间每次打开累积一份完整对话框 widget 树 |
| AUD-4102 | Suggestion | process | `src/app/ui/shortcut_dialog.py:655` | high | 直接读取 ShortcutManager 私有成员 `_custom_shortcuts` 判断自定义状态 |
| AUD-4103 | Suggestion | risk | `src/app/ui/shortcut_dialog.py:643,689` | high | `_updating_table` 状态锁只写不读，是无效的死标志 |
| AUD-4104 | Suggestion | process | `src/app/ui/shortcut_dialog.py:630-635` | high | 启动时 `_on_category_changed` 被触发两次，初始表格填充两遍 |
| AUD-4105 | Suggestion | process | `src/app/ui/shortcut_dialog.py:668-669` | medium | 表格刷新为 O(行数×注册表) 的逐行冲突扫描，且搜索每键入一个字符就整表重建 |
| AUD-4106 | Suggestion | process | `src/app/ui/shortcut_dialog.py:283,310` | high | Export/Import 使用 `ShortcutManager.instance()` 单例而非构造注入的 `self._manager` |
| AUD-4107 | Suggestion | process | `src/app/ui/shortcut_dialog.py:111-113` | high | 构造函数内新建 ConfigManager 实例仅为读取一个布尔配置 |
| AUD-4108 | Suggestion | process | `src/app/ui/shortcut_dialog.py:342` | high | 导入 preset 触发逐条 `save_config()` 同步磁盘写，大 preset 卡 UI |
| AUD-4109 | Suggestion | process | `src/app/ui/shortcut_dialog.py:37,41,94,101-102,856` | medium | 多处参数/返回值类型注解缺失或不精确 |
| AUD-4110 | Major | risk | `src/app/ui/welcome_dialog.py:192,209-212` | medium | `recent_files`/`recent_projects` 列表元素未做类型验证，非字符串条目可致欢迎页构造崩溃 |
| AUD-4111 | Suggestion | process | `src/app/ui/welcome_dialog.py:31-40` | medium | `_ProjectClassifyWorker.run()` 无异常兜底，任何异常都会使 `done` 不再发射、"Loading projects…" 占位行永久停留 |
| AUD-4112 | Suggestion | risk | `src/app/ui/welcome_dialog.py:200-201,238-239,444-447` | medium | 三处宽泛 `except Exception` 静默吞错，掩盖配置/保存阶段错误 |
| AUD-4113 | Suggestion | process | `src/app/ui/welcome_dialog.py:214-218` | high | `item.setForeground(item.foreground())` 是无效赋值，缺失文件行未按预期置灰 |
| AUD-4114 | Suggestion | process | `src/app/ui/welcome_dialog.py:25,57,60` | high | 类型注解缺失或不精确 |
| AUD-42001 | Suggestion | process | `src/app/update_checker.py:64-68` | medium | 环境变量 TOPOS_TEST_MODE 在生产环境会静默禁用更新检查并伪造 no_update |
| AUD-42002 | Suggestion | perf-opportunity | `src/app/update_checker.py:15, 18-31` | medium | 版本兜底字面量 "3.0.0" 依赖人工同步，打包/冻结环境下可能以错误本地版本比较 |
| AUD-42003 | Suggestion | process | `src/app/update_checker.py:40-45` | high | parse_version 只取前三段数字，忽略预发布与四段版本号 |
| AUD-42004 | Suggestion | doc-gap | `src/app/update_checker.py:36-37, 54` | high | 注释/文档写 GitHub，实际端点为 Gitee |
| AUD-4400 | Major | bug | `src/features/composite/adapters/main_window_adapter.py:650` | medium | restore_graph 对外部工程数据的反序列化全程无异常防护，损坏数据会中断整个工程打开流程 |
| AUD-4401 | Major | perf-opportunity | `src/features/composite/adapters/main_window_adapter.py:586（入口 538）` | medium | Media Pool 海报发布在每帧异步回读路径上触发全图序列化 get_composite_data() |
| AUD-4402 | Suggestion | process | `src/features/composite/adapters/main_window_adapter.py:590-598` | medium | 海报 QImage 构造未校验 data 形状与传入 width/height 一致性 |
| AUD-4403 | Suggestion | process | `src/features/composite/adapters/main_window_adapter.py:681-683` | medium | _absolutize_paths 原地修改传入的工程数据字典，产生调用方不可见的副作用 |
| AUD-4404 | Suggestion | process | `src/features/composite/adapters/main_window_adapter.py:300-303, 559-562, 600-601, 710-713` | medium | 多处宽泛 except Exception 且以 debug/warning 级日志吞掉，违反"不得宽泛 except 掩盖领域错误" |
| AUD-4405 | Suggestion | process | `src/features/composite/adapters/main_window_adapter.py:389-394` | medium | handle_frame_ready 在页面守卫之前无条件按帧打 INFO 日志 |
| AUD-4406 | Suggestion | process | `src/features/composite/adapters/main_window_adapter.py:63-64, 96-100, 271-272, 650-663` | medium | 适配器深度访问对端私有成员，契约脆弱 |
| AUD-4407 | Suggestion | process | `src/features/composite/adapters/main_window_adapter.py:72-75, 109` | medium | _wire_signals 的一次性 _connected 标志与 runtime 重建不匹配，重建后信号失联 |
| AUD-4408 | Suggestion | process | `src/features/composite/adapters/main_window_adapter.py:43, 132, 255-259, 545` | medium | 类型注解缺失/过宽 |
| AUD-4409 | Suggestion | process | `src/features/composite/adapters/caption_input.py:29-31` | medium | as_node_inputs 对 word_timings/style 仅做浅拷贝，嵌套可变对象仍与源共享 |
| AUD-4500 | Major | bug | `src/features/composite/ai/composite_plan.py:115（另见 49-50、148-162）` | medium | frozen 数据类持有外部可变 Mapping，plan_hash/to_dict 可在构造后被篡改 |
| AUD-4501 | Major | doc-gap | `src/features/composite/ai/composite_plan.py:296（另见 310-316、285-289）` | medium | validate_composite_plan 校验缺口：空 node_type / 空 node_id 不拦截，docstring 声称的检查未实现 |
| AUD-4502 | Suggestion | process | `src/features/composite/ai/composite_plan.py:324-348（对照 95-96、101）` | high | __all__ 漏导出 NODE_OP_DUPLICATE_NODE / NODE_OP_SET_OUTPUT_TARGET |
| AUD-4503 | Suggestion | process | `src/features/composite/ai/composite_plan.py:296、310` | high | validate_composite_plan 用字符串字面量而非 NODE_OP_* 常量比对 kind |
| AUD-4504 | Suggestion | process | `src/features/composite/ai/composite_plan.py:282、292、296` | medium | available_node_types 为空时 fail-open，且 checks_run 仍宣称 node_type_exists 已运行 |
| AUD-4505 | Suggestion | process | `src/features/composite/ai/composite_plan.py:196-199、243-248（消费点 214-219、259-264）` | medium | severity / check 字符串未做枚举校验，拼错将使门禁静默放行 |
| AUD-4506 | Suggestion | process | `src/features/composite/ai/composite_plan.py:161` | high | plan_hash 的 json.dumps(default=str) 静默字符串化，可能产生哈希碰撞 |
| AUD-4600 | Major | bug | `src/features/composite/bake_service.py:164-172` | medium | 烘焙重接线静默失败：connect() 返回 False 而非抛异常，except 分支是死代码 |
| AUD-4601 | Major | bug | `src/features/composite/bake_service.py:251-267` | medium | Unbake 忽略 connect() 失败仍删除烘焙节点，可永久断开下游消费者 |
| AUD-4602 | Major | bug | `src/features/composite/bake_service.py:160-166` | medium | 重写只处理目标的第一个输出端口，多输出节点（keyer matte/shuffle 等）的次端口消费者烘焙后拿空结果 |
| AUD-4603 | Major | perf-opportunity | `src/features/composite/bake_service.py:223-231, 259-263` | medium | bake/unbake 往返会给从未改名的节点永久写入 user_label，冻结 i18n 翻译与实例编号语义 |
| AUD-4604 | Suggestion | process | `src/features/composite/bake_service.py:146-158` | medium | 向 ImageInputNode 注入 schema 外参数 cache_to_disk 并随工程序列化 |
| AUD-4605 | Suggestion | process | `src/features/composite/bake_service.py:90-95, 110-111；对照 69-87, 146-149` | medium | render_sequence 的 pad/ext 形参可与烘焙节点序列模式静默失配 |
| AUD-4606 | Suggestion | process | `src/features/composite/bake_service.py:152-158` | medium | set_parameter 的 except 回退分支为死代码，且会绕过脏标记追踪 |
| AUD-4607 | Suggestion | process | `src/features/composite/bake_service.py:90-96` | high | render_sequence 的 graph 形参从未使用 |
| AUD-4608 | Suggestion | process | `src/features/composite/bake_service.py:136, 164-175, 251-263` | medium | 图重写绕过 NodeGraph 的 RLock 直接读 connections、改节点状态 |
| AUD-4609 | Suggestion | process | `src/features/composite/bake_service.py:69-87, 148-149` | medium | plan_bake 不校验帧范围与 base_name，frame_start<=0 被静默钳制 |
| AUD-4610 | Suggestion | process | `src/features/composite/bake_service.py:45-46, 194-204` | medium | make_undo_action 不防护 before/after 为空 dict 的误用，可致整图清空 |
| AUD-4701 | Critical | bug | `src/features/composite/core/graph_migration.py:677-683（` | medium | migrate_v6_to_v7 借用 v9 的 `_SCENE3D_KEEP` 删参，v6 场景环境参数被静默丢弃，v8→v9 的全局设置迁移扑空 |
| AUD-4702 | Suggestion | risk | `src/features/composite/core/graph_migration.py:868-882（nodes 迭代同型问题：198、248、310、347、352、390、651、656、708、746；v10_to_v11:830 已正确防护）` | medium | migrate_graph 不校验版本字段与 nodes 类型：字符串版本崩溃、异常版本"未迁移即盖章"、nodes:null 崩溃 |
| AUD-4703 | Suggestion | process | `src/features/composite/core/graph_migration.py:420-423` | medium | v5→v6 提升 legacy 动画参数后一律重置为 0.0，而非按节点默认值恢复 |
| AUD-4704 | Suggestion | process | `src/features/composite/core/graph_migration.py:255-265（与 117-127` | medium | v2→v3 旧参数名重命名分支对 v1 工程不可达：v1→v2 已预先填入新名默认值 |
| AUD-4705 | Suggestion | process | `src/features/composite/core/graph_migration.py:873 与各迁移函数首行（196、243、308、338、383、650、705、742、807、828）` | medium | 迁移链每步全量 deepcopy，v1 工程升级最多 11 次整图深拷贝 |
| AUD-4706 | Suggestion | process | `src/features/composite/core/graph_migration.py:478-484` | high | `spawn_3d_scene_children` 参数与返回值缺类型注解 |
| AUD-4707 | Suggestion | perf-opportunity | `src/features/composite/core/asset_dependency.py:278-284（配合 619-620 的无条件 yield）` | medium | 依赖收集不考虑节点 disabled 状态，禁用节点的空路径会阻塞整个 collect |
| AUD-4708 | Suggestion | process | `src/features/composite/core/asset_dependency.py:374-389` | high | collect 回滚只还原参数，不清理已拷贝的目标文件 |
| AUD-4709 | Suggestion | process | `src/features/composite/core/eval_context.py:79-80（同类：92、102、107、119、125、132、141、147、150-157）` | high | 大量字段注解为 Any，注释中已写明精确类型却未落进签名 |
| AUD-4710 | Suggestion | process | `src/features/composite/core/format.py:153-159、294-302` | high | `from_dict` 构造器对反序列化数据无类型校验，非数值直接抛未捕获 ValueError |
| AUD-4711 | Suggestion | process | `src/features/composite/core/gl_bind.py:45-52` | medium | `glGetCurrentContext` 外围 `except Exception` 宽泛吞异常并静默跳过绑定 |
| AUD-48001 | Suggestion | process | `src/features/composite/core/graph_preflight.py:659-663` | medium | `_registered_port_schema` 用无日志的宽泛 `except Exception` 吞掉节点构造错误，端口契约校验被静默跳过 |
| AUD-48002 | Suggestion | risk | `src/features/composite/core/graph_preflight.py:810（及 791、1105）` | medium | 序列化 payload 的 isinstance 防护不一致：`_check_groups` 已诊断的非法映射仍会使 `_flatten_group_connections` / 边界环检测崩溃 |
| AUD-48003 | Suggestion | process | `src/features/composite/core/graph_preflight.py:455-471` | medium | `_check_render_pass_contract` 用原始 connections 而非 Group 展平后的边，Group 内的 Scene3D AOV 端口绕过检查 |
| AUD-48004 | Suggestion | process | `src/features/composite/core/graph_preflight.py:210-214` | medium | “图中没有输出节点”在 export 模式下仍为 WARNING，与模块自身 severity 政策矛盾 |
| AUD-48005 | Suggestion | process | `src/features/composite/core/graph_preflight.py:1192（及 753、766）` | medium | `run_preflight` 的 `graph` 参数无类型注解；`_check_groups.visit` 的 `path` 参数从未被读取 |
| AUD-48006 | Suggestion | doc-gap | `src/features/composite/core/graph_snapshot.py:31-32（及 89、98）` | medium | 文档声明"Immutable snapshot"的三个 dataclass 均未加 `frozen=True`，跨线程不可变契约无任何强制 |
| AUD-48007 | Critical | risk | `src/features/composite/core/image_loader.py:692-714` | medium | `cancel_all` 在 2 秒有界等待后无条件清空 keep-alive 列表，仍在运行的解码线程失去全部引用，可被循环 GC 在运行中销毁 QThread 导致进程崩溃 |
| AUD-48008 | Major | bug | `src/features/composite/core/image_loader.py:439-466（2.x 路径）、317-325（3.x 路径）、447` | medium | 仅含 Y（亮度）通道的 EXR 解码为全黑图像：读取循环只取 R/G/B/A，缺失通道零填充，Y 数据从未被读出 |
| AUD-48009 | Suggestion | risk | `src/features/composite/core/image_loader.py:701-706` | medium | `cancel_all` 在互斥锁外执行 `self._cancelled_workers.clear()`，违反“所有公共方法线程安全”的类契约 |
| AUD-48010 | Suggestion | risk | `src/features/composite/core/image_loader.py:716-720（及 116-117、656-680）` | medium | 被取消的 worker 永不触发 `_cleanup_finished_workers_unlocked`，已结束的 QThread 对象在会话内累积；且并发解码线程数无上限 |
| AUD-48011 | Suggestion | process | `src/features/composite/core/image_loader.py:475-477（及 495-498）` | medium | `np.frombuffer` 产生的只读数组经 `ascontiguousarray` 不会被复制，`depth_data`/`extra_channels` 以只读视图形式逃逸出公共 API |
| AUD-48012 | Suggestion | process | `src/features/composite/core/image_loader.py:62-64（及 631-632）` | medium | 容器字段/属性类型注解未参数化（`list`、`dict` 裸类型） |
| AUD-48013 | Suggestion | process | `src/features/composite/core/layer_model.py:203-218` | medium | `Layer.from_dict` 验证缺口：布尔字段未强转、`in_frame`/`out_frame` 未做类型检查、`layer_type` 未对照 `ALL_LAYER_TYPES` 校验 |
| AUD-48014 | Suggestion | process | `src/features/composite/core/layer_provenance.py:118-121（及 184-187）` | medium | `for_node` 对每个图层重复全量溯源（每次 `_trace_chain` 重建全图 incoming 表），整图逐节点查询放大为 O(N·L·E) |
| AUD-48015 | Suggestion | risk | `src/features/composite/core/layer_provenance.py:192-207` | medium | `_trace_chain.visit` 为无界递归，病态长链会触发 RecursionError |
| AUD-4900 | Major | bug | `src/features/composite/core/layer_stack_compiler.py:694-702（及 674-691、602-625）` | medium | 编译器对已存在 Merge 节点立即写参数，“干跑”编译实际产生了图变异且不受 undo 约束 |
| AUD-4901 | Major | doc-gap | `src/features/composite/core/layer_stack_compiler.py:602-619（映射逻辑）；佐证：src/features/composite/core/shaders.py:380 “opacity modulates the foreground”` | medium | opacity/blend 投影落点错位：中间层 opacity 调制的是“上方整组”，底层与单层栈完全不投影（文档承诺的 Transform 回退未实现） |
| AUD-4902 | Suggestion | process | `src/features/composite/core/layer_stack_compiler.py:286-294（及 298-309）` | medium | 新建 Merge 的摆放基准点计算后从未使用，`_merge_placement_hint` 实为死代码 |
| AUD-4903 | Suggestion | process | `src/features/composite/core/layer_stack_compiler.py:235-238` | high | `_layer_is_live` 每层重扫全表判断 solo，编译复杂度 O(N²) |
| AUD-4904 | Suggestion | process | `src/features/composite/core/layer_stack_compiler.py:60` | high | `_CompileCtx.managed_node_ids` 类型注解退化为裸 `set` |
| AUD-4905 | Critical | bug | `src/features/composite/core/layer_stack_controller.py:1684-1704` | medium | `_recompile_stack_recursive` 丢弃嵌套（Precomp 子栈）编译产生的变更：只聚合父栈 changes，子栈 create/connect 变更从未被应用，但子栈 `generated_merge_node_i |
| AUD-4906 | Major | bug | `src/features/composite/core/layer_stack_controller.py:1099-1113（Layer 构造）与 1133-1143（_clone_node）；关联删除路径 906-927` | medium | `duplicate_layer` 复制 Precomp 层时共享同一个 child_stack_id：双引用别名 + 删除任一层会拔掉对方的子栈 |
| AUD-4907 | Major | bug | `src/features/composite/core/layer_stack_controller.py:1170-1172（stack 取值）、1200-1205（matte 校验` | medium | 属性/移动/复制操作全部锚定 main 栈：嵌套栈层的 parent 与 track_matte 校验对着 main 的 layer_ids 比对，必然失败；move/duplicate 直接不支持嵌套层 |
| AUD-4908 | Suggestion | process | `src/features/composite/core/layer_stack_controller.py:380-403（add_layer）、493-568（add_media_layer）、902-1009（remove_layer）、1019-1040（move_layer）、1234-1258（set_layer_property）、1065-1131（duplicate_layer）；对照 435-453、696-720、746-792 的回滚版本` | medium | 异常回滚不一致：新流程有 `_restore(before)`，老流程（add_layer/add_media_layer/remove_layer/move_layer/set_layer_property/duplicate_layer |
| AUD-4909 | Suggestion | process | `src/features/composite/core/layer_stack_controller.py:1719-1722` | medium | `_apply_changes` 硬编码绝对导入 MergeNode，绕过注入的 node_factory |
| AUD-4910 | Suggestion | test-gap | `src/features/composite/core/layer_stack_controller.py:1965-1967` | high | `_new_layer_id` 的首个成员测试恒为 False（str 与 Layer 对象比较） |
| AUD-4911 | Suggestion | process | `src/features/composite/core/layer_stack_controller.py:1234（set_layer_property 的 before）、1795-1830（_snapshot 全量序列化）` | high | 每次属性变更（含可合并的 opacity 拖动）都做全图快照，交互热路径成本高 |
| AUD-4912 | Suggestion | process | `src/features/composite/core/layer_stack_controller.py:1924-1927（_push_undo 吞掉 UndoAction 导入失败→undo 被静默禁用）、1867-1881（from_dict 失败 continue，节点无声消失）、1886-1890（connect 失败 pass）、1913-1916（set_expression 失败 pass）` | medium | undo/恢复链路上的宽泛 `except` 可能掩盖领域错误，静默丢失节点/连接 |
| AUD-4913 | Suggestion | process | `src/features/composite/core/layer_stack_controller.py:1173-1233（mutable_props 无 label_color 分支）、467-475（layer_type 形参直接透传）` | medium | 输入验证缺口：label_color 任意值放行、add_media_layer 的 layer_type 未校验 |
| AUD-4914 | Suggestion | process | `src/features/composite/core/layer_stack_controller.py:1099-1113` | medium | `duplicate_layer` 丢失 solo/parent_layer_id/track_matte_* 字段 |
| AUD-4915 | Suggestion | process | `src/features/composite/core/layer_stack_controller.py:1843-1852、1884` | medium | `_restore` 绕过 NodeGraph 的 RLock 直接清写内部容器 |
| AUD-5000 | Major | bug | `src/features/composite/core/layer_stack_validator.py:329-338` | medium | 图环检测为死代码：`topological_sort()` 有环时抛异常而非返回部分列表，`node_graph_cycle` 错误永远不会被报告 |
| AUD-5001 | Suggestion | risk | `src/features/composite/core/layer_stack_validator.py:333` | high | 无锁直接读取 `self.graph.nodes`，绕过 NodeGraph 的 RLock 保护 |
| AUD-5002 | Suggestion | test-gap | `src/features/composite/core/layer_stack_validator.py:280` | high | Track Matte 校验对 `stack.layer_ids`（list）做逐层成员测试，整体 O(n²) |
| AUD-5003 | Suggestion | process | `src/features/composite/core/layer_stack_validator.py:73-74、82-86、123` | high | `graph` 参数为 `Any` 且未做 None 防御，`stack` 合法但 `graph` 为 None 时直接 AttributeError |
| AUD-5004 | Major | bug | `src/features/composite/core/media_drop_payload.py:92-95、314-339（对照 :51、62-67）` | medium | 音频资产被静默误分类为 image，拖放后创建指向音频文件的 image_input 节点（坏图层） |
| AUD-5005 | Suggestion | process | `src/features/composite/core/media_drop_payload.py:150-161` | medium | text/plain 兜底要求文本含逗号，单资产拖拽的兜底永远不生效 |
| AUD-5006 | Suggestion | process | `src/features/composite/core/media_drop_payload.py:201-219` | medium | 解析失败时按 index 把 asset_id 与 fallback_paths 配对的启发式可错配文件；`asset_lookup` 周围宽泛 except 掩盖领域错误 |
| AUD-5007 | Suggestion | process | `src/features/composite/core/media_drop_payload.py:281-289` | medium | 显式携带 duration<=0 的视频/序列资产被置 duration=0，下游写入 `total_frames=0` |
| AUD-5008 | Major | doc-gap | `src/features/composite/core/node_capabilities.py:1245-1248（对照 docstring :1230-1231）` | medium | 仅在 NODE_REGISTRY 动态注册的插件节点类型查询能力返回 None，被调度器归为 placeholder 而不渲染；docstring 与实现不符 |
| AUD-5009 | Suggestion | process | `src/features/composite/core/node_capabilities.py:1250-1254` | medium | `get_node_capability` 每次调用都执行 `from .node_engine import NODE_REGISTRY`，热路径谓词函数逐节点逐帧付出重复导入开销 |
| AUD-5010 | Suggestion | process | `src/features/composite/core/node_capabilities.py:1168-1180（派生集合）、1256-1274（覆盖逻辑）、1450-1512（检查范围）` | medium | 导出的模块级派生集合不应用 IS_PLACEHOLDER 类属性覆盖，与 `get_node_capability` 形成两套可分歧的真值；一致性检查只覆盖 placeholder 字段 |
| AUD-5011 | Suggestion | process | `src/features/composite/core/node_capabilities.py:30-31、1115` | high | 未使用导入（`field`、`FrozenSet`）与裸 `tuple` 返回注解 |
| AUD-5100 | Major | bug | `src/features/composite/core/node_engine.py:578-598` | medium | remove_node_and_reconnect 缺少 remove_node 具备的表达式/绑定/父子关系清理 |
| AUD-5101 | Major | bug | `src/features/composite/core/node_engine.py:579-592` | medium | remove_node_and_reconnect 在连接已被剥离后才调用 _mark_dirty_downstream，实际是空操作 |
| AUD-5102 | Major | risk | `src/features/composite/core/node_engine.py:1313-1344` | medium | evaluate() 遍历节点与缓存期间不持图锁，锁纪律不一致 |
| AUD-5103 | Major | risk | `src/features/composite/core/node_engine.py:2087-2140（to_dict）、2367-2464、2466-2522` | medium | to_dict()/validate()/两个 migrate_* 公有方法无锁读写图状态 |
| AUD-5104 | Major | bug | `src/features/composite/core/node_engine.py:2158-2176` | medium | from_dict 直接索引连接字段，损坏的项目数据在加载期抛未验证异常 |
| AUD-5105 | Major | bug | `src/features/composite/core/node_engine.py:2188-2199` | medium | from_dict 将 groups/viewport_state/viewer_state 按引用别名赋值且无结构验证 |
| AUD-5106 | Suggestion | process | `src/features/composite/core/node_engine.py:1346-1353` | medium | evaluate() 宽泛 except Exception 仅记 warning，不回写节点错误状态 |
| AUD-5107 | Suggestion | process | `src/features/composite/core/node_engine.py:947-952` | medium | resolve_all_expressions 以副作用方式改写节点活参数 |
| AUD-5108 | Suggestion | process | `src/features/composite/core/node_engine.py:931-936` | high | resolve_node_expressions 硬编码 fps=24.0，忽略 self.fps |
| AUD-5109 | Suggestion | process | `src/features/composite/core/node_engine.py:1476-1477` | high | _evaluate_snapshot_parameters 无条件为所有节点生成参数别名深拷贝 |
| AUD-5110 | Suggestion | perf-opportunity | `src/features/composite/core/node_engine.py:2346-2348、2363-2365、1280-1281` | high | 图遍历复杂度 O(V·E)/O(n²)：环检测、下游标脏逐节点全量扫连接；拓扑排序用 list.pop(0) |
| AUD-5111 | Suggestion | process | `src/features/composite/core/node_engine.py:2170-2176` | high | 加载期 connect() 失败被静默丢弃且每次成功连接都 bump generation |
| AUD-5112 | Suggestion | process | `src/features/composite/core/node_engine.py:1807-1811、1700-1701` | high | snapshot() 拓扑成环时静默回退插入序，且循环体内重复 import copy |
| AUD-5113 | Suggestion | process | `src/features/composite/core/node_engine.py:992-1002、2037-2047` | medium | get_sticky_note/get_group 等取值器返回内部活 dict 引用 |
| AUD-5114 | Suggestion | process | `src/features/composite/core/node_engine.py:2658-2660（对照注释 119-129）` | medium | unregister_node_type 对非 DEFERRED 类型也置 None，违反自述的注册表契约 |
| AUD-5115 | Suggestion | process | `src/features/composite/core/node_engine.py:1293、1235、2351、2600、2779` | medium | 关键签名缺类型注解 / 隐式 Optional |
| AUD-5116 | Suggestion | process | `src/features/composite/core/node_engine.py:1342-1343、616` | medium | 求值缓存 LRU 驱逐与 clear() 直接丢弃 TextureResult，未走 deferred-release |
| AUD-5117 | Suggestion | process | `src/features/composite/core/node_engine.py:1357-1363` | high | has_connected_output 只检查第一个 OutputNode |
| AUD-5200 | Major | bug | `src/features/composite/core/node_plugin.py:182` | medium | 应用级插件目录路径解析错误，仓库自带节点插件永不被发现 |
| AUD-5201 | Major | bug | `src/features/composite/core/node_plugin.py:279-280、306-314` | high | exec_module 失败后残破模块永久遗留 sys.modules |
| AUD-5202 | Major | bug | `src/features/composite/core/node_plugin.py:434-438（对照 372-378 的 override 注册）` | medium | unregister_plugin 会删除被其他插件 override 的同名节点类型 |
| AUD-5203 | Major | bug | `src/features/composite/core/node_plugin.py:369-398、400-409` | medium | register_plugin 注册非原子：异常路径留下幽灵节点类型且重试会产生分类重复 |
| AUD-5204 | Suggestion | doc-gap | `src/features/composite/core/node_plugin.py:163-164` | high | docstring 虚构 "Discovery uses a file lock"，实际无任何锁 |
| AUD-5205 | Suggestion | process | `src/features/composite/core/node_plugin.py:389-391、478-501` | medium | 注册时写入的 PluginInfo.path/enabled 不持久，查询结果恒为默认值 |
| AUD-5206 | Suggestion | process | `src/features/composite/core/node_plugin.py:348-362` | high | on_load 成功后注册失败，不回滚 on_unload，钩子不平衡 |
| AUD-5207 | Suggestion | process | `src/features/composite/core/node_plugin.py:519-536` | high | reload_plugins 不清理 sys.modules 中的旧插件模块 |
| AUD-5208 | Suggestion | process | `src/features/composite/core/node_plugin.py:140` | high | check_dependencies 返回类型为裸 tuple，无参数化注解 |
| AUD-5209 | Suggestion | process | `src/features/composite/core/output_targets.py:90-95` | medium | 重复/空 node id 在 node_order 中相互覆盖，依赖集与顺序可能失真 |
| AUD-5210 | Suggestion | risk | `src/features/composite/core/runtime_context.py:60-65` | medium | clip_effects 将可变 EffectInstance 引用带入"frozen"上下文，跨线程别名风险 |
| AUD-5211 | Suggestion | process | `src/features/composite/core/runtime_context.py:56-57` | medium | quality/input_stage 为自由字符串，未与 CompositeQualityProfile 归一化对接 |
| AUD-5212 | Suggestion | process | `src/features/composite/core/runtime_context.py:63` | high | clip_effects 类型注解为裸 tuple |
| AUD-5213 | Suggestion | risk | `src/features/composite/core/runtime_diagnostics.py:66-68` | high | publish_many 逐条加锁，可合并为单次临界区 |
| AUD-5300 | Critical | bug | `src/features/composite/core/shaders.py:3797-3832（H 左/右装载）、3841-3846（H 读取）、3895-3930（V 装载）、3937-3942（V 读取）` | medium | 共享内存版模糊计算着色器的 padding 协作装载只填充每 16 个槽位中的一个，卷积读取未初始化共享内存 |
| AUD-5301 | Critical | bug | `src/features/composite/core/shaders.py:3992-4022（装载）、4029-4031（读取）` | medium | _ERODE_DILATE_COMPUTE_SHARED 仅装载 4 个角块，padding 边条区域未初始化即被形态学核读取 |
| AUD-5302 | Critical | bug | `src/features/composite/core/shaders.py:3962` | high | _ERODE_DILATE_COMPUTE_SHARED 声明约 210KB 共享内存，超出 GL 保证上限与常见硬件实际上限，程序无法建立 |
| AUD-5303 | Major | bug | `src/features/composite/core/shaders.py:3773-3775+3835（BLUR_H_SHARED）、3876-3878+3932（BLUR_V_SHARED）、3970-3972+4024（ERODE_DILATE_SHARED）` | medium | 三个共享内存计算着色器在 barrier() 之前提前 return，分叉屏障属未定义行为 |
| AUD-5304 | Critical | bug | `src/features/composite/core/shaders.py:2486` | medium | _ERODE_DILATE_FRAGMENT 的 reflect 边界模式公式把画面内坐标整体 180° 翻转 |
| AUD-5305 | Major | bug | `src/features/composite/core/shaders.py:4731-4738` | high | _RAMP_FRAGMENT 的 mirror 延展模式是周期 1 三角波，[0,1] 内的 t 也被反转 |
| AUD-5306 | Major | bug | `src/features/composite/core/shaders.py:1851（_COLOR_CORRECT）、2069（_SET_ALPHA）、2172（_HUECORRECT）、3119（_FUSED_COLOR）、3537（_LUT3D）、4116（_DEEP_MERGE）` | medium | 六个着色器仍保留 S3-A 已在混合着色器中废除的歧义 mask 启发式 `m.a > 0 ? m.a : m.r` |
| AUD-5307 | Major | bug | `src/features/composite/core/shaders.py:3141-3152（_FUSED_COLOR）对照 919-929（_GRADE）；_FUSED 缺参见 2822-2825` | medium | _FUSED_COLOR 的 grade 阶段算子顺序与 _GRADE 不一致（且 _FUSED 缺少扩展参数），融合与逐节点渲染结果分叉 |
| AUD-5308 | Suggestion | process | `src/features/composite/core/shaders.py:1907` | high | _COLOR_CORRECT_FRAGMENT 的 alpha 通道选择器是无操作（mix 两端相同） |
| AUD-5309 | Suggestion | process | `src/features/composite/core/shaders.py:1762-1763` | high | _CS_TRANSFORM 每像素调用 inverse() 求常量矩阵的逆 |
| AUD-5310 | Suggestion | process | `src/features/composite/core/shaders.py:4677-4686` | medium | _NOISE_FRAGMENT 三个输出通道分支结果完全相同，u_output_channels 无实际作用 |
| AUD-5311 | Suggestion | process | `src/features/composite/core/shaders.py:417-418 与 451-452（_BLUR_H/_V_FRAGMENT）、2412-2415（_SHARPEN）、2474-2477（_ERODE_DILATE_FRAGMENT）、3606-3607 与 3662-3663（_BLUR_*_COMPUTE_SSBO）、5683-5689（_DENOISE）、674-677（_BILATERAL）` | medium | 多个片元着色器仍使用“静态最大边界 + continue”循环，未跟进 T-P1-21 的动态边界改造 |
| AUD-5312 | Suggestion | process | `src/features/composite/core/shaders.py:3615-3619（SSBO H）、3670-3674（SSBO V）、3803-3807/3821-3825（SHARED H）` | medium | 计算着色器版模糊仅处理 edge_policy 0/1，mirror(2) 被静默降级且两条路径行为不一致 |
| AUD-5400 | Critical | bug | `src/features/composite/core/tile_padding_computer.py:140` | medium | 源类型硬编码集合缺失 video_frame/text/ramp/noise 等源与生成器节点，这些图源的 total_padding 恒为 0，分块渲染出现接缝 |
| AUD-5401 | Critical | bug | `src/features/composite/core/tile_padding_computer.py:272` | medium | _get_node_padding 硬编码 if/elif 类型链遗漏 denoise/face_smooth/light_wrap/edge_detect 等已声明非零 padding 的 effect 节点，一律返回 0 |
| AUD-5402 | Major | bug | `src/features/composite/core/tile_padding_computer.py:383` | medium | _get_node_padding 用宽泛 `except Exception: pass` 吞掉全部异常并静默返回 0 padding |
| AUD-5403 | Suggestion | process | `src/features/composite/core/tile_padding_computer.py:163` | high | `memory_budget_mb or 默认值` 把显式传入的 0 预算当作未提供 |
| AUD-5404 | Suggestion | process | `src/features/composite/core/tile_padding_computer.py:389` | medium | _compute_tile_size 的 output_w/output_h 形参从未使用，属误导性死参数 |
| AUD-5405 | Suggestion | process | `src/features/composite/core/tile_scheduler.py:83` | medium | generate_tiles 输入验证不足：负 padding 导致评估区小于输出区、float tile_size 直接抛 TypeError |
| AUD-5406 | Suggestion | process | `src/features/composite/core/time_remap.py:147` | medium | 纯仿射折叠引入取整漂移，链式 retime 实际取帧与逐层合成可差 1-2 帧 |
| AUD-5407 | Suggestion | process | `src/features/composite/core/time_remap.py:203` | medium | precomp 的 hold_frame=None 时 int(None) 抛 TypeError，整个节点退化为 pass-through，speed/offset 一并失效 |
| AUD-5408 | Critical | bug | `src/features/composite/core/undo_stack.py:629` | medium | UndoTree.compress 合并删除 next_node 时不重接其子节点，当前历史链断裂：undo 游标会走进已删除节点，redo 永久失效，被合并动作不可达 |
| AUD-5409 | Major | bug | `src/features/composite/core/undo_stack.py:658` | medium | compress 的按最旧淘汰剪枝同样不重接子链：可删除 current 的祖先，keep_types 节点沦为不可达孤儿，undo 到底后游标落入 ghost 触发与 AUD-5408 相同的级联 |
| AUD-5410 | Major | bug | `src/features/composite/core/undo_stack.py:201` | medium | load_from_file 的动作恢复是死代码：UndoNode.from_dict 从不写入 action 字段，恢复循环永不执行，载入后 undo 移游标不执行动作、redo 拒绝前进 |
| AUD-5411 | Major | bug | `src/features/composite/core/undo_stack.py:819` | medium | save_to_file 对含 group 动作的历史必然失败：data 内嵌 UndoAction 对象列表，json.dump 抛 TypeError 被宽泛 except 吞掉 |
| AUD-5412 | Major | bug | `src/features/composite/core/undo_stack.py:286` | medium | undo/redo 执行回调无异常保护：回调半途抛异常时游标不移动、无回滚，重试会把已部分应用的子动作再应用一遍 |
| AUD-5413 | Suggestion | process | `src/features/composite/core/undo_stack.py:777` | medium | begin_merge_coalescing 无条件丢弃未提交的 _pending_merge_action，嵌套/重复 begin 会静默丢失撤销记录 |
| AUD-5414 | Suggestion | process | `src/features/composite/core/undo_stack.py:840` | medium | undo/redo 执行期间无重入保护，回调触发的参数变更路径可重入 push() 污染正在撤销的历史 |
| AUD-5415 | Suggestion | process | `src/features/composite/core/undo_stack.py:593` | high | 类型注解不严谨：可变默认值语义的参数未标 Optional |
| AUD-5500 | Major | risk | `src/features/composite/core/video_context.py:1045-1052` | medium | shutdown() 先清空 _node_clips 再调 close_clip_for_node，服务级剪辑从未被关闭（资源泄漏） |
| AUD-5501 | Major | bug | `src/features/composite/core/video_context.py:933-951` | medium | _compute_source_frame 未验证 source_in/out 区间，duration<=0 时 loop 路径 ZeroDivisionError 或负数模导致错误帧 |
| AUD-5502 | Major | risk | `src/features/composite/core/video_context.py:522-529, 175-184, 668-669, 732` | medium | _timeline 共享可变状态完全绕过 _lock，UI 写入与渲染/解码线程读取存在数据竞争 |
| AUD-5503 | Suggestion | process | `src/features/composite/core/video_context.py:694-710` | medium | request_gpu_frame_for_node 缺返回类型注解；is_stale 注解用了非法的小写 callable |
| AUD-5504 | Suggestion | process | `src/features/composite/core/video_context.py:186-195` | medium | TimelineContext.update_time 用 int() 截断，与 _compute_source_frame 已修复的同类浮点差一帧问题不一致 |
| AUD-5505 | Suggestion | process | `src/features/composite/core/video_context.py:116-139, 246-261` | medium | RenderQuality.AUTO 语义未实现，to_frame_quality 恒返回 'proxy' 且全库无调用者 |
| AUD-5506 | Suggestion | process | `src/features/composite/core/video_context.py:444-456, 966-1012, 1061-1082` | medium | set_node_params/get_node_params 以 clip_id 为键，共享同一文件的多个节点参数互相覆盖；且该 API 与 VideoNodeParams 在 src/tests 均无调用者 |
| AUD-5507 | Suggestion | process | `src/features/composite/core/video_context.py:759-762` | medium | has_vt_source_for_node 宽泛 except Exception 直接返回 False，掩盖服务错误并反转 CPU 帧放行策略 |
| AUD-5601 | Major | bug | `src/features/composite/edit_service.py:758` | medium | set_expression/remove_expression 直接调用 Node 层方法，绕过引擎的环检测、表达式依赖管理器注册、脏传播与 generation 递增 |
| AUD-5602 | Major | risk | `src/features/composite/edit_service.py:314` | medium | 关键帧/参数/表达式类操作不递增 graph._generation，乐观锁 base_generation 与 graph_revision 缓存 token 失真 |
| AUD-5603 | Major | bug | `src/features/composite/edit_service.py:321` | medium | validate/apply 状态不对称：同一 plan 内 remove_node 之后的操作仍可引用被删节点 |
| AUD-5604 | Major | bug | `src/features/composite/edit_service.py:422` | medium | add_node 对注册表 None 占位类型（如 motion_graphic）在 validate 阶段抛未捕获 TypeError |
| AUD-5605 | Major | bug | `src/features/composite/edit_service.py:395` | medium | set_parameter 对 str/file/hdri/channel 类型缺少类型校验：缺 value 键通过校验后 apply 抛 KeyError，任意标量可写入 str 参数 |
| AUD-5606 | Major | bug | `src/features/composite/edit_service.py:654` | medium | add_node.position 完全未校验：类型错误在 apply 期才爆，inf/nan 可入库 |
| AUD-5607 | Major | bug | `src/features/composite/edit_service.py:492` | medium | group/ungroup 的存在性未在 validate 阶段校验，非法 plan 整体回滚而非给出诊断 |
| AUD-5608 | Major | bug | `src/features/composite/edit_service.py:74` | medium | CompositeEditPlan.from_dict 对畸形输入抛非领域异常（TypeError/ValueError 逃逸边界） |
| AUD-5609 | Suggestion | process | `src/features/composite/edit_service.py:694` | medium | duplicate_node 不克隆 keyframe_tracks/_expressions，与"克隆类型与全部参数"注释矛盾；position_offset=0 被静默改为 40 |
| AUD-5610 | Suggestion | process | `src/features/composite/edit_service.py:398` | medium | int 参数取值校验与归一化在三条路径上不一致：set_parameter 拒绝非整数，add_keyframe 静默取整，set_keyframes 原样存浮点 |
| AUD-5611 | Suggestion | process | `src/features/composite/edit_service.py:304` | medium | 回滚路径自身无异常保护，且恢复流程多处 `except Exception: pass` 静默吞错 |
| AUD-5612 | Suggestion | process | `src/features/composite/edit_service.py:530` | medium | 同一 plan 内多条连线打同一单输入端口不产生告警，引擎静默替换使计划与实际图结构不符 |
| AUD-5613 | Suggestion | process | `src/features/composite/edit_service.py:872` | medium | _validate_color_value 硬编码 [0,1]，忽略 ParamDef 的 min_val/max_val |
| AUD-5614 | Suggestion | process | `src/features/composite/edit_service.py:166` | medium | 领域对象全程 Any 类型 + 直接读写引擎私有属性 |
| AUD-5615 | Suggestion | process | `src/features/composite/edit_service.py:819` | high | 校验期临时节点实例无缓存，大 plan 上重复构造 |
| AUD-57001 | Suggestion | doc-gap | `src/features/composite/evaluators/__init__.py:24-25` | high | docstring 引用不存在的路径 src/composite/graph_evaluator_mixin.py |
| AUD-57002 | Suggestion | process | `src/features/composite/evaluators/ai_evaluator.py:22-23, 38-41` | medium | _AI_NODE_TYPES 与 base_evaluator.py 重复定义（双事实源），且 get_ai_node_types() 全库无调用方 |
| AUD-57003 | Suggestion | process | `src/features/composite/evaluators/ai_evaluator.py:16-19` | high | 导入 Dict 未使用、TYPE_CHECKING 块为空 pass（死代码） |
| AUD-57004 | Suggestion | doc-gap | `src/features/composite/evaluators/ai_evaluator.py:29-31` | high | docstring 引用不存在的路径 src/composite/ai_compute_manager.py |
| AUD-5801 | Major | bug | `src/features/composite/evaluators/base_evaluator.py:3417` | medium | `_apply_fused_chain` 输出 TextureResult 未传播上游 revision，池 glo 回收后可致下游缓存键陈旧命中 |
| AUD-5802 | Major | bug | `src/features/composite/evaluators/base_evaluator.py:2419` | high | `_apply_merge_blend` 渲染后未解绑纹理单元 0，违背本文件 P1-6 绑定纪律 |
| AUD-5803 | Major | bug | `src/features/composite/evaluators/base_evaluator.py:2281` | medium | merge 前自动色彩空间对齐分配的转换纹理无所有权归属，依赖池预算驱逐兜底 |
| AUD-5804 | Major | bug | `src/features/composite/evaluators/base_evaluator.py:1591` | medium | `_upload_ai_result_texture` 结果纹理 revision 固定为 1，未随输入内容演进 |
| AUD-5805 | Major | bug | `src/features/composite/evaluators/base_evaluator.py:1573` | medium | `_readback_texture_fullres` 对 HDR 线性数据无条件 clamp(0,1)，合成回写丢失高光 |
| AUD-5806 | Major | bug | `src/features/composite/evaluators/base_evaluator.py:1034` | medium | 节点求值异常路径无纹理回滚，且警告日志不带堆栈 |
| AUD-5807 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:697` | medium | 陈旧请求取消路径的纹理释放循环是死代码 |
| AUD-5808 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:690` | medium | dirty-skip 分支设置 output_result 未校验 `_is_selected_output`，与 cache-hit 分支不对称 |
| AUD-5809 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:11` | medium | 未用导入与死代码：math/traceback/GPUResourcePool/RenderScheduler/NodePlan、两个未接线局部变量、无调用的 `_first_output_name` |
| AUD-5810 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:3019` | medium | 跨模块访问他人私有属性（fb_manager/_resource_pool/perf/scheduler 内部结构） |
| AUD-5811 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:3085` | medium | `_create_ephemeral_node` 时间范围门控未做 int 容错，与源节点门控路径不一致 |
| AUD-5812 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:1810` | high | `_motion_blur_sub_frames` 中 `if n == 1` 为不可达分支，且 samples=1 的用户意图被静默改写 |
| AUD-5813 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:3185` | medium | 多个方法缺少参数/返回类型注解 |
| AUD-5814 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:343` | high | deprecated color_input 节点在每次快照同步都打 info 日志 |
| AUD-5815 | Suggestion | process | `src/features/composite/evaluators/base_evaluator.py:1052` | medium | 求值循环直接改写"不可变"快照节点的 port_revisions |
| AUD-5901 | Critical | bug | `src/features/composite/evaluators/color_evaluator.py:151-156` | medium | `_apply_color_correct` 设置 shader 不存在的 `u_brightness` uniform，每次求值必抛 KeyError，ColorCorrect 节点输出为空 |
| AUD-5902 | Major | bug | `src/features/composite/evaluators/color_evaluator.py:139-165` | medium | `_apply_color_correct` 未映射 ColorCorrectNode 的 13 个真实参数，且 8 个 shader 必需 uniform 缺省为 0 将校正完全还原甚至压碎中间调 |
| AUD-5903 | Major | risk | `src/features/composite/evaluators/color_evaluator.py:92, 145, 189, 252, 286, 314, 340, 366, 434` | high | 九个效果方法池纹理获取后无异常保护（同 AUD-5910 泄漏模式） |
| AUD-5904 | Suggestion | process | `src/features/composite/evaluators/color_evaluator.py:415-425` | medium | `_apply_topos_color` 每次求值都实例化 `ColorStackRenderService` 并解析 stack |
| AUD-5905 | Suggestion | process | `src/features/composite/evaluators/color_evaluator.py:476-480` | high | `_apply_topos_color` 自建第二份 1×1 白掩码纹理，重复 `_get_white_mask_texture()` 且永不释放 |
| AUD-5906 | Suggestion | process | `src/features/composite/evaluators/color_evaluator.py:115, 167-177, 214, 483（对照 126-127 的正确解绑）` | high | 多个效果渲染后不解绑纹理单元（仅 `_apply_grade` 遵循 P1-6），且 `_apply_grade` 存在死变量 `white_tex` |
| AUD-5910 | Major | risk | `src/features/composite/evaluators/blur_evaluator.py:175-207` | medium | 池纹理获取后无 try/finally 保护，求值中途抛错即永久泄漏整幅纹理 |
| AUD-5911 | Suggestion | process | `src/features/composite/evaluators/blur_evaluator.py:167-170` | high | 旧版 blur_h/blur_v 回退路径静默丢弃 kernel_mode/通道掩码/mix 语义 |
| AUD-5912 | Suggestion | process | `src/features/composite/evaluators/blur_evaluator.py:181-204` | medium | separable 双 pass 渲染后残留纹理单元绑定，与 P1-6 约定不一致 |
| AUD-5913 | Suggestion | process | `src/features/composite/evaluators/blur_evaluator.py:354-357, 403-408` | medium | `_apply_defocus`/`_apply_sharpen` 直接索引 uniform，未沿用 `_set_uniform_if_present` 容错模式 |
| AUD-5914 | Suggestion | process | `src/features/composite/evaluators/blur_evaluator.py:365 vs 416 与 93` | high | `_apply_defocus` 结果沿用原始 bbox，而 blur/sharpen 传 `effective_bbox()`，元数据传播不一致 |
| AUD-5920 | Major | bug | `src/features/composite/evaluators/cache_evaluator.py:101-129` | medium | 磁盘缓存命中路径丢失 depth/AOV/render_diagnostics 元数据，与新鲜求值结果不一致 |
| AUD-5921 | Suggestion | process | `src/features/composite/evaluators/cache_evaluator.py:88-95` | medium | `_readback_result_array` 切换 FBO 与 viewport 后不恢复全局 GL 状态 |
| AUD-5922 | Suggestion | process | `src/features/composite/evaluators/cache_evaluator.py:179-184` | high | `_invalidate_cache_with_downstream` 用裸 `except Exception: pass` 吞掉遍历错误且无日志 |
| AUD-5923 | Suggestion | risk | `src/features/composite/evaluators/cache_evaluator.py:74-99` | high | 磁盘缓存同步全帧 f4 readback + npz 压缩阻塞渲染线程（MVP 已注明） |
| AUD-5930 | Major | risk | `src/features/composite/evaluators/channel_evaluator.py:30, 68, 114, 179` | medium | 四个效果方法池纹理获取后无异常保护（同 AUD-5910 泄漏模式） |
| AUD-5931 | Suggestion | process | `src/features/composite/evaluators/channel_evaluator.py:64-74` | medium | `_apply_copy_channel` 只验证 input_b 不验证 input_a（不对称输入验证） |
| AUD-5932 | Suggestion | process | `src/features/composite/evaluators/channel_evaluator.py:42, 84, 135（对照 235-236）` | medium | shuffle/copy_channel/remove_channels 渲染后不解绑 unit 0，与 channel_merge 的完整清理不一致 |
| AUD-6001 | Critical | bug | `src/features/composite/evaluators/filter_evaluator.py:129` | medium | erode/dilate 迭代≥2 引用不存在的 `_resource_pool._texture_by_id`，节点求值必然抛 AttributeError |
| AUD-6002 | Major | risk | `src/features/composite/evaluators/filter_evaluator.py:1687-1698, 1700-1717` | medium | 3D LUT 纹理缓存无界、绕过资源池、无释放路径 |
| AUD-6003 | Suggestion | process | `src/features/composite/evaluators/filter_evaluator.py:104-105, 730-732, 738-740` | medium | 计算着色器回退路径的宽泛 `except Exception` 掩盖编程错误 |
| AUD-6004 | Suggestion | process | `src/features/composite/evaluators/filter_evaluator.py:716-717` | medium | `_apply_roto` 着色器缺失时不产出 `matte` 输出，破坏双端口契约 |
| AUD-6005 | Suggestion | process | `src/features/composite/evaluators/filter_evaluator.py:923-931, 963-964, 1093-1095` | medium | `_apply_paint` clone 采样的双重高级索引产生整帧拷贝，叠加全帧 f4 回读 |
| AUD-6006 | Suggestion | process | `src/features/composite/evaluators/filter_evaluator.py:275-279（透传）对比 229-237、336-344、1258-1266、1532-1540 等（未透传）` | medium | 效果输出 TextureResult 未透传 `bit_depth`/`revision`，HDR 元数据在链路中被降级 |
| AUD-6007 | Suggestion | process | `src/features/composite/evaluators/filter_evaluator.py:306-327, 376-394, 437-460` | low | uniform 赋值未做数值类型归一，与同文件其他路径风格不一致 |
| AUD-6008 | Suggestion | process | `src/features/composite/evaluators/filter_evaluator.py:1687, 1700, 1011, 1081, 544, 748` | medium | 多个辅助函数缺少参数/返回类型注解 |
| AUD-6009 | Major | bug | `src/features/composite/evaluators/histogram_manager.py:641-645, 402, 448` | medium | 直方图缓存键依赖 glo 且 revision 恒为 0，池化纹理复用导致跨图像缓存串用 |
| AUD-6010 | Suggestion | risk | `src/features/composite/evaluators/histogram_manager.py:468-479, 393-395, 440-441` | medium | `_downsample` 在无 resource_pool/fb_manager 的直建路径泄漏纹理与 FBO |
| AUD-6011 | Suggestion | process | `src/features/composite/evaluators/histogram_manager.py:605` | high | 可变默认参数 `channel_mask=[1.0, 1.0, 1.0, 0.0]` |
| AUD-6012 | Suggestion | risk | `src/features/composite/evaluators/histogram_manager.py:508-521, 722-727, 747` | medium | `release()` 未释放 quad VBO；`get_stats` 无锁读取缓存内部结构 |
| AUD-6100 | Major | risk | `src/features/composite/evaluators/keyer_evaluator.py:505` | medium | 模块未定义 `logger`，未知 matte cleanup 操作触发 NameError 崩溃 |
| AUD-6101 | Major | bug | `src/features/composite/evaluators/keyer_evaluator.py:200-208（发送方）与 314-317（接收方）` | medium | keylight 降级路径丢失用户 screen_color，恒用默认绿 |
| AUD-6102 | Suggestion | process | `src/features/composite/evaluators/keyer_evaluator.py:359-389` | medium | `_apply_difference_keyer` 纹理单元 1 的解绑不在 finally 中 |
| AUD-6103 | Suggestion | process | `src/features/composite/evaluators/keyer_evaluator.py:466-473` | medium | matte_cleanup 的 size/iterations 只做下限钳制，无上限校验 |
| AUD-6104 | Suggestion | process | `src/features/composite/evaluators/keyframe_evaluator.py:13-18` | high | 占位 mixin 残留死导入与空 TYPE_CHECKING 块 |
| AUD-6105 | Critical | bug | `src/features/composite/evaluators/merge_evaluator.py:211` | medium | `ctx.clear(color=1.0)` 传标量导致 TypeError，DeepMerge 深度合成主路径必崩 |
| AUD-6106 | Major | bug | `src/features/composite/evaluators/merge_evaluator.py:125-138` | medium | deep_merge shader 缺失时的兜底返回错误尺寸元数据 |
| AUD-6107 | Suggestion | process | `src/features/composite/evaluators/merge_evaluator.py:176-177` | high | `alpha_clamp` 参数读取后从未下发，死参数 |
| AUD-6108 | Suggestion | process | `src/features/composite/evaluators/merge_evaluator.py:102-104（对比 41-44）` | medium | deep_merge 输入仅判 None 未判 is_valid，与 dissolve 校验不一致 |
| AUD-6109 | Suggestion | process | `src/features/composite/evaluators/merge_evaluator.py:67-79、146-202` | medium | 多输入纹理单元（1-4）的复位不在 try/finally 中 |
| AUD-6110 | Suggestion | process | `src/features/composite/evaluators/merge_evaluator.py:204-214、254-255` | medium | depth_output shader 缺失时返回"仅清空"的深度纹理且标记 has_deep_data |
| AUD-6111 | Critical | risk | `src/features/composite/evaluators/shader_library.py:91-121（STANDARD_NAMES）与 181、183、186、206（_SHADER_SOURCES）` | medium | STANDARD_NAMES 漏登 4 个已注册 shader，Roto/MaskMerge/运动模糊累积恒走降级路径 |
| AUD-6112 | Suggestion | process | `src/features/composite/evaluators/shader_library.py:270-314` | medium | `reload`/`reload_all` 立即 release 可能在用程序，违背 deferred-release 模型 |
| AUD-6113 | Suggestion | process | `src/features/composite/evaluators/shader_library.py:217-227` | low | `_ensure_fused_source` 就地变更类级共享 `_SHADER_SOURCES` |
| AUD-6114 | Suggestion | process | `src/features/composite/evaluators/shader_library.py:209、316-320` | high | 类型注解缺失 |
| AUD-6115 | Suggestion | process | `src/features/composite/evaluators/tile_evaluator.py:57-64、99-107` | medium | bbox 缩放用 int() 截断且 upsample 假定等比缩放 |
| AUD-6116 | Suggestion | process | `src/features/composite/evaluators/transform_evaluator.py:126-127、203-204` | high | 函数体内 `import logging` 且每次调用重建 logger |
| AUD-6117 | Suggestion | process | `src/features/composite/evaluators/transform_evaluator.py:156-159` | medium | translate/scale/pivot 解包无形状校验 |
| AUD-6118 | Suggestion | process | `src/features/composite/evaluators/transform_evaluator.py:366-380` | medium | crop 矩形无边界与负值校验 |
| AUD-6200 | Suggestion | risk | `src/features/composite/export_provider.py:126-147` | medium | render_full_res_passes 全库无调用方且缺少渲染线程路由，standalone-only 设计一旦接线可能在调用线程（含 UI 线程）创建 GL 上下文 |
| AUD-6201 | Suggestion | process | `src/features/composite/export_provider.py:122-124, 159-161` | high | 宽泛 except Exception 丢失异常类型与堆栈，仅保留 str(e) |
| AUD-6202 | Suggestion | process | `src/features/composite/export_provider.py:54, 60-64, 37` | high | 弱类型注解：核心依赖以 object 鸭子类型标注，泛型风格混用 |
| AUD-6203 | Suggestion | process | `src/features/composite/facade.py:133-150` | medium | build_overlay_objects 对 meta 字典直接下标访问，缺键/非 dict 时抛 KeyError/AttributeError；SimpleNamespace 回退假设属性齐全 |
| AUD-6204 | Suggestion | process | `src/features/composite/facade.py:98, 114` | high | 返回类型注解不精确（裸 tuple / 裸 dict） |
| AUD-6205 | Suggestion | process | `src/features/composite/face_workflow.py:37-41` | medium | "forehead" 多边形注释与索引几何不符：表格实为含下巴(152)的全脸轮廓 |
| AUD-6206 | Suggestion | process | `src/features/composite/face_workflow.py:69, 107` | medium | _polygon_mask 假设 landmark 为二元组，与本模块 serialize_landmarks 输出的三元组不对称 |
| AUD-6207 | Major | perf-opportunity | `src/features/composite/ml_roto.py:283-293` | medium | apply_keyframes 仅动画化 pos(x/y) 通道，c1/c2 贝塞尔柄无轨道：中间帧的控制柄冻结在末帧 |
| AUD-6208 | Suggestion | process | `src/features/composite/ml_roto.py:1-46` | high | ml_roto 在 src/ 内零调用方，属未接线服务（仅 tests/composite/test_ml_roto.py、test_advanced_plan_audit.py 引用） |
| AUD-6209 | Suggestion | process | `src/features/composite/ml_roto.py:216-219` | high | propagate 锚帧关键帧丢弃 decimated 标志，simplified_for_point_cap 恒为 False |
| AUD-6210 | Suggestion | process | `src/features/composite/ml_roto.py:243-261（反向），237-238/256-257（None break），224-261（整体）` | medium | propagate 的 stopped_reason 被反向传播覆盖、mask_to_keyframe 为 None 时静默 break；长序列传播无取消钩子 |
| AUD-6211 | Suggestion | process | `src/features/composite/ml_roto.py:283-286, 294/310/319, 306-309` | medium | apply_keyframes 含冗余同步、死变量与不去重的轨道追加 |
| AUD-6212 | Major | risk | `src/features/composite/module.py:71-76` | medium | AI 缓存回调每次写缓存都新建 AICacheManager：每实例独立锁 + manifest 读改写竞态 + 每写重载清单 |
| AUD-6213 | Major | bug | `src/features/composite/module.py:165-175` | high | _save_composite_handler 计算 graph_data 后直接丢弃并返回成功（假保存处理器，数据丢失陷阱） |
| AUD-6214 | Suggestion | process | `src/features/composite/module.py:36-42, 207, 212-216, 394-404` | medium | _runtime 未在 __init__ 初始化，hasattr 掩盖缺失；get_composite_page/get_viewer 在 runtime 无页时返回 None 而忽略 self._page |
| AUD-6215 | Suggestion | process | `src/features/composite/module.py:44-49, 80-81, 106-108` | medium | 用 ImportError 探测特性可用性会吞掉传递依赖的真实导入错误；两处宽泛 except 返回降级值掩盖领域错误 |
| AUD-6216 | Suggestion | process | `src/features/composite/module.py:36, 306, 406` | high | 类型注解缺失/参数无类型 |
| AUD-6300 | Suggestion | process | `src/features/composite/nodes/__init__.py:7-97` | medium | 包内 11 个节点模块未被 `__init__` 导出，聚合清单与目录漂移 |
| AUD-6301 | Suggestion | process | `src/features/composite/nodes/__init__.py:15-97` | low | 约 85 个 `import *` 通配导入，同名符号会被静默覆盖 |
| AUD-6302 | Suggestion | process | `src/features/composite/nodes/_gpu_readback.py:60-61` | high | `fb_manager is None` 回退路径每次回读新建 FBO 且从不释放 |
| AUD-6303 | Suggestion | process | `src/features/composite/nodes/_gpu_readback.py:62-63` | medium | `fbo.use()` + 改写 `gl.viewport` 后不恢复，GL 全局状态外泄 |
| AUD-6304 | Suggestion | process | `src/features/composite/nodes/ai_denoise_node.py:19` | high | `__init__` 缺少类型注解，与同族已注解节点不一致 |
| AUD-6305 | Suggestion | process | `src/features/composite/nodes/ai_expand_node.py:21` | high | `__init__` 缺少类型注解 |
| AUD-6306 | Major | bug | `src/features/composite/nodes/ai_gen_expand_node.py:231-248（并见 src/shared/ai_models/generation/image_gen_job.py:202-209）` | medium | 输入像素从不进入生成请求，节点却用 provider 全帧输出整体替换输入帧 |
| AUD-6307 | Major | risk | `src/features/composite/nodes/ai_gen_expand_node.py:220-222` | medium | 云端生成在渲染线程上同步执行，网络往返期间整个渲染队列停摆 |
| AUD-6308 | Major | risk | `src/features/composite/nodes/ai_gen_expand_node.py:40-41, 189-193` | medium | `cost_confirm`（费用确认回调）在渲染线程被调用，任何交互式实现都会从非 UI 线程操作 Qt |
| AUD-6309 | Major | bug | `src/features/composite/nodes/ai_gen_expand_node.py:216-221（并见 image_gen_job.py:135-142, 86-93）` | medium | 每次评估都做全帧 GPU 回读 + 全量像素 SHA-256，cache 命中路径也不例外 |
| AUD-6310 | Major | bug | `src/features/composite/nodes/ai_gen_expand_node.py:234-248` | medium | 输出纹理用裸 `gl.texture` 创建，脱离 resource_pool/fb_manager 追踪体系，无法被释放也无法被下游按 id 解析 |
| AUD-6311 | Major | bug | `src/features/composite/nodes/ai_gen_expand_node.py:43-56, 206-210` | medium | `set_generation_transport` 在生产代码中零调用，节点永远停留在 NEEDS_CONFIG 直通 |
| AUD-6312 | Suggestion | risk | `src/features/composite/nodes/ai_gen_expand_node.py:38-52` | medium | 进程级三个全局无锁、非原子地成组替换，热切换窗口可能错配 |
| AUD-6313 | Suggestion | process | `src/features/composite/nodes/ai_gen_expand_node.py:222-229, 236-248` | medium | provider 输出未经形状/通道/取值域校验即写缓存并上传，坏结果会被"投毒缓存"固化为 DONE+直通 |
| AUD-6314 | Suggestion | process | `src/features/composite/nodes/ai_gen_expand_node.py:156-174` | high | 连接的 mask 尺寸不符时被静默丢弃，退化为 border/full-frame 生成请求 |
| AUD-6315 | Suggestion | process | `src/features/composite/nodes/ai_gen_expand_node.py:134-137, 220-221` | high | `_build_job` 的 `revision_signature` 参数从未被使用（死参数） |
| AUD-6316 | Suggestion | process | `src/features/composite/nodes/ai_gen_expand_node.py:152-155, 239-245` | medium | 输出 TextureResult 沿用输入 `bbox/format`，当实际输出尺寸与输入不一致时元数据失配 |
| AUD-6317 | Suggestion | process | `src/features/composite/nodes/ai_generate_node.py:24` | high | `__init__` 缺少类型注解 |
| AUD-6318 | Suggestion | process | `src/features/composite/nodes/ai_inpaint_node.py:21` | high | `__init__` 缺少类型注解 |
| AUD-6319 | Suggestion | process | `src/features/composite/nodes/ai_remove_node.py:76-88` | medium | 异步分支不可用时的直通回退完全静默，无任何诊断状态 |
| AUD-6320 | Suggestion | process | `src/features/composite/nodes/ai_remove_node.py:76-77` | high | `_do_evaluate` 参数部分缺类型注解（ctx/fb_manager/resource_pool/render_mode） |
| AUD-64001 | Major | bug | `src/features/composite/nodes/base_node.py:486（对照 412-413、496-499、501-519）` | medium | `_init_port_revisions()` 在子类赋值 outputs 之前执行，本批次 8 类节点端口修订跟踪自始为空 |
| AUD-64002 | Major | bug | `src/features/composite/nodes/base_node.py:1243-1246` | high | `from_dict` 加载 parameters 不做任何类型/范围验证（外部数据未验证） |
| AUD-64003 | Major | bug | `src/features/composite/nodes/base_node.py:1043-1047、1093-1096` | medium | scene payload 收集时原地改写共享 `payload.node_id` 并用宽泛 `except Exception` 吞错 |
| AUD-64004 | Suggestion | process | `src/features/composite/nodes/base_node.py:399-405` | high | `display_title` 的 i18n 回退使用裸 `except Exception: pass` |
| AUD-64005 | Suggestion | process | `src/features/composite/nodes/base_node.py:278-286（对照 377、455-458）` | medium | `ParamDef.set_keyframe_track` 变异类级共享的 PARAM_DEFS 实例（危险死代码） |
| AUD-64006 | Suggestion | process | `src/features/composite/nodes/base_node.py:509-511` | high | `bump_output_revision(port_name)` 对未登记端口静默无操作 |
| AUD-64007 | Suggestion | process | `src/features/composite/nodes/base_node.py:184-187 与 199-203` | medium | `memory_bytes` 主深度通道与 AOV "depth" 通道计费口径不一致 |
| AUD-64008 | Suggestion | process | `src/features/composite/nodes/base_node.py:977-980（同类：blur_node.py:332-333、blend_node.py:268-271）` | medium | 旁路/直通路径直接复用上游 TextureResult 实例（共享可变对象别名） |
| AUD-64009 | Suggestion | process | `src/features/composite/nodes/base_node.py:411` | high | 节点默认 ID 仅取 uuid4 前 8 位（约 32 bit 熵） |
| AUD-64010 | Major | bug | `src/features/composite/nodes/blend_node.py:113-169（缺失处）；src/features/composite/nodes/base_node.py:959-976（契约与默认实现）` | medium | MergeNode 未定义 BYPASS_MAPPING，禁用节点时默认旁路取 `iter(inputs.values())` 首项（语义未定义） |
| AUD-64011 | Suggestion | process | `src/features/composite/nodes/blend_node.py:268-271` | high | 单输入直通分支忽略 `alpha_policy="output_straight"` |
| AUD-64012 | Suggestion | process | `src/features/composite/nodes/blend_node.py:284-290` | high | 占位 TextureResult 尺寸恒取 input_b，忽略 format_policy（且 `input_b.is_valid` 判断恒真） |
| AUD-64013 | Major | bug | `src/features/composite/nodes/blur_node.py:296-298、251-253、275-276` | medium | radius/radius_x/radius_y 无上限钳制，ROI/tile padding 直接按原始值展开 |
| AUD-64014 | Suggestion | process | `src/features/composite/nodes/blur_node.py:196-200` | high | `blur_type_param_visibility` 尾部死代码 |
| AUD-64015 | Major | bug | `src/features/composite/nodes/camera3d_node.py:249、287-295；佐证 src/features/composite/render3d/math_utils.py:33-39` | medium | `_do_evaluate` 读取 cam_fov 无钳制（同函数内其余参数均有 max() 防御），可生成含 inf 的投影矩阵 |
| AUD-64016 | Suggestion | process | `src/features/composite/nodes/camera3d_node.py:227-228（对照第一块 180-181 已收窄为` | high | `set_parameter` 镜头联动第二同步块 `except Exception: pass` 吞掉全部错误 |
| AUD-64017 | Suggestion | process | `src/features/composite/nodes/channel_merge_node.py:182-189` | medium | identity 直通分支仅凭 texture_id 相等判定"四输入同图" |
| AUD-64018 | Suggestion | process | `src/features/composite/nodes/chroma_keyer_node.py:102-109` | medium | tolerance/softness/edge_width/output_mode 未钳制、未强转，与同函数 screen_color 的处理不一致 |
| AUD-64019 | Suggestion | process | `src/features/composite/nodes/clamp_node.py:51；对照 src/features/composite/nodes/base_node.py:224` | high | channels 参数使用未注册的 ptype "string"（规范类型表为 "str"） |
| AUD-64020 | Suggestion | process | `src/features/composite/nodes/clamp_node.py:86-89` | high | min/max 未校验 min<=max，也未做数值强转 |
| AUD-6500 | Critical | bug | `src/features/composite/nodes/color_correct_node.py:140` | medium | color_correct 效果参数集与 GPU 分发器/着色器契约不匹配，任一参数改动即导致节点输出为空 |
| AUD-6501 | Suggestion | process | `src/features/composite/nodes/color_correct_node.py:141` | medium | 评估时未按 PARAM_DEFS 边界对参数做 clamp/类型验证 |
| AUD-6502 | Suggestion | process | `src/features/composite/nodes/color_node.py:113-116` | high | to_dict 覆盖为无操作，属误导性死代码 |
| AUD-6503 | Suggestion | process | `src/features/composite/nodes/compare_node.py:105-106` | high | 单输入透传分支可返回未校验 is_valid 的 TextureResult |
| AUD-6504 | Major | bug | `src/features/composite/nodes/constant_node.py:568-569` | medium | width/height 只钳制下限，未按 ParamDef 上限截断，可触发超大 GPU 纹理分配 |
| AUD-6505 | Suggestion | process | `src/features/composite/nodes/constant_node.py:199-210` | medium | legacy sRGB 迁移在已含原子 color/color2 的文件上会被 _sync_color_channel_mirrors 撤销 |
| AUD-6506 | Suggestion | process | `src/features/composite/nodes/constant_node.py:405-423` | high | _content_hash() 为死代码，与实际使用的 content_sig 并存两套口径 |
| AUD-6507 | Suggestion | process | `src/features/composite/nodes/constant_node.py:514-517` | medium | _render_gradient_gpu 更改全局 GL 状态（FBO 绑定/viewport）后不恢复 |
| AUD-6508 | Suggestion | process | `src/features/composite/nodes/copy_channel_node.py:30-31` | high | HELP_TEXT 参数名与实际参数名不一致 |
| AUD-6509 | Suggestion | process | `src/features/composite/nodes/cornerpin_node.py:129-132` | medium | _compute_inverse_homography 与 _apply_homography_point 无生产调用（死代码） |
| AUD-6510 | Suggestion | process | `src/features/composite/nodes/crop_node.py:171` | high | 非重采样路径 crop 区与输入不相交时回退到未裁剪 bbox，宣称不存在的数据窗口 |
| AUD-6511 | Suggestion | process | `src/features/composite/nodes/crop_node.py:126-128` | high | crop 尺寸未按 ParamDef 上限截断，越界值直通 GPU 分配 |
| AUD-6512 | Suggestion | process | `src/features/composite/nodes/cryptomatte_select_node.py:146-150` | medium | remove_selection 守卫条件把 id 值当作下标比较（值/索引混淆） |
| AUD-6513 | Suggestion | process | `src/features/composite/nodes/cryptomatte_select_node.py:133-134` | high | 选择数达 MAX_SELECTED=32 后静默丢弃新选择，无任何反馈通道 |
| AUD-6600 | Suggestion | process | `src/features/composite/nodes/cs_transform_node.py:68-77` | medium | `direction` 参数未做合法性校验，非法持久化值被静默当作 linear_to_srgb |
| AUD-6601 | Suggestion | process | `src/features/composite/nodes/deep_merge_node.py:140-147` | medium | 单输入旁路分支可能把无效 TextureResult 当作 result 下发 |
| AUD-6602 | Suggestion | process | `src/features/composite/nodes/deep_merge_node.py:208-223` | medium | `_bypass_evaluate` 中 BYPASS_MAPPING 分支之后的代码不可达（死代码） |
| AUD-6603 | Critical | risk | `src/features/composite/nodes/deflicker_node.py:308-324, 418-426, 453-459` | medium | 每帧输出纹理没有任何所有者且从不释放——GPU 显存随评估帧数无界增长 |
| AUD-6604 | Major | bug | `src/features/composite/nodes/deflicker_node.py:327-343, 366-375` | medium | 节点持有 GL 资源（_luma_tex、两个 program、VBO）但未实现 cleanup()；ctx 切换时旧资源直接覆盖不释放 |
| AUD-6605 | Major | bug | `src/features/composite/nodes/deflicker_node.py:49, 254-265` | medium | 环形缓冲上限 96 帧全分辨率 float32 平面（RGB 模式 ×3 通道），峰值内存过高 |
| AUD-6606 | Suggestion | process | `src/features/composite/nodes/defocus_node.py:143-145` | high | 预览半径下限 `max(5.0, max_radius * 0.4)` 恒不生效（死逻辑） |
| AUD-6607 | Major | bug | `src/features/composite/nodes/deformer3d_node.py:133-152, 418-435` | medium | 变形缓存签名与 mesh program 不含 GL context 身份，上下文更换后复用已死 VAO/program |
| AUD-6609 | Major | bug | `src/features/composite/nodes/denoise_node.py:46-49, 155-160` | medium | `quality` 档位未写入 `_effect_params`，合成页 GPU 派发完全忽略质量设置 |
| AUD-6610 | Suggestion | process | `src/features/composite/nodes/denoise_node.py:84-87, 100-102` | medium | `compute_input_roi` 的 radius 未做 int 化，与 `_tile_padding` 类型处理不一致 |
| AUD-6611 | Suggestion | process | `src/features/composite/nodes/denoise_node.py:50-51, 132-133` | medium | `luminance_only` 与 `chroma_only` 可同时为 True，无互斥约束 |
| AUD-6612 | Suggestion | process | `src/features/composite/nodes/depth_matte_node.py:89-94` | high | `range_start > range_end` 未做归一化（swap），反转区间输出几乎为空的 matte |
| AUD-6700 | Suggestion | process | `src/features/composite/nodes/difference_keyer_node.py:130-132` | high | option 参数值未在节点侧校验，任意字符串静默透传渲染器 |
| AUD-6701 | Suggestion | process | `src/features/composite/nodes/dissolve_node.py:105-108` | high | 单输入有效分支未校验 input_b.is_valid，可把无效 TextureResult 当结果下发 |
| AUD-6702 | Suggestion | process | `src/features/composite/nodes/dissolve_node.py:124-133（对照 base_evaluator.py:2489-2501）` | low | marker 占位尺寸用纹理原始宽高，与渲染端画布感知尺寸逻辑不一致 |
| AUD-6703 | Suggestion | process | `src/features/composite/nodes/face_smooth_node.py:62-68（对照 70-74）` | medium | compute_input_roi 读 self.parameters 而非入参 params，与 _tile_padding 数据源不一致 |
| AUD-6704 | Suggestion | process | `src/features/composite/nodes/face_smooth_node.py:83-86` | medium | error_message 只置不清，mask 恢复后陈旧错误残留 |
| AUD-6705 | Major | risk | `src/features/composite/nodes/face_track_node.py:127-129、113-116、86-88（对照 base_node.py:643-651、912-919）` | medium | rerun_on_evaluate 在求值中途 set_parameter，自我置脏并触发状态回调（渲染线程） |
| AUD-6706 | Suggestion | risk | `src/features/composite/nodes/face_track_node.py:26-31` | high | 进程级全局 _DETECTOR 为无锁可变共享状态 |
| AUD-6707 | Suggestion | process | `src/features/composite/nodes/face_track_node.py:104-112` | medium | 检测失败仅写日志与 detection_state 字符串，对 UI 不可见 |
| AUD-6708 | Critical | bug | `src/features/composite/nodes/file_output_node.py:227（import 在 252；模块顶部无 moderngl 导入）` | medium | _readback_texture 在函数内 import moderngl（L252）之前引用 moderngl.LINEAR（L227）→ 每次导出必然 UnboundLocalError，写盘功能全灭 |
| AUD-6709 | Major | risk | `src/features/composite/nodes/file_output_node.py:246-258、272-282` | medium | VBO/VAO 不在 finally 释放，异常路径泄漏 GL 缓冲区 |
| AUD-6710 | Major | bug | `src/features/composite/nodes/file_output_node.py:344-346、369-373` | medium | 非 EXR 一律 clip(0,1)：TIFF+32bit 浮点导出丢失 HDR 高光（与自身注释矛盾） |
| AUD-6711 | Major | bug | `src/features/composite/nodes/file_output_node.py:349-377、422-424、439-442` | medium | 格式/位深/通道组合无前置验证：JPEG+RGBA、JPEG+16/32bit、PNG+32bit 等必然失败组合到 imwrite 才报错且原因被吞 |
| AUD-6712 | Suggestion | process | `src/features/composite/nodes/file_output_node.py:228-231、262` | low | readback 改绑 FBO/viewport 后不恢复调用方 GL 状态 |
| AUD-6713 | Suggestion | process | `src/features/composite/nodes/file_output_node.py:23` | high | `import traceback` 未使用（L23） |
| AUD-6800 | Major | bug | `src/features/composite/nodes/frame_interpolate_node.py:201（另 161-163 同类分支）` | medium | 节点自建输出纹理不经 resource_pool/fb_manager 跟踪，任何释放路径都无法回收 |
| AUD-6801 | Major | risk | `src/features/composite/nodes/frame_interpolate_node.py:111-127（_ensure_blend_resources），全文件无 cleanup 定义` | medium | 未实现 cleanup()：blend program/VBO 与 ONNX session 随节点移除/上下文切换泄漏 |
| AUD-6802 | Suggestion | perf-opportunity | `src/features/composite/nodes/frame_interpolate_node.py:249-261` | high | RIFE 模型路径保留每帧双全帧 GPU→CPU 回读 + CPU→GPU 上传 |
| AUD-6803 | Critical | bug | `src/features/composite/nodes/geo3d_node.py:149-167（释放重建）、169-182（VAO 复用早退）、274-279（调用顺序）` | medium | 实例参数变化时缓存 VAO 仍绑定已释放的旧 instance VBO，按新实例数渲染产生越界属性取数 |
| AUD-6804 | Major | bug | `src/features/composite/nodes/geo3d_node.py:290` | medium | _geo_revision 每次求值无条件自增，Scene3D 内容签名缓存恒 miss |
| AUD-6805 | Suggestion | process | `src/features/composite/nodes/geo3d_node.py:184、249` | high | primitive 参数未经白名单校验直传 get_primitive，损坏工程文件导致节点空输出 |
| AUD-6806 | Suggestion | process | `src/features/composite/nodes/glow_node.py:60-69、93-95（对照 filter_evaluator.py:229-237）` | medium | compute_output_bbox 声明按 radius 扩张数据窗，实际派发输出不扩张，规划与结果不一致 |
| AUD-6807 | Suggestion | process | `src/features/composite/nodes/grade_node.py:72` | high | mask_channels 使用非规范 ptype "string"（全库规范为 "str"） |
| AUD-6808 | Suggestion | process | `src/features/composite/nodes/grade_node.py:36-38、56-58（对照 core/shaders.py:910-933）` | medium | HELP_TEXT 与实现不符：声称 rgba 通道支持与负 gamma 处理，派发着色器均未实现 |
| AUD-6809 | Major | risk | `src/features/composite/nodes/group_node.py:192-202` | medium | from_dict 反序列化 input_mappings/output_mappings/member_node_ids 无结构验证，畸形工程文件在加载期崩溃 |
| AUD-6810 | Suggestion | process | `src/features/composite/nodes/group_node.py:278-284、295-297` | medium | promote/unpromote 直接改 parameters/promoted_params，绕过 set_parameter 的脏状态/修订号提交 |
| AUD-6811 | Suggestion | process | `src/features/composite/nodes/group_node.py:256-267、279-284` | medium | 显式 proxy_name 与既有 promotion 重名时静默覆盖，旧成员 binding 变孤儿 |
| AUD-6812 | Suggestion | process | `src/features/composite/nodes/group_node.py:87、348-352、361-405` | medium | 死代码与遗留路径：_compute_cache_key 无调用方、_internal_eval_cache 从未使用、_last_result 回退永不命中 |
| AUD-6813 | Suggestion | process | `src/features/composite/nodes/group_node.py:55-59、72-73、165-184（对照 to_dict/from_dict）` | medium | group_name/is_expanded/group_color 参数与实例属性双轨状态，面板编辑不生效、持久化以属性为准 |
| AUD-6900 | Major | risk | `src/features/composite/nodes/image_input_node.py:512-515` | medium | 任意参数编辑都会触发渲染线程同步磁盘重解码（纹理内容未变也重读） |
| AUD-6901 | Major | risk | `src/features/composite/nodes/image_input_node.py:503-515` | medium | 图像序列（####/%04d）播放期间每帧在渲染线程同步解码，无法实时预览 |
| AUD-6902 | Major | bug | `src/features/composite/nodes/image_input_node.py:280-285` | medium | 同步上传路径释放/重建自持 GPU 纹理时未设置 `_cache_invalidated_needed`，下游缓存可能绑定已释放/复用的 glo |
| AUD-6903 | Suggestion | process | `src/features/composite/nodes/image_input_node.py:274-291` | high | 上传路径整幅图像双重拷贝，峰值内存翻倍 |
| AUD-6904 | Suggestion | process | `src/features/composite/nodes/image_input_node.py:468-475` | medium | missing_policy=error 且解码失败后无失败退避，每次 evaluate 重复同步解码尝试 |
| AUD-6905 | Suggestion | process | `src/features/composite/nodes/image_input_node.py:583-585` | medium | 解码失败/回退占位时 `_cpu_color_space_hint` 不清理，占位纹理沿用旧文件色彩空间标记 |
| AUD-6906 | Suggestion | process | `src/features/composite/nodes/image_input_node.py:217, 238, 244, 449, 554` | high | 多个公开方法缺少类型注解 |
| AUD-6907 | Suggestion | process | `src/features/composite/nodes/image_input_node.py:505` | high | 内联 `"#" in path or "%0" in path` 与 `has_frame_pattern` 重复实现 |
| AUD-6908 | Suggestion | process | `src/features/composite/nodes/huecorrect_node.py:66-74` | high | 默认值在 PARAM_DEFS / __init__ / has_changes 三处重复，存在漂移风险 |
| AUD-6909 | Suggestion | doc-gap | `src/features/composite/nodes/invert_node.py:60-85` | medium | HDR 反相按 1.0-c 定义产生负值，HDR 语义未文档化/无 pivot |
| AUD-6910 | Suggestion | process | `src/features/composite/nodes/keyer_node.py:208-221` | medium | keyer_mode/output_mode 未做白名单校验、gain 无上限钳制 |
| AUD-6911 | Suggestion | process | `src/features/composite/nodes/lens_distortion_node.py:192-201` | medium | `_effect_params` 原样透传 `self.parameters`，畸变系数未按 ParamDef 范围钳制 |
| AUD-6912 | Suggestion | process | `src/features/composite/nodes/lens_flare_node.py:124` | medium | `color` 参数未验证/归一化，非数值形态导致求值抛异常 |
| AUD-6913 | Major | bug | `src/features/composite/nodes/light3d_node.py:183` | medium | `from_dict` 旧参数迁移分支不可达，v6 场景灯光静默回落默认值 |
| AUD-6914 | Suggestion | process | `src/features/composite/nodes/light3d_node.py:323-326` | high | cleanup 与 IBL 降级路径宽泛 `except Exception: pass` 静默吞异常 |
| AUD-7000 | Suggestion | doc-gap | `src/features/composite/nodes/luma_keyer_node.py:27-34（对照 :46-52）` | high | HELP_TEXT 参数名与实际参数定义不一致（文档过期） |
| AUD-7001 | Suggestion | process | `src/features/composite/nodes/lut_node.py:64-65、124-128` | medium | `_lut_mtime` 只写不读，LUT 文件内容变化不会触发任何缓存失效 |
| AUD-7002 | Suggestion | process | `src/features/composite/nodes/lut_node.py:131-180` | medium | `get_lut_info`/`_parse_lut_size` 全库无调用者；整文件 `readlines()` 且以宽 except 吞掉解析错误 |
| AUD-7003 | Suggestion | process | `src/features/composite/nodes/lut_node.py:131` | high | 返回类型注解 `Dict[str, any]` 使用内置函数 `any` 而非 `typing.Any` |
| AUD-7004 | Major | bug | `src/features/composite/nodes/mask_merge_node.py:70-81、115-125` | medium | 输出 bbox/尺寸声明与分发端实际输出不一致；尺寸选择含恒真死条件 |
| AUD-7005 | Suggestion | process | `src/features/composite/nodes/mask_node.py:83-84（对照 :116）` | medium | ParamDef 默认值（"matte"）与实例默认值（"result"）不一致，属性面板"重置默认"行为与新建节点不同 |
| AUD-7006 | Major | bug | `src/features/composite/nodes/matte_cleanup_node.py:89-90` | medium | blur 模式 ROI 扩张量约为实际高斯半径的一半，ROI/分块渲染边缘欠采样 |
| AUD-7007 | Suggestion | process | `src/features/composite/nodes/matte_node.py:49-60（对照 luma_keyer_node.py:46-52）` | medium | 与 LumaKeyerNode 功能重复且参数命名分叉 |
| AUD-7008 | Major | bug | `src/features/composite/nodes/model3d_node.py:394-408、458-461` | medium | 导入失败后无失败粘滞/退避：损坏资产会在每次求值时重新提交完整导入 |
| AUD-7009 | Major | risk | `src/features/composite/nodes/model3d_node.py:776-786（对照 :698-705、:112-114）` | medium | LOD 变体一次性同步构建 3 级，其中 LOD3 在本节点不可达，首次导航帧在渲染线程白白多做一次全网格简化 |
| AUD-7010 | Major | bug | `src/features/composite/nodes/model3d_node.py:827、843、878-881、968-974、1025-1026` | medium | `_do_evaluate` 对约 20 个参数直接 `float()`/`int()` 强转，非数值入参抛异常后节点静默输出空 payload |
| AUD-7011 | Suggestion | process | `src/features/composite/nodes/model3d_node.py:713-717、731-734` | high | `_resolve_lod_level` 中 interactive_lod 设置检查重复，第二处为不可达死代码 |
| AUD-7012 | Suggestion | process | `src/features/composite/nodes/model3d_node.py:233-234` | high | `__init__` 中 `import time as _time_mod` 未使用 |
| AUD-7013 | Suggestion | process | `src/features/composite/nodes/model3d_node.py:1133-1162` | medium | `cleanup()` 未清空 `_mesh_lod_variants` 与部分失败状态字段 |
| AUD-7014 | Suggestion | process | `src/features/composite/nodes/model3d_node.py:892-926` | medium | 关闭 `use_imported_model` 或清空路径后，已加载模型的 GL 资源（VAO/VBO/程序）不释放 |
| AUD-7015 | Suggestion | process | `src/features/composite/nodes/model3d_node.py:134-145（结合 base_node.py:977-980 默认旁路）` | medium | disabled 直通将图像 TextureResult 映射到 scene 类型的 display 端口，语义错误 |
| AUD-7101 | Major | bug | `src/features/composite/nodes/noise_node.py:112-126（佐证：src/features/composite/evaluators/filter_evaluator.py:392、src/features/composite/core/shaders.py:4659）` | medium | `animate`/`animation_speed` 参数完全无效：`_effect_params` 从不提供 `time`，下游 `u_time` 恒为 0.0 |
| AUD-7102 | Suggestion | process | `src/features/composite/nodes/noise_node.py:17` | high | 未使用的导入 `Any`；`noise_type`/`output_channels` 在本层不验证合法性 |
| AUD-7103 | Suggestion | process | `src/features/composite/nodes/normalize_node.py:131-136` | high | `channels` 字符串未验证，任意非法字符串生成全零 channel_mask，归一化静默变 no-op |
| AUD-7104 | Suggestion | process | `src/features/composite/nodes/output_node.py:106-117` | high | `_parse_bg_color` 接受非有限浮点（"nan" 等），NaN 经 min/max 钳制后变成 1.0，背景静默变为不透明白色 |
| AUD-7105 | Suggestion | process | `src/features/composite/nodes/output_node.py:554-566（acquire 在 555，program 编译在 562-566）；其余 pass 的默认 f2 获取：367-370、433-436、489-492、613-616` | medium | 全部显示 pass 固定使用默认 RGBA16F 临时纹理（不随 render_mode 分层）；`_apply_color_space` 使用已弃用的 `acquire_texture` 别名且在着色器编译之前获取纹理 |
| AUD-7106 | Suggestion | process | `src/features/composite/nodes/output_node.py:687-696` | high | `_resize` 仅双线性 passthrough 采样，无 mipmap/预过滤，大幅缩小（>2×）时混叠 |
| AUD-7107 | Major | doc-gap | `src/features/composite/nodes/particle_node.py:433-452（列表推导在 441-446）` | medium | 湍流力场为逐粒子 Python 循环，复杂度 O((frame−t0)×N×2)，与 docstring "fully vectorized in numpy" 不符；可达参数下单个求值数十秒 |
| AUD-7108 | Suggestion | process | `src/features/composite/nodes/particle_node.py:460、473` | high | CPU 回退路径 `_rasterize` 每次分配全画布 `np.mgrid`（int64×2）+ float32 画布，4K 下峰值约 400MB |
| AUD-7109 | Suggestion | process | `src/features/composite/nodes/particle_node.py:734-741` | high | GPU 失败仅记录一次告警，`_gpu_fail_logged` 永久置位后所有后续异常静默回退 CPU |
| AUD-7110 | Critical | risk | `src/features/composite/nodes/pointcloud3d_node.py:169-178` | medium | 每次求值直接 `gl.texture(...)` 新建纹理，节点不持有引用、不经 resource_pool、缓存不接管释放 → GPU 纹理泄漏 |
| AUD-7111 | Suggestion | process | `src/features/composite/nodes/pointcloud3d_node.py:179-181` | high | 上传失败仅 `logger.warning`，未设置 `error_message`（其前一行刚被清空），UI 无错误状态 |
| AUD-7200 | Suggestion | doc-gap | `src/features/composite/nodes/ramp_node.py:6-11, 140, 143` | medium | ramp_type/extend_mode 未做白名单验证，且模块文档与 RAMP_TYPES 列表不一致 |
| AUD-7201 | Suggestion | process | `src/features/composite/nodes/ramp_node.py:141-142` | high | 输出尺寸只做下限钳制，未按 ParamDef 上限（16384）钳制 |
| AUD-7202 | Major | bug | `src/features/composite/nodes/reformat_node.py:251-263, 266-278` | medium | 同尺寸直通路径新建 TextureResult 丢失 revision/bit_depth/frame_metadata/AOV 等元数据 |
| AUD-7203 | Major | bug | `src/features/composite/nodes/reformat_node.py:147-168（对照 transform_evaluator.py:318-346）` | medium | compute_output_bbox 以 bbox 尺寸为缩放基准且用整除，与实际渲染 _apply_reformat 以整图尺寸缩放不一致 |
| AUD-7204 | Major | bug | `src/features/composite/nodes/reformat_node.py:182-216, 218-225（对照 base_node.py:1149-1150 默认实现）` | medium | compute_input_roi 逆映射忽略居中偏移、distort 用平均缩放，且覆盖后丢失 requires_roi_expansion 的滤波 padding |
| AUD-7205 | Suggestion | process | `src/features/composite/nodes/reformat_node.py:259, 274` | high | 直通分支中 preserve_bbox 条件两个分支完全相同（死条件） |
| AUD-7206 | Suggestion | process | `src/features/composite/nodes/reformat_node.py:186（对照 104、136、248）` | high | resize_mode 缺省值在各辅助函数间不一致（fit vs no_resize） |
| AUD-7207 | Suggestion | process | `src/features/composite/nodes/reformat_node.py:92-96, 238-243` | high | 未知 preset 名静默回落到输入尺寸，无告警 |
| AUD-7208 | Major | bug | `src/features/composite/nodes/relight_node.py:148-149` | medium | color 参数未经验证即按下标取值/float()，损坏的持久化数据可令求值抛异常、节点静默输出空 |
| AUD-7209 | Suggestion | doc-gap | `src/features/composite/nodes/retime_node.py:9-10, 78-82（对照 base_evaluator.py:2163-2166）` | medium | 模块 docstring 宣称光流插值/帧混合，实现实为直通 + 元数据（文档过度承诺） |
| AUD-7210 | Major | bug | `src/features/composite/nodes/roto_shape_node.py:179-216（关键 181-183, 193-207, 212-216）` | medium | set_parameter 对 roto_point_* 先提交后验证 + 静默 except，失败时 JSON 与数值通道发散，后续求值可再抛异常 |
| AUD-7211 | Suggestion | process | `src/features/composite/nodes/roto_shape_node.py:275（对照 node_capabilities.py:835-843）` | medium | node_capabilities 的 effect_type="roto_shape" 与节点 marker "_effect_type": "roto" 不一致 |
| AUD-7212 | Suggestion | process | `src/features/composite/nodes/roto_shape_node.py:126-137（对照 11-16 默认路径、218-252 parse_points 无范围限制）` | medium | 动态生成的点位 ParamDef 硬钳 [0,1]，与 JSON 点位可越界的事实不一致，动画化越界点会被钳断 |
| AUD-7300 | Major | risk | `src/features/composite/nodes/scene3d_node.py:2999` | medium | 正交视图 depth renderbuffer 无任何持有/释放记账，每次 quad 视图重建泄漏 3 个 GL renderbuffer |
| AUD-7301 | Major | risk | `src/features/composite/nodes/scene3d_node.py:2386` | medium | ID-write 与 outline VAO 自建的 VBO/IBO 无释放记账，目标重建/清理时泄漏 |
| AUD-7302 | Major | bug | `src/features/composite/nodes/scene3d_node.py:1288` | medium | `_lod_vao_cache` 全生命周期从不释放（含 MeshVBOPool 引用计数不减） |
| AUD-7303 | Major | bug | `src/features/composite/nodes/scene3d_node.py:1393` | medium | 深度/ID/阴影 pass 的 VAO 缓存仅按 node_name 键控，重名 mesh 复用到错误几何 |
| AUD-7304 | Major | bug | `src/features/composite/nodes/scene3d_node.py:1369` | medium | 深度 pass 硬编码 near=0.1/far=1000，与外接 Camera3D 可调裁剪面不一致 → 线性深度输出错误 |
| AUD-7305 | Major | bug | `src/features/composite/nodes/scene3d_node.py:2567` | medium | ID/outline/shadow pass 在自身导入路径使用 bind-pose 世界矩阵，动画播放时拾取/描边/阴影错位 |
| AUD-7306 | Major | bug | `src/features/composite/nodes/scene3d_node.py:2621` | medium | Geo3D 对象从不进入 GPU ID pass 与 outline pass（procedural 回退 VAO 恒为 None），但 pick_map 仍为其分配 id |
| AUD-7307 | Suggestion | process | `src/features/composite/nodes/scene3d_node.py:2632` | medium | `_render_id_pass` 的 `_p7_id_pick_info` 赋值意外落在 finally 块内；异常路径会以 UnboundLocalError 掩盖根因 |
| AUD-7308 | Suggestion | process | `src/features/composite/nodes/scene3d_node.py:4309` | medium | `_inst_wmin/_inst_wmax` 的赋值守卫与使用守卫不一致（潜在 UnboundLocalError，当前路径不可达） |
| AUD-7309 | Suggestion | perf-opportunity | `src/features/composite/nodes/scene3d_node.py:4921` | medium | 性能诊断统计失真：`vbo_cache_hit` 永不为 False、`model_cache_hit` 恒 None |
| AUD-7310 | Suggestion | risk | `src/features/composite/nodes/scene3d_node.py:1728` | medium | GL 状态泄漏与冗余宽泛 except |
| AUD-7311 | Suggestion | process | `src/features/composite/nodes/scene3d_node.py:3367` | medium | 参数输入验证不完整：尺寸无上限钳制、浮点参数无 NaN 防护 |
| AUD-7312 | Suggestion | risk | `src/features/composite/nodes/scene3d_node.py:3300` | medium | `redraw_with_view` 在渲染线程直写 `self.parameters`，绕过参数提交/undo 路径 |
| AUD-7313 | Suggestion | process | `src/features/composite/nodes/scene3d_node.py:1318` | medium | 类型注解缺失（多处） |
| AUD-7400 | Suggestion | process | `src/features/composite/nodes/set_alpha_node.py:89` | high | mode/value 参数未做白名单与范围校验，非法值静默退化为 constant 模式 |
| AUD-7401 | Suggestion | doc-gap | `src/features/composite/nodes/set_alpha_node.py:34` | high | HELP_TEXT 文档的参数名 `constant_value` 与实际参数 `value` 不一致 |
| AUD-7402 | Suggestion | process | `src/features/composite/nodes/sharpen_node.py:31` | high | HELP_TEXT 声称 amount 范围 0.0–2.0，PARAM_DEFS 上限却是 10.0 |
| AUD-7403 | Suggestion | process | `src/features/composite/nodes/sharpen_node.py:86` | medium | radius/amount/threshold 求值路径未做类型与范围校验（含 `_tile_padding` 的 int 转换） |
| AUD-7404 | Major | bug | `src/features/composite/nodes/switch_node.py:73` | medium | `compute_output_bbox` 未将 `which` 转 int，浮点值时端口键永远失配，bbox 回退到首个有效输入 |
| AUD-7405 | Suggestion | process | `src/features/composite/nodes/switch_node.py:48` | medium | `_update_ports` 重建端口后不重建 `_port_revisions`，端口 revision 跟踪全链失效 |
| AUD-7407 | Major | bug | `src/features/composite/nodes/timeline_clip_input_node.py:142` | medium | 宽泛 `except Exception` 吞掉 provider 全部异常并返回空结果，export 模式下静默丢帧 |
| AUD-7408 | Suggestion | process | `src/features/composite/nodes/timeline_clip_input_node.py:67` | medium | `_cached_texture`/`_revision` 为死状态机：生产代码从不注入非空纹理，`_revision` 无任何消费方 |
| AUD-7409 | Suggestion | process | `src/features/composite/nodes/timeline_clip_input_node.py:127` | high | 每次 evaluate 跨 feature 导入私有符号 `_to_texture_result` |
| AUD-7410 | Suggestion | process | `src/features/composite/nodes/topos_color_node.py:44` | medium | 类未定义 PARAM_DEFS，`bypass`/`intensity` 等参数无 UI 元数据、校验与动画声明 |
| AUD-7411 | Major | bug | `src/features/composite/nodes/text_node.py:1336` | medium | 每次重栅格化重复执行两次完整文本测量布局（`_measure_text_bbox` 被调用两遍） |
| AUD-7412 | Major | bug | `src/features/composite/nodes/text_node.py:1550` | medium | PIL 回退路径将 HDR 颜色直接乘 255，fill>1.0 时颜色分量越界，headless 导出颜色损坏 |
| AUD-7413 | Suggestion | risk | `src/features/composite/nodes/text_node.py:1125` | medium | caption 覆盖直接改写共享 `self.parameters["text"]`，依赖"渲染实例分离 + 单线程求值"的隐式假设 |
| AUD-7414 | Suggestion | process | `src/features/composite/nodes/text_node.py:1623` | high | 测量函数 `_measure_text_bbox_pil` 以副作用改写 `font_size` 参数，绕过 set_parameter 状态机 |
| AUD-7415 | Suggestion | process | `src/features/composite/nodes/text_node.py:1456` | medium | PIL 字体缓存设计：每个新 (family,size,bold) 键都全盘 os.walk 系统字体目录，类级缓存无上限 |
| AUD-7416 | Suggestion | process | `src/features/composite/nodes/text_node.py:962` | high | `_build_char_placements` 空字形分支的注释与代码矛盾（空白字符是否消耗 stagger 槽位） |
| AUD-7417 | Suggestion | process | `src/features/composite/nodes/text_node.py:526` | high | 空文本/纯空白文本按整画布尺寸分配 RGBA32F 纹理 |
| AUD-7501 | Major | bug | `src/features/composite/nodes/tracker_node.py:70` | medium | `decode_track_data` 键规范化对非数值键抛未捕获异常，违背"容忍损坏"契约 |
| AUD-7502 | Major | perf-opportunity | `src/features/composite/nodes/tracker_node.py:101（存储设计）；触发点 render_scheduler.py:576-577` | medium | `track_data` 以整段 JSON 字符串作为参数存储，调度器每次生成计划都全量序列化+MD5，长跟踪下播放性能退化 |
| AUD-7503 | Suggestion | process | `src/features/composite/nodes/tracker_node.py:55-56, 69-71` | high | `decode_track_data` 在入参为 dict 时原地修改调用方对象 |
| AUD-7504 | Critical | bug | `src/features/composite/nodes/transform_node.py:180-187` | medium | `_build_inverse_matrix` 不是前向矩阵的逆（m01/m10 符号错误），rotation ≠ 0 时 `compute_input_roi` 算出错误 ROI → ROI 渲染取错区域 |
| AUD-7505 | Major | doc-gap | `src/features/composite/nodes/transform_node.py:247（调度器路径用 bbox 尺寸）；337-338、350-352（求值器路径用 image 尺寸）；263-265（docstring 声称一致）；204-206（flip 常量假设 y 底部锚定）` | medium | 调度器侧与求值器侧使用不同的 pivot/坐标系参考：`compute_output_bbox`/`compute_input_roi` 用输入 bbox 尺寸，`_do_evaluate`/GPU 路径用纹理尺寸，与自身 docstrin |
| AUD-7506 | Suggestion | process | `src/features/composite/nodes/transform_node.py:63-70` | medium | 端口在 `super().__init__()` 之后才赋值，`_port_revisions` 为空，`bump_output_revision()` 变 no-op |
| AUD-7507 | Suggestion | process | `src/features/composite/nodes/unknown_placeholder_node.py:95-99` | medium | `from_dict` 的 known_fields 缺 `expression_enabled` 与 `user_label`，占位节点的禁用表达式状态与重命名在重载后丢失 |
| AUD-7508 | Suggestion | process | `src/features/composite/nodes/unknown_placeholder_node.py:66` | high | `position` 反序列化无类型验证，损坏的外部数据会产生错误元组 |
| AUD-7509 | Suggestion | doc-gap | `src/features/composite/nodes/unknown_placeholder_node.py:50-56` | medium | `_do_evaluate` docstring 声称记录警告日志，实际无任何日志调用 |
| AUD-7601 | Critical | bug | `src/features/composite/nodes/video_frame_node.py:1037-1046, 1095, 1484, 1837-1838, 1862-1874` | medium | `_frame_cache` 缓存条目与共享转换 scratch 缓冲对象别名，缓存命中会上屏错误帧/撕裂帧 |
| AUD-7602 | Major | bug | `src/features/composite/nodes/video_frame_node.py:1649-1654, 2322-2328` | medium | `_clear_cache` 对 `_vt_programs` 的 `(prog, vao)` 元组调用 `.release()`，AttributeError 被空 except 吞掉，VT program/VAO 从不释放 |
| AUD-7603 | Major | bug | `src/features/composite/nodes/video_frame_node.py:2052-2057, 2069-2072, 1669-1680, 1681-1688, 2113` | medium | GL 资源释放遗漏：mid FBO 附件纹理从不释放、尺寸变化路径直接丢弃 FBO 不 release、flip program/VBO/IBO 不释放 |
| AUD-7604 | Major | risk | `src/features/composite/nodes/video_frame_node.py:2393, 2429-2448, 2458-2464` | medium | `_upload_frame_gpu_vt` 绑定成功后至 pin 前的异常路径释放 gpu_frame 但不释放 bridge 租约，IOSurfaceGLBridge ring 槽位泄漏 |
| AUD-7605 | Major | perf-opportunity | `src/features/composite/nodes/video_frame_node.py:349-355, 2784-2805` | medium | GC 后备释放机制是死代码：`_gc_resources` 永远为空列表，finalizer 恒空转 |
| AUD-7606 | Suggestion | risk | `src/features/composite/nodes/video_frame_node.py:943-950, 1839, 1870-1871, 1637-1638` | medium | `_vt_cpu_ok`/`_vt_cpu_miss` 跨线程读写无锁，与 `_frame_cache` 的锁纪律不一致 |
| AUD-7607 | Suggestion | process | `src/features/composite/nodes/video_frame_node.py:307, 1622-1625, 1662-1666, 1704-1719` | high | 死代码：`_texture_cache` 从未写入，`_get_cache_key` 无调用者 |
| AUD-7608 | Suggestion | process | `src/features/composite/nodes/video_frame_node.py:2765-2778` | medium | `from_dict` 将 `_meta` 外部数据未经校验直写只读参数，None/异常类型可流入解码请求 |
| AUD-7609 | Suggestion | process | `src/features/composite/nodes/video_frame_node.py:302, 317, 1268, 1484` | high | 类型注解与实际存储不符：`_preload_target` 三元组、`_frame_cache` 混存 `_PlanarFrameData` |
| AUD-7610 | Suggestion | process | `src/features/composite/nodes/video_frame_node.py:452-459, 461-462` | medium | 服务开片路径直接改写 `self.parameters[...]` 绕过 `set_parameter` 版本记账；`_video_metadata` 在 total_frames==0 的新片上残留旧片元数据 |
| AUD-7611 | Suggestion | process | `src/features/composite/nodes/video_frame_node.py:2041-2042, 2097-2125` | medium | `_flip_blit` 吞掉渲染异常后 `_upload_frame_gpu` 仍返回 True，翻转失败时以陈旧内容冒充新帧 |
| AUD-7612 | Suggestion | process | `src/features/composite/nodes/vignette_node.py:81-110` | high | `_do_evaluate` 返回值与注解 `Dict[str, TextureResult]` 不符：混入 `_effect_*` 非纹理键且 `texture_id=-1` 占位 |
| AUD-7613 | Suggestion | process | `src/features/composite/nodes/vignette_node.py:48, 66` | high | `cache_to_disk` 参数定义并初始化但节点内从未使用（死参数） |
| AUD-7700 | Major | risk | `src/features/composite/nodes/volume3d_node.py:190-197` | medium | 每次求值新建 ModernGL 纹理但既不持有也不释放、不经 resource_pool，GL 纹理无主泄漏 |
| AUD-7701 | Critical | bug | `src/features/composite/nodes/volume3d_node.py:184-198` | medium | RGB 已按 alpha 预乘却标记 alpha_premultiplied=False，合成时被二次乘 alpha，渲染结果错误 |
| AUD-7702 | Suggestion | process | `src/features/composite/nodes/volume3d_node.py:134-143` | medium | load_volume 的两个 except 分支代码完全重复，且后者为宽泛 Exception |
| AUD-7703 | Suggestion | process | `src/features/composite/nodes/volume3d_node.py:144,189-201` | high | 纹理上传失败只记 warning，error_message 已被清空，节点呈现"无错误也无输出"的不一致状态 |
| AUD-7704 | Suggestion | risk | `src/features/composite/nodes/volume3d_node.py:75-78,166-177` | medium | output_width/height 上限 8192 × step_count 上限 128 的 CPU gather 无内存预算，极端参数可致渲染线程 MemoryError |
| AUD-7705 | Suggestion | process | `src/features/composite/nodes/volume3d_node.py:154-156` | medium | absorption_color 取自外部项目数据但未做类型/长度验证 |
| AUD-7710 | Major | bug | `src/features/composite/nodes/warp_node.py:158-162（配合 102-104 的 pad/truncate）` | medium | grid_size 变更按扁平索引截断/补零 offsets，不做格点重采样，位移点被重新绑到新格的不同 (row,col)，变形视觉错乱 |
| AUD-7711 | Suggestion | process | `src/features/composite/nodes/warp_node.py:117-120` | high | from_dict 用宽泛 `except Exception: pass` 吞掉点通道同步失败 |
| AUD-7712 | Suggestion | process | `src/features/composite/nodes/warp_node.py:131-137,158-162` | medium | 网格缩小时旧 warp_point_* 通道键永久残留 parameters 并随 to_dict 持久化 |
| AUD-7713 | Suggestion | process | `src/features/composite/nodes/warp_node.py:61,242-244` | medium | _do_evaluate 等方法类型注解缺失，与 base 及邻节点不一致 |
| AUD-7720 | Suggestion | process | `src/features/composite/nodes/zblur_node.py:90-91` | medium | _tile_padding 用 int() 截断模糊半径，与 shader 实际可达 ceil(radius) 的采样范围差 1px |
| AUD-7800 | Suggestion | process | `src/features/composite/preferences.py:29-31, 174, 196-197（对照 :50 与 src/shared/config/config_manager.py:177）` | medium | Color Engine 默认值文案与实际默认 "ocio" 矛盾（tooltip 与注释声称 builtin 是默认） |
| AUD-7801 | Suggestion | process | `src/features/composite/preferences.py:180-189, 309-320` | medium | OCIO 不可用时禁用项仍可被程序化选中，UI 显示的引擎与实际生效引擎不一致 |
| AUD-7802 | Suggestion | process | `src/features/composite/preferences.py:366-367, 371-372（对照 load() 的 264-267, 283-285）` | medium | restore_defaults 缺少与 load() 一致的类型容错，异常会被对话框静默吞掉导致恢复默认部分生效 |
| AUD-7803 | Suggestion | process | `src/features/composite/preferences.py:184-187` | medium | `model().item()` 依赖 QComboBox 默认 QStandardItemModel 的实现细节，类型层面不成立 |
| AUD-7804 | Suggestion | process | `src/features/composite/preferences.py:64, 67, 358（对照 src/shared/preferences/__init__.py:36 基类` | medium | 类型注解不完整（基类接口对齐） |
| AUD-7900 | Major | risk | `src/features/composite/render/ai_cache_manager.py:169` | medium | put() 在持锁状态下执行 EXR 写盘、全量 manifest 落盘与驱逐删文件，跨线程阻塞渲染路径 |
| AUD-7901 | Major | bug | `src/features/composite/render/ai_cache_manager.py:52` | medium | manifest 整文件覆写 + 存在多实例并发使用，无跨实例同步导致丢更新 |
| AUD-7902 | Suggestion | process | `src/features/composite/render/ai_cache_manager.py:175` | high | put() 未检查 cv2.imwrite 返回值，写失败仍登记 manifest 并返回路径 |
| AUD-7903 | Suggestion | risk | `src/features/composite/render/ai_cache_manager.py:46` | high | manifest 落盘非原子，崩溃损坏后被静默清空，全部缓存文件成孤儿 |
| AUD-7904 | Suggestion | process | `src/features/composite/render/ai_cache_manager.py:43` | medium | _load_manifest 不校验外部 JSON 结构（外部数据未验证） |
| AUD-7905 | Suggestion | process | `src/features/composite/render/ai_cache_manager.py:30` | high | 类型注解缺失/不精确 |
| AUD-7906 | Critical | bug | `src/features/composite/render/ai_compute_manager.py:563` | medium | _generate_preview 使用未导入的 cv2，默认参数下所有标准 AI 节点必然 NameError 失败 |
| AUD-7907 | Critical | bug | `src/features/composite/render/ai_compute_manager.py:241` | medium | _prepare_outpaint_input 使用未导入的 cv2，ai_expand 节点必然 NameError 失败 |
| AUD-7908 | Major | bug | `src/features/composite/render/ai_compute_manager.py:814` | medium | 自定义生成模型失败路径无终态信号，_workers 残留条目使该 cache_key 永久卡死 |
| AUD-7909 | Major | bug | `src/features/composite/render/ai_compute_manager.py:1155` | medium | 并发上限驱逐的 worker 被静默丢弃，无任何终态信号反馈 |
| AUD-7910 | Major | bug | `src/features/composite/render/ai_compute_manager.py:506` | medium | 取消以 RuntimeError 形式进入通用异常分支，被当作失败误报给 UI |
| AUD-7911 | Major | risk | `src/features/composite/render/ai_compute_manager.py:1271` | medium | _on_worker_stale 弹出 worker 后未 deleteLater，QThread 可能在仍在运行时被 Python GC 销毁（竞态崩溃） |
| AUD-7912 | Major | risk | `src/features/composite/render/ai_compute_manager.py:1193` | medium | cancel/cancel_all/cancel_stale_requests 持管理器锁同步 wait(1000-3000ms)×N，阻塞渲染线程的并发调用 |
| AUD-7913 | Major | risk | `src/features/composite/render/ai_compute_manager.py:545` | medium | ONNX session 缓存无上限、无失效，GPU 显存随模型切换累积 |
| AUD-7914 | Major | bug | `src/features/composite/render/ai_compute_manager.py:624` | medium | tile/overlap 边界无输入验证：overlap>=tile_size 抛 ValueError；overlap==1 产生 NaN 权重黑缝 |
| AUD-7915 | Suggestion | process | `src/features/composite/render/ai_compute_manager.py:104` | high | run() 中无使用的惰性导入 AIInferenceWorker |
| AUD-7916 | Suggestion | risk | `src/features/composite/render/ai_compute_manager.py:1118` | high | get_preview_result 读取 _workers 未持锁，与其余访问方式不一致 |
| AUD-7917 | Suggestion | process | `src/features/composite/render/ai_compute_manager.py:1251` | high | _on_worker_preview 写入 _preview_results 绕过 MAX_PREVIEW_RESULTS FIFO 上限 |
| AUD-7918 | Suggestion | risk | `src/features/composite/render/ai_compute_manager.py:926` | high | 工作线程中 np.random.seed 污染进程级全局 RNG |
| AUD-7919 | Suggestion | process | `src/features/composite/render/ai_compute_manager.py:88` | high | 类型注解缺失/含糊 |
| AUD-7920 | Suggestion | perf-opportunity | `src/features/composite/render/cache_manager.py:671` | medium | 逐条驱逐全量重扫 + 每轮 O(n) 内存汇总，驱逐复杂度 O(n²) |
| AUD-7921 | Suggestion | process | `src/features/composite/render/cache_manager.py:759` | high | _current_node_ids 为未在 __init__ 声明的动态属性，未设置时陈旧清理静默失效 |
| AUD-7922 | Suggestion | process | `src/features/composite/render/cache_manager.py:314` | medium | cache key 内嵌完整 upstream 元组字符串，键体量大且每次驱逐全串 split |
| AUD-80001 | Critical | bug | `src/features/composite/render/composite_export_renderer.py:1135-1137（同型代码 989-990、1240-1242）` | medium | 瓦片渲染跨瓦片复用首瓦片裁剪后的 SOURCE 缓存，大图导出/大帧预览静默产错图 |
| AUD-80002 | Major | risk | `src/features/composite/render/composite_export_renderer.py:542-691` | medium | render_gpu_handle 缺少一次性模式的 cleanup 纪律，非持久调用方泄漏全部 GL 资源 |
| AUD-80003 | Major | doc-gap | `src/features/composite/render/composite_export_renderer.py:353-356` | medium | render_and_readback_passes：render_passes=None 且 srgb_u8 时 AOV 也走 sRGB 读回，违反自身文档契约 |
| AUD-80004 | Suggestion | process | `src/features/composite/render/composite_export_renderer.py:1158-1261` | high | `_render_tiled` 为无调用死代码，且 TileContext 构造已与现行瓦片协议漂移 |
| AUD-80005 | Suggestion | process | `src/features/composite/render/composite_export_renderer.py:309-311（cleanup 置空见 1512）` | high | `_finish_trace` 在 cleanup 之后读 ctx.info，一次性导出路径设备标识恒为空 |
| AUD-80006 | Suggestion | process | `src/features/composite/render/composite_export_renderer.py:547、122` | high | `render_gpu_handle` 的 render_mode 注解与默认值不符；`_wrapped_context` 属性从未被读取 |
| AUD-80007 | Suggestion | process | `src/features/composite/render/composite_export_renderer.py:1414-1417、1429-1430` | medium | srgb_u8 读回在 shader 缺失时静默降级为 linear f32，输出 dtype 契约悄悄改变 |
| AUD-80008 | Suggestion | process | `src/features/composite/render/composite_export_renderer.py:1150、1255` | high | 用 `'tiles' in dir()` 探测局部变量绑定，反模式且脆弱 |
| AUD-80010 | Suggestion | process | `src/features/composite/render/composite_frame_renderer.py:368-371` | medium | 时间线查找失败时静默回退到字典中第一个 timeline，多时间线工程可能取错源 |
| AUD-80011 | Suggestion | perf-opportunity | `src/features/composite/render/composite_frame_renderer.py:242-247（早退路径 160-163、171-175 只清 last_diagnostics）` | high | 异常/早退路径不清 `last_frame_trace`，失败帧携带上一帧的性能归因 |
| AUD-80012 | Suggestion | process | `src/features/composite/render/composite_frame_renderer.py:36、42、92` | high | 未使用导入 `Dict`、`TextureResult`；内部渲染器类型退化为 Any |
| AUD-80013 | Suggestion | process | `src/features/composite/render/composite_frame_renderer.py:508-526` | medium | `collect_async_preview(wait=True)` 的"终帧正确性"契约被 pbo_readback 中重复定义的 get_latest 削弱 |
| AUD-80020 | Suggestion | risk | `src/features/composite/render/composite_graph_render_service.py:243-252（读取侧 239-241）` | high | telemetry 列表只增不减，长时间运行内存无界增长 |
| AUD-80021 | Suggestion | process | `src/features/composite/render/composite_graph_render_service.py:43-56、74、252` | high | `GpuFrameHandle` dataclass 与 `_revision_counter` 为死代码 |
| AUD-80022 | Suggestion | process | `src/features/composite/render/composite_graph_render_service.py:71-72、167-176` | medium | `gl_context` 仅作能力开关从未被绑定使用，API 语义有误导 |
| AUD-80023 | Suggestion | process | `src/features/composite/render/composite_graph_render_service.py:256-260` | high | cleanup 使用无日志的宽泛 `except Exception: pass` |
| AUD-81001 | Major | bug | `src/features/composite/render/composite_render_output.py:517-532（串行）与 957-998（并行视频）、1076-1104（并行序列）` | medium | 串行与并行导出路径的"中途取消"语义相反：串行保留部分产物并报 success，并行删除产物并误报 "Render failed" |
| AUD-81002 | Major | bug | `src/features/composite/render/composite_render_output.py:632-692（串行循环无 try/except）、1061-1071（并行 stage 的 finally 只清理 GL renderer）` | medium | render_image_sequence 无异常路径清理：渲染/保存抛异常时已写帧残留在磁盘，与 render_video 的失败清理不对称 |
| AUD-81003 | Major | risk | `src/features/composite/render/composite_render_output.py:847-856（安全网）、964/983/990（每条路径都会执行）；联动 src/features/composite/render/parallel_frame_render.py:261-262（` | medium | GL 安全网 `_cleanup_parallel_renderers` 在调用者线程销毁可能仍被 worker 线程使用的 renderer（join 仅等 5 秒），cleanup() 无任何并发防护 |
| AUD-81004 | Major | bug | `src/features/composite/render/composite_render_output.py:466-512（串行）、915-930（并行 _sink）` | medium | 渲染为 None 的帧被静默跳过且 pts 连续重排，输出视频时长缩短、后续帧时间轴前移（音画失步风险） |
| AUD-81005 | Suggestion | process | `src/features/composite/render/composite_render_output.py:696-729` | medium | `_render_one_frame` 存在死代码：局部 renderer/timeline_id/clip_id/asset_id 计算后全部弃用，与 `_render_one_frame_with` 内重复实现 |
| AUD-81006 | Suggestion | process | `src/features/composite/render/composite_render_output.py:604、613、661` | medium | `ext` 未做输入验证，且 613 行 walrus 重复归一化导致空/非法 ext 时串行与并行路径产出文件名不一致 |
| AUD-81007 | Suggestion | process | `src/features/composite/render/composite_render_output.py:1153-1157（调用点 395）` | medium | `_fps_to_rational` 对 fps≤0 无防护，0/负帧率会生成 Rational(0,1)/负有理数直传编码器 |
| AUD-81008 | Suggestion | process | `src/features/composite/render/composite_render_output.py:488-500（串行循环内 import + 转换）、880-894（并行 _convert 重复同样逻辑）、1037-1038 与 1054（每帧/每 sink import 自身模块函数与 FileIO）` | medium | 逐帧循环内的重复 import 与串行/并行两份重复的转换打包代码 |
| AUD-81009 | Suggestion | process | `src/features/composite/render/composite_render_output.py:858-863、1000-1005` | high | `_run_parallel_video_stage` 的 `start_frame` 形参从未使用；两个并行 stage 辅助方法缺参数与返回值类型注解 |
| AUD-81010 | Major | risk | `src/features/composite/render/composite_render_worker.py:187-191、220-236` | medium | worker 线程在首个任务结束即 quit，而 export() 只防并发不防复用：第二次 export 的任务会静默丢失（甚至可能在已 deleteLater 的 QObject 上 invokeMethod） |
| AUD-81011 | Suggestion | process | `src/features/composite/render/composite_render_worker.py:65、148-149、153-154` | high | `_cancel_requested` 布尔字段只写不读，为死状态 |
| AUD-81012 | Suggestion | process | `src/features/composite/render/composite_render_worker.py:98-126` | high | run() 对 settings/context 直接下标取键，缺键时报错信息不可读且无前置校验 |
| AUD-8201 | Major | bug | `src/features/composite/render/composite_renderer.py:1661-1664（同类问题 :648）` | medium | AI 结果与 3D 视图快速路径渲染后缺失 `_release_transient_textures`，瞬态纹理延迟归还资源池 |
| AUD-8202 | Major | bug | `src/features/composite/render/composite_renderer.py:2078-2195（核心空转段 :2164-2188）` | medium | PREFETCH_EVALUATE 处理器为可达的空实现，预取功能静默失效 |
| AUD-8203 | Suggestion | risk | `src/features/composite/render/composite_renderer.py:1625（acquire）、:1645（仅成功路径 release）、:1649-1651（except 直接 return）` | medium | 缩略图回读异常路径泄漏 `thumb_tex` |
| AUD-8204 | Suggestion | process | `src/features/composite/render/composite_renderer.py:1100-1114` | high | 播放驱动循环结束时用 `print` 向 stdout 倾倒统计 |
| AUD-8205 | Suggestion | process | `src/features/composite/render/composite_renderer.py:973-981` | medium | `_drive_stopped` 检查本类从不存在的 `_stop_flag`，防线实际为空 |
| AUD-8206 | Suggestion | risk | `src/features/composite/render/composite_renderer.py:1662、:1664` | high | 直接访问调度器私有 `_latest_request_id` 且绕过其锁 |
| AUD-8207 | Suggestion | process | `src/features/composite/render/composite_renderer.py:1054` | medium | 播放驱动循环直接变异"不可变"快照的 `frame_number` |
| AUD-8208 | Suggestion | process | `src/features/composite/render/composite_renderer.py:1162-1176` | medium | 空 file_path 的 image_input 节点每次求值都被标记 LOADING，且永无解码到来 |
| AUD-8209 | Suggestion | process | `src/features/composite/render/composite_renderer.py:653-657（对比 :754-760）` | medium | RENDER_3D_VIEW 快速路径只更新 `_last_viewer_roi` 而不同步 `_active_viewer_roi` |
| AUD-8211 | Major | bug | `src/features/composite/render/compute_shader_manager.py:516（` | medium | compute 分发之间及下游采样前缺少 glMemoryBarrier（imageStore → texture 读取） |
| AUD-8212 | Major | bug | `src/features/composite/render/compute_shader_manager.py:467-473、:509（edge_policy 透传）` | medium | compute 模糊/腐蚀膨胀路径未实现 mirror(2) 边缘策略，静默退化 |
| AUD-8213 | Suggestion | process | `src/features/composite/render/compute_shader_manager.py:115、:411-418` | high | `_output_texture_cache` 为死属性，声明与清理逻辑均无生产者 |
| AUD-8214 | Suggestion | process | `src/features/composite/render/compute_shader_manager.py:463-481（同类 :581-602）` | medium | `apply_blur_compute` 中途异常时中间/输出纹理不归还资源池（无 try/finally） |
| AUD-8300 | Major | bug | `src/features/composite/render/disk_cache_store.py:258` | medium | store() 中 os.path.getsize 无异常保护，并发 clear() 可使其抛未捕获 OSError |
| AUD-8301 | Suggestion | process | `src/features/composite/render/disk_cache_store.py:268-277` | medium | store() 的 json.dumps 未捕获 TypeError，metadata 含不可序列化值时异常逃逸 |
| AUD-8302 | Suggestion | process | `src/features/composite/render/disk_cache_store.py:415-425` | medium | _touch 每次缓存命中全量重写 manifest 且强制 fsync，热路径 I/O 放大 |
| AUD-8303 | Suggestion | risk | `src/features/composite/render/disk_cache_store.py:224-244` | medium | 同 key 切换载荷格式时旧格式文件不清理，磁盘泄漏直至驱逐 |
| AUD-8304 | Suggestion | risk | `src/features/composite/render/disk_cache_store.py:279, 458-473` | medium | 共享单例与统计计数器无锁，注释自证的并行写场景下统计丢失/双实例 |
| AUD-8305 | Suggestion | process | `src/features/composite/render/frame_trace.py:102, 134` | high | to_dict 硬编码丢弃 gpu_time_ms 字段值，未来赋值将被静默忽略 |
| AUD-8306 | Major | risk | `src/features/composite/render/gl_utils.py:250-254, 262-268` | medium | upload_numpy_texture 调用不存在的 resource_pool.allocate_texture，池化路径永不命中且兜底纹理泄漏 |
| AUD-8307 | Major | bug | `src/features/composite/render/gl_utils.py:104-105, 195-199` | high | download_texture 声称保存原 FBO 却从未跟踪，finally 无条件恢复到 ctx.screen 破坏调用方绑定 |
| AUD-8308 | Major | bug | `src/features/composite/render/gl_utils.py:87, 160, 216` | medium | download/upload 声称 BGR/BGRA 但无任何通道交换，单侧使用即红蓝反转 |
| AUD-8309 | Major | bug | `src/features/composite/render/gl_utils.py:201-204, 270-273` | medium | 失败路径宽泛 except 静默返回全零数组，GL 错误被黑图掩盖 |
| AUD-8310 | Major | bug | `src/features/composite/render/gpu_resource_pool.py:1260-1265, 1321-1324` | medium | 小池 LRU 驱逐把 pinned 纹理弹出 _pool_lru 后不恢复，纹理滞留 _pooled_textures 成不可驱逐孤儿 |
| AUD-8311 | Major | bug | `src/features/composite/render/gpu_resource_pool.py:801-819, 325-328` | medium | acquire_video_slot 按 render_mode 取 dtype，release/clear 硬编码 'f2'，key 不匹配导致 EXPORT 槽位无法释放 |
| AUD-8312 | Major | bug | `src/features/composite/render/gpu_resource_pool.py:300-302, 338-348` | medium | VideoSlotPool 槽位只增不减：end_frame 不回收未用槽，无数量/尺寸上限 |
| AUD-8313 | Suggestion | risk | `src/features/composite/render/gpu_resource_pool.py:153-172, 1859-1869` | medium | VRAMMonitor 经 ctypes 直调 GL 无线程防护，公共预算查询 API 可在非渲染线程触发 glGetIntegerv |
| AUD-8314 | Suggestion | process | `src/features/composite/render/gpu_resource_pool.py:552-553, 1523-1530` | high | _stats_format_distribution/_stats_by_usage 初始化后从未更新（死统计字段） |
| AUD-8315 | Suggestion | process | `src/features/composite/render/gpu_resource_pool.py:288-289, 295-302, 335, 360` | high | VideoSlotPool._in_frame/_acquired_this_frame 记录后无任何消费者（死状态机） |
| AUD-8316 | Suggestion | perf-opportunity | `src/features/composite/render/gpu_resource_pool.py:1267-1278, 1303-1319` | high | 成本驱逐每轮全量扫描 _pool_lru，O(n²) 复杂度 |
| AUD-8317 | Suggestion | process | `src/features/composite/render/gpu_resource_pool.py:924-931` | high | _destroy_texture 直访 FramebufferManager 私有成员 _fbo_cache/_active_textures |
| AUD-8318 | Suggestion | process | `src/features/composite/render/gpu_resource_pool.py:143-144, 155` | high | VRAMMonitor 存在死分支与无意义初值 |
| AUD-8319 | Suggestion | process | `src/features/composite/render/metal_compute_bridge.py:59-67` | medium | _kernel 检查-编译-写入非原子，并发首用同名 kernel 重复编译并覆盖 |
| AUD-8400 | Major | bug | `src/features/composite/render/pbo_readback.py:296、341（第一份，死代码）与 :451、497（第二份，生效）` | medium | 同一类内重复定义 poll_ready 与 get_latest，后一份静默覆盖前一份（死代码且两份实现已分叉） |
| AUD-8401 | Major | bug | `src/features/composite/render/pbo_readback.py:519-523（生效版）对照 :376-382（被覆盖的死代码版）` | medium | 生效版 get_latest(wait_newest=True) 忽略 _wait_entry_fence 的超时结果仍映射 PBO，T-P1-71 修复在生效副本中丢失 |
| AUD-8402 | Major | perf-opportunity | `src/features/composite/render/pbo_readback.py:524-532（连同 534-550 的 sweep）` | medium | 生效版 get_latest 非阻塞路径丢弃 poll_ready 刚映射完成的数据并返回 None——异步 lane 的非阻塞收集永远无法交付已完成帧 |
| AUD-8403 | Suggestion | risk | `src/features/composite/render/pbo_readback.py:182-185、212-219` | medium | enqueue 覆写未收集的 pending entry 时不删除其 fence，GLsync 对象泄漏 |
| AUD-8404 | Suggestion | risk | `src/features/composite/render/pbo_readback.py:424-444` | medium | enqueue_sync 直接覆写 ring 槽位：在途 entry 的 fence 泄漏且其请求被静默丢弃 |
| AUD-8405 | Suggestion | process | `src/features/composite/render/pbo_readback.py:205-210、476-484` | medium | fence 不可用（glFenceSync 创建失败 / glClientWaitSync 查询异常）时无等待兜底直接映射在途 PBO |
| AUD-8406 | Suggestion | process | `src/features/composite/render/pbo_readback.py:89（gl_context 无类型）、:552（cleanup 无 -> None）、:304-305（第一份 poll_ready 内 import ctypes/_gl 未使用；该份为死代码，合并修复时一并处理）` | high | 类型注解缺失与未使用 import |
| AUD-8407 | Suggestion | process | `src/features/composite/render/parallel_frame_render.py:199-205（worker 侧）与 :246-252 / :240-244（consumer 侧）` | medium | on_worker_error 对同一错误被调用两次，违反 "called once" 契约 |
| AUD-8408 | Major | bug | `src/features/composite/render/prefetch_scheduler.py:189、50、104、236-241` | medium | velocity 单位不一致（时间戳为秒、阈值按毫秒标定），趋势预测实质失效 |
| AUD-8409 | Suggestion | process | `src/features/composite/render/prefetch_scheduler.py:399-405、101、412-424` | medium | fallback 预测 confidence=0.2 恒低于触发阈值 0.4，该分支永远不会触发 prefetch，且缺 bounds clamp 与统计计数 |
| AUD-8410 | Suggestion | process | `src/features/composite/render/prefetch_scheduler.py:281-283、319-322` | high | 2 值历史的 simple extrapolation 经 history_factor 缩放后置信度上限 0.32，该分支实际不可触发 |
| AUD-8411 | Suggestion | process | `src/features/composite/render/prefetch_scheduler.py:191-206、109、115、338-339` | medium | _param_histories 无淘汰机制 + 类型注解缺失 + 死变量 |
| AUD-8412 | Major | bug | `src/features/composite/render/render_scheduler.py:924-927、1045-1053（消费方 src/features/composite/evaluators/base_evaluator.py:769-772）` | medium | ROI 启用时，非 Output 上游的 viewer_node 预览分支 output_roi 恒为 None → 被判 lod_level=2（四分之一分辨率评估） |
| AUD-8413 | Suggestion | process | `src/features/composite/render/render_scheduler.py:624-632（对照 540-548 的契约说明）` | medium | update_tracking=False 的 analysis-only plan 仍可能改脏跟踪状态（ROI 键重置未受该开关保护） |
| AUD-8414 | Suggestion | process | `src/features/composite/render/render_scheduler.py:523-525、1215-1216、1223-1224、1242-1243、1267-1268` | medium | _prev_port_revisions/_prev_upstream_revisions 只写不读，死状态 |
| AUD-8415 | Suggestion | process | `src/features/composite/render/render_scheduler.py:582-583（对照模块级 26-30 已导入 is_output_node_type）` | high | plan() 节点循环内重复局部 import，且 get_node_capability 未使用 |
| AUD-8416 | Suggestion | risk | `src/features/composite/render/render_scheduler.py:1329-1337、1301-1302（对照 is_stale:1310-1313、should_cancel_evaluation:1354-1355 均持 _id_lock）` | medium | is_stale_request/mark_current 对 _latest_* 字段无锁读写，与同类方法加锁风格不一致 |
| AUD-8417 | Suggestion | process | `src/features/composite/render/render_scheduler.py:66-71、79-89` | medium | _snapshot_node_needs_frame 两处宽泛 except Exception 吞错，帧依赖误判会导致 dirty-skip 回放旧帧 |
| AUD-8418 | Suggestion | process | `src/features/composite/render/render_scheduler.py:139-141` | high | _check_capability_consistency 定义后调用被注释，_GPU_STATEFUL_TYPES 与能力表一致性校验实际未运行 |
| AUD-8419 | Suggestion | perf-opportunity | `src/features/composite/render/render_scheduler.py:981-986、1004-1007、576-577` | medium | _propagate_roi 每次规划为每个 effect 节点两遍实例化临时 Node，叠加每节点 json.dumps+md5——交互路径性能模式 |
| AUD-8420 | Suggestion | process | `src/features/composite/render/render_scheduler.py:9（"Immutable request"）、152-157 与 271-282；:1274 advance_request_id、:1285 mark_current(request_or_id)、:1233 reset_dirty_tracking、:1247 prepare_for_export、:1357 record_plan_stats 均缺返回注解` | medium | 类型注解缺失；RenderRequest 声称 Immutable 但非 frozen 且 __post_init__ 就地改字段 |
| AUD-8501 | Major | risk | `src/features/composite/render/topos_cache_codec.py:223-235` | medium | `encode_toos_payload_float` 异常路径不调用 `close()`/`abort()`，泄漏 native 编码上下文与实例资源 |
| AUD-8502 | Suggestion | process | `src/features/composite/render/topos_cache_codec.py:30,62` | high | `Optional` 未从 typing 导入即使用 |
| AUD-8503 | Suggestion | process | `src/features/composite/render/topos_cache_codec.py:176-180,223` | medium | 每次调用新建 `ToposCodec` / `ToposImageSequenceEncoder` 实例，丢失持久缓冲复用 |
| AUD-8504 | Suggestion | process | `src/features/composite/render/topos_cache_codec.py:275-277,126-127` | medium | `decode_toos_payload` 对 info 的 bit_depth=0 无防护，可触发除零 |
| AUD-8505 | Suggestion | process | `src/features/composite/render/topos_cache_codec.py:190,243；消费方 src/features/composite/render/disk_cache_store.py:265-266` | medium | manifest 片段键 `file_bytes` 与消费方读取的 `bytes` 不匹配，override 为死代码 |
| AUD-8506 | Suggestion | process | `src/features/composite/render/topos_cache_codec.py:283-285` | high | `_sizeof` 为死代码 |
| AUD-8507 | Suggestion | process | `src/features/composite/render/tracker_analyze_worker.py:65,119,166` | medium | 未知 `mode` 静默回退 TRANSLATION，错误被掩盖且 payload 记录失真 |
| AUD-8508 | Suggestion | process | `src/features/composite/render/tracker_analyze_worker.py:132-137；消费方 src/features/composite/ui/composite_page.py:3738` | medium | 取消语义复用 `failed` 信号 + `"cancelled"` 魔法字符串协议 |
| AUD-8509 | Suggestion | process | `src/features/composite/render/tracker_analyze_worker.py:37-38` | high | `__init__` 的 `parent` 参数缺类型注解 |
| AUD-8601 | Major | bug | `src/features/composite/render3d/animation.py:172-178（对照 :81-82 的 remap 裁剪）` | medium | AnimationState.update 的 finished 判定未应用 end_frame 裁剪，与 remap_frame_to_anim_time 结果不一致 |
| AUD-8602 | Suggestion | process | `src/features/composite/render3d/animation.py:253` | medium | AnimationSamplerCache 以 id(clip) 作为缓存 key，依赖 CPython id 复用风险与调用方自觉 clear |
| AUD-8603 | Suggestion | process | `src/features/composite/render3d/animation.py:25` | high | 模块级 logger 从未使用，且以 __import__("logging") 非常规方式创建 |
| AUD-8604 | Suggestion | process | `src/features/composite/render3d/animation.py:205（local_matrices: dict）、:238-242` | medium | 公共 API 类型注解不完整（select_clip / clip_names / _CacheEntry / sample） |
| AUD-8605 | Major | bug | `src/features/composite/render3d/camera_model.py:77-82` | medium | gate_fit 的 fill 与 overscan 语义相对 Maya 标准约定互换（fill 取较大 gate、overscan 取较小 gate，方向相反） |
| AUD-8606 | Suggestion | process | `src/features/composite/render3d/camera_model.py:44-47` | medium | world_unit_scale 对未知单位静默回退 1.0（米） |
| AUD-8607 | Suggestion | doc-gap | `src/features/composite/render3d/aov.py:203-221` | medium | IDAllocator 文档承诺「跨会话一致」，实现却是按分配（遍历）顺序递增的 id |
| AUD-8608 | Suggestion | process | `src/features/composite/render3d/aov.py:176（encode_id_to_rgba -> tuple）、:210` | high | 类型注解不完整（裸 tuple 返回、IDAllocator.__init__ 缺 -> None） |
| AUD-8701 | Critical | bug | `src/features/composite/render3d/deformers/cpu.py:246-249` | medium | Stretch 变形在 UI 允许的参数范围内可产生 NaN/inf 顶点并上传 GPU |
| AUD-8702 | Major | bug | `src/features/composite/render3d/deformers/cpu.py:229-235、248-257` | medium | Taper/Stretch 非均匀缩放后法线未按 inverse-transpose 变换，原样返回旧法线 |
| AUD-8703 | Major | bug | `src/features/composite/render3d/deformers/cpu.py:291-298、319-321` | medium | evaluate_deformer 对未实现/未知 deformer_type 静默 no-op（SHEAR/SPHERIFY 已导出但无实现） |
| AUD-8704 | Suggestion | process | `src/features/composite/render3d/deformers/cpu.py:317、322` | medium | 每个变形器执行都强制 astype 全量拷贝，栈式/大网格下多余内存带宽 |
| AUD-8705 | Suggestion | process | `src/features/composite/render3d/deformers/cpu.py:40-41` | high | _axis_index 对非法轴名静默回退 Y 轴 |
| AUD-8706 | Major | bug | `src/features/composite/render3d/deformers/base.py:148-149（对照 96、133-137）` | high | DeformerStack.evaluate 无条件返回 recomputed_normals=True，违反 DeformerResult 契约 |
| AUD-8707 | Major | bug | `src/features/composite/render3d/deformers/base.py:141-147（对照 docstring 126-127）；cpu.py:65-66` | medium | spec.selection_set_id 在求值中被完全忽略，selection_mask 一刀切作用于栈内所有变形器 |
| AUD-8708 | Suggestion | process | `src/features/composite/render3d/deformers/base.py:81-86` | medium | DeformerSpec.signature() 遗漏 selection_set_id，且 params 含不可哈希值时会 TypeError |
| AUD-8709 | Suggestion | process | `src/features/composite/render3d/deformers/base.py:64` | high | params 字段类型注解未参数化 |
| AUD-8710 | Suggestion | doc-gap | `src/features/composite/render3d/deformers/__init__.py:6、24-25、29-42` | medium | 模块 docstring 公开接口与实际导出不一致；导出了无实现的 SHEAR/SPHERIFY 常量 |
| AUD-88001 | Suggestion | process | `src/features/composite/render3d/fields.py:29` | high | falloff 字符串未校验，`_VALID_FALLOFFS` 常量定义后从未使用 |
| AUD-88010 | Major | bug | `src/features/composite/render3d/format_capabilities.py:786-789` | medium | `convert_vertices` 的 Z-up→Y-up 轴翻转是镜像（行列式 -1）而非旋转，几何会被翻面 |
| AUD-88011 | Suggestion | process | `src/features/composite/render3d/format_capabilities.py:373、458、449-454` | high | 冗余的函数内 `import os`；`ModelLoaderBackend.load` 缺返回类型注解 |
| AUD-88020 | Suggestion | doc-gap | `src/features/composite/render3d/frustum.py:108、123-125、131` | medium | `aabb_screen_size` 文档承诺"<0 表示 AABB 在相机后"，实现不存在任何返回负值的路径（死注释） |
| AUD-88021 | Suggestion | process | `src/features/composite/render3d/frustum.py:167-183` | medium | `mesh_local_aabb` 全库无调用者，与 `primitives.MeshData.local_aabb` 重复实现且缺少失效机制 |
| AUD-88030 | Suggestion | risk | `src/features/composite/render3d/gizmo_overlay.py:150-151、293、407-409、608-609` | medium | `update()` 之前调用 `build_draw_list()`/`begin_drag()` 会因 `_proj`/`_view` 为 None 崩溃（防御缺口） |
| AUD-88031 | Suggestion | process | `src/features/composite/render3d/gizmo_overlay.py:568-573` | high | `drag_rotate` 实现与注释矛盾：注释称按轴取主导屏幕分量，实现对所有轴一律 `(dx+dy)*90` |
| AUD-88032 | Suggestion | process | `src/features/composite/render3d/gizmo_overlay.py:142、169-170、623-629` | medium | 类型注解缺失；`end_drag` 清理从未被设置的 `_drag_anchor` 属性 |
| AUD-88040 | Suggestion | risk | `src/features/composite/render3d/gizmo_renderer.py:172-224、258` | medium | `_build` 无异常安全：中途失败后重试会泄漏已创建的 GL 对象；GL 上下文重建后 `_built` 不复位 |
| AUD-88041 | Suggestion | process | `src/features/composite/render3d/gizmo_renderer.py:165-171、226-238` | high | 类型注解缺失（`__init__` 无 `-> None`，ctx/矩阵参数未注解） |
| AUD-88050 | Critical | bug | `src/features/composite/render3d/hdri.py:121-133（关键行 130）` | medium | 辐照度卷积遗漏 sin(theta) 立体角权重，漫反射 IBL 积分在数学上错误（渲染错误） |
| AUD-88051 | Major | risk | `src/features/composite/render3d/hdri.py:302-314` | medium | `load_equirect` 对单通道（灰度）全景图崩溃：cv2 返回二维数组，`img.shape[2]` 抛 IndexError（外部输入未验证） |
| AUD-88052 | Suggestion | process | `src/features/composite/render3d/hdri.py:610-623` | medium | `_integrate_brdf_lut` 每次创建新 FBO 和空 VAO 且不显式 release，与其余 program 走 `_vaos` 缓存的模式不一致 |
| AUD-88053 | Suggestion | process | `src/features/composite/render3d/hdri.py:443-444、524-525、633-634、697-698` | high | 多处 `while glGetError() != GL_NO_ERROR: pass` 清空全局 GL 错误标志，会吞掉无关代码的真实 GL 错误 |
| AUD-88054 | Suggestion | process | `src/features/composite/render3d/hdri.py:120-132` | high | 辐照度着色器循环边界非常量（float 累加 phi/theta），不符合项目"循环上限编译期常量"铁律 |
| AUD-88055 | Suggestion | doc-gap | `src/features/composite/render3d/hdri.py:368、387、622-623` | medium | bake 结束后 viewport/FBO 状态不恢复（依赖调用方自律）；缓存键文档与实现不一致（docstring 称含 size，实际 key 无 size） |
| AUD-88056 | Suggestion | process | `src/features/composite/render3d/hdri.py:316-323` | medium | LDR 全景图按图像自身 max 归一化（而非位深满量程），暗图被整体提亮，相对辐射度失真 |
| AUD-88061 | Suggestion | process | `src/features/composite/render3d/ies_profile.py:46-56` | medium | IES 角度表单调性未验证：`sample_profile` 的线性扫描假设严格升序，降序表（LM-63 允许）会得到错误插值 |
| AUD-8900 | Suggestion | process | `src/features/composite/render3d/lod_builder.py:83、117、251、260` | high | 多处宽泛 `except Exception` 静默吞错，其中 `choose_lod_level` 连日志都没有 |
| AUD-8901 | Critical | bug | `src/features/composite/render3d/material_model.py:377-384` | medium | `material_override_to_dict` 静默丢弃 list 类型值，材质面板的 base_color 覆盖无法持久化（保存/重载后丢失） |
| AUD-8902 | Suggestion | process | `src/features/composite/render3d/material_model.py:209-222` | medium | `MaterialEditModel.signature()` 漏掉 bump_scale / displacement_scale / clearcoat_roughness / subsurface |
| AUD-8903 | Suggestion | process | `src/features/composite/render3d/material_model.py:604-627` | high | `MaterialPreset` 只有 `to_dict` 没有 `from_dict`，材质库缺「加载」半边 |
| AUD-8904 | Suggestion | risk | `src/features/composite/render3d/material_model.py:514` | medium | `material_summary` 在渲染线程安全路径上做 `os.path.isfile` 磁盘 IO |
| AUD-8905 | Major | bug | `src/features/composite/render3d/material_state.py:53-64（key 字段）、66-103（from_material）` | medium | `MaterialStateKey` 未纳入 per-mesh `has_tangent` 状态，缓存命中时 `u_has_normal_map` 沿用上一 mesh 的值 |
| AUD-8906 | Suggestion | process | `src/features/composite/render3d/material_state.py:76-85` | high | `_tex_tuple` 注释与类型注解声明 `(path, mtime)` 二元组，实际返回 `(path,)` 一元组 |
| AUD-8907 | Major | bug | `src/features/composite/render3d/material_texture_loader.py:249-260` | medium | `_apply_texture_wrap` 用裸 `glBindTexture` 绑定到「当前激活纹理单元」且不设置/恢复单元，多 slot 首次应用 wrap 时逐级串位覆盖 sampler 单元绑定 |
| AUD-8908 | Major | bug | `src/features/composite/render3d/material_texture_loader.py:74-86` | medium | `_cache` 按 `(path, mtime)` 只增不淘汰，旧 mtime 条目的 GL 纹理永不释放 |
| AUD-8909 | Suggestion | process | `src/features/composite/render3d/material_texture_loader.py:94-96、106-109` | high | `_filter_wrap_state` 未在 `__init__` 声明，靠 `getattr` 惰性创建，对象形状不完整 |
| AUD-8910 | Suggestion | process | `src/features/composite/render3d/material_texture_loader.py:97-100、110-114、188-192、228-231、261-262、264-269` | high | 多处 `except Exception: pass/return False` 宽泛吞错且无日志 |
| AUD-8911 | Suggestion | doc-gap | `src/features/composite/render3d/math_utils.py:361-370` | medium | `spot_intensity` docstring 声称 smoothstep 且「与 shader spot_cone_intensity 一致」，实现是线性斜坡 |
| AUD-8912 | Suggestion | process | `src/features/composite/render3d/math_utils.py:17-18` | high | `IDENTITY4` 是可变共享模块级常量 |
| AUD-8913 | Suggestion | process | `src/features/composite/render3d/math_utils.py:375-382` | high | `focal_from_fov` 对 fov→180° 无防护，除零产生 inf |
| AUD-8914 | Suggestion | process | `src/features/composite/render3d/mesh_diagnostics.py:217-219` | medium | `uv_stretch_to_color` 用本批最大偏差归一化，均匀拉伸的整体被显示为「无拉伸」 |
| AUD-8915 | Suggestion | risk | `src/features/composite/render3d/mesh_picking.py:470-484` | medium | `pick_face` 兼容路径为 O(三角形数) 纯 Python 循环，大模型点击会阻塞调用线程 |
| AUD-8916 | Suggestion | process | `src/features/composite/render3d/mesh_picking.py:278-288` | low | `MeshPickCache._signature` 依赖 `id(vertices)/id(indices)`，数组 GC 后地址复用可致误命中旧 BVH |
| AUD-8917 | Suggestion | process | `src/features/composite/render3d/mesh_picking.py:642-649` | medium | `screen_to_world_ray` 只对 near 点做 w 除零检查，far 点除以≈0 会产生 NaN 方向且未被 norm 检查拦截 |
| AUD-8918 | Suggestion | process | `src/features/composite/render3d/mesh_picking.py:124-128` | high | `TriangleBVH.__init__` 物化 (T,3,3) float64 临时数组，超大模型构建时内存尖峰 |
| AUD-8919 | Major | risk | `src/features/composite/render3d/mesh_vbo_pool.py:101-105` | high | `acquire` 中 VBO 创建成功后 IBO 创建抛异常时无回滚，GL buffer 泄漏 |
| AUD-8920 | Suggestion | process | `src/features/composite/render3d/mesh_vbo_pool.py:122-127、131-135` | high | `release`/`release_all` 用 `except Exception: pass` 吞掉 GL release 错误且无日志 |
| AUD-9000 | Suggestion | process | `src/features/composite/render3d/model_import_cache.py:243-264` | medium | `get()` 对 manifest 标量字段强转 int/float 并直接索引 `data["digest"]`，畸形 JSON 值会抛未捕获异常 |
| AUD-9001 | Suggestion | process | `src/features/composite/render3d/model_import_cache.py:272-306（put 无任何尺寸/条目数检查）` | high | 缓存目录无容量上限与淘汰策略，GLB 派生文件只增不减 |
| AUD-9002 | Suggestion | process | `src/features/composite/render3d/model_import_cache.py:50-63、137` | high | `_assimp_version_tag()` 每次构建 cache key 都同步跑一次 subprocess，无 memoization |
| AUD-9003 | Suggestion | process | `src/features/composite/render3d/model_import_cache.py:308-318` | medium | `_ingest_file` 用 `shutil.copyfile` 直写目标文件名，非原子；跨进程并发可读到半写的 GLB |
| AUD-9004 | Suggestion | process | `src/features/composite/render3d/model_import_report.py:142-148` | medium | `from_summary_dict` 对 warnings/lost_features 类型未验证：标量触发未捕获 TypeError，字符串被逐字符拆分 |
| AUD-9005 | Major | risk | `src/features/composite/render3d/model_import_service.py:279-283、326-333；佐证 model_loader.py:987-994` | medium | 在途导入未完成时应用退出会被非 daemon worker 线程阻塞（最长约 120s+） |
| AUD-9006 | Major | bug | `src/features/composite/render3d/model_loader.py:309-334` | medium | `_convert_trimesh_scene` 把 `transforms.local`（相对父节点）当「相对根」用，层级 glTF 场景的祖先变换丢失/错位 |
| AUD-9007 | Major | bug | `src/features/composite/render3d/model_loader.py:1287-1339` | medium | `_restore_gltf_animation_and_skin` 按「节点名==trimesh 几何名」匹配重建树，未匹配时 mesh 被静默丢弃，FBX CLI fallback 可能渲染为空 |
| AUD-9008 | Major | bug | `src/features/composite/render3d/model_loader.py:206-217` | medium | `_flip_scene_uv_v` 用宽泛 `except Exception: pass` 掩盖 UV 翻转失败，违反 .clinerules「不得宽泛 except 掩盖领域错误」 |
| AUD-9009 | Suggestion | process | `src/features/composite/render3d/model_loader.py:917-921` | high | `_load_assimp` 用 `"load" in msg` 启发式判定 libassimp 缺失，常见解析错误会被误报为「未安装 libassimp」 |
| AUD-9010 | Suggestion | process | `src/features/composite/render3d/model_loader.py:1302-1330、1393-1395` | medium | `_restore_gltf_animation_and_skin` 的 except 元组不含 RecursionError，深层/环形 children 会让整个导入失败而非降级 |
| AUD-9011 | Suggestion | perf-opportunity | `src/features/composite/render3d/model_loader.py:848-861` | high | `_extract_trimesh_skeletons` 拓扑排序 O(n²)：每节点 `unique_names.index` + 每次递归重建全集 set |
| AUD-9012 | Suggestion | perf-opportunity | `src/features/composite/render3d/model_loader.py:1157-1170` | medium | `_resample_gltf_track` 逐行 Python 循环做线性插值，长动画导入慢 |
| AUD-9013 | Suggestion | process | `src/features/composite/render3d/model_loader.py:529-537` | medium | `_normalize_color_factor` 对全 NaN 数组返回 NaN 颜色，未做有限性兜底 |
| AUD-9014 | Suggestion | process | `src/features/composite/render3d/model_loader.py:1065-1077` | high | `_load_gltf_document_and_buffers` 对 buffer uri 不做路径规范化，绝对路径/`..` 可逃逸模型目录读任意本地文件 |
| AUD-9015 | Suggestion | doc-gap | `src/features/composite/render3d/model_loader.py:525-526、1718-1719` | high | 材质转换失败时宽泛 `except Exception: return None` 整体静默丢材质，与 docstring「失败返回标量 fallback」不符 |
| AUD-9016 | Suggestion | process | `src/features/composite/render3d/model_loader.py:288（` | medium | 第三方对象参数普遍缺类型注解（Suggestion 级汇总） |
| AUD-9017 | Suggestion | process | `src/features/composite/render3d/morph.py:18-20、112-117` | high | `MorphError` 定义后从未 raise/使用，属死代码导出 |
| AUD-9018 | Suggestion | process | `src/features/composite/render3d/morph.py:74-78、84-87` | medium | `apply_morph_targets` 对传入权重不做有限性验证，NaN/Inf 权重会生成 NaN 几何直接进 VBO |
| AUD-9100 | Suggestion | doc-gap | `src/features/composite/render3d/node_icon_overlay.py:139-150, 186, 379-398, 462-504` | medium | add_camera_icon 的 near/far 与 add_light_icon 的 range 参数存而不用，docstring 与实现不符 |
| AUD-9101 | Suggestion | process | `src/features/composite/render3d/node_icon_overlay.py:204-215, 643-658` | medium | 跨近平面折线直接剔除后方点，不做近平面裁剪，穿越视相机时线段错连 |
| AUD-9102 | Suggestion | process | `src/features/composite/render3d/node_icon_overlay.py:663-709` | medium | hit_test 每次调用全量重建所有图标几何并逐点投影，无缓存 |
| AUD-9103 | Suggestion | process | `src/features/composite/render3d/node_icon_overlay.py:680, 683, 703-704` | medium | hit_test 循环内 anchor 为死赋值 |
| AUD-9104 | Suggestion | process | `src/features/composite/render3d/node_icon_overlay.py:683, 703-704` | medium | `icon.get("icon_pos") or icon["pos"]` 用 or 而非 is not None 判空 |
| AUD-9105 | Suggestion | doc-gap | `src/features/composite/render3d/perf_stats.py:99-129` | high | reset() 不重置 quality_profile/viewport_layout/active_view，与"清零所有计数器"的 docstring 不符 |
| AUD-9106 | Major | bug | `src/features/composite/render3d/point_cloud.py:96-103, 157-171` | medium | 外部点云文件的 NaN/Inf 坐标未过滤，rasterize 出现未定义整型转换与静默错误输出 |
| AUD-9107 | Suggestion | process | `src/features/composite/render3d/point_cloud.py:127-135` | medium | points_from_track_data 对 tx/ty 直接 float() 无容忍，单帧坏值中断整个导入 |
| AUD-9108 | Critical | bug | `src/features/composite/render3d/primitives.py:299-305 (plane), 373-379 (sphere), 445-471 (cylinder 盖), 527-539 (cone 底盖), 582-589 (torus)` | medium | plane/sphere/torus 及 cylinder 顶/底盖、cone 底盖的三角形缠绕与法线方向相反，backface culling 开启时整面被剔除 |
| AUD-9109 | Major | bug | `src/features/composite/render3d/primitives.py:203-228（消费者 mesh_vbo_pool.py:90）` | medium | content_hash 不含 normals/uvs 字节，注释却声称包含；VBO 池据此共享缓冲致错误 UV/法线 |
| AUD-9110 | Suggestion | process | `src/features/composite/render3d/primitives.py:156-158` | high | compute_tangents 的 denom+1e-12 处理数值不稳健 |
| AUD-9111 | Suggestion | process | `src/features/composite/render3d/primitives.py:47-58` | medium | MeshData.__post_init__ 不校验 vertices/normals/uvs 形状一致 |
| AUD-9112 | Suggestion | process | `src/features/composite/render3d/primitives.py:391-400, 624-628` | high | PRIMITIVES 用 None 占位再于模块尾回填的脆弱模式 |
| AUD-9113 | Suggestion | process | `src/features/composite/render3d/primitives.py:65, 230` | high | 类型注解缺失：local_aabb 返回值、_normalize_morph_map 入参 |
| AUD-9114 | Suggestion | process | `src/features/composite/render3d/raycast.py:133-193` | high | ray_mesh_intersect 为 O(M) 纯 Python 循环且全仓库无调用方（死代码） |
| AUD-9115 | Suggestion | process | `src/features/composite/render3d/raycast.py:156-165` | high | ray_mesh_intersect 假定 indices 为 3 的倍数，畸形数组越界 IndexError |
| AUD-9116 | Suggestion | process | `src/features/composite/render3d/raycast.py:38-39, 126-130` | medium | screen_to_ray 对奇异 proj@view 直接求逆无防护；ray_plane_intersect 的 t<0 语义未在契约中说明 |
| AUD-9117 | Major | bug | `src/features/composite/render3d/scene_graph.py:29-35, 383-398, 565-599` | medium | 节点身份仅由 (source_path, 名字路径) 哈希，同名兄弟节点 id/动画键冲突 |
| AUD-9118 | Suggestion | process | `src/features/composite/render3d/scene_graph.py:429-434, 452-471` | medium | compute_bbox 只检查 bmin 非有限，bmax 含 Inf 时穿透 → normalize_scene 产出退化缩放矩阵 |
| AUD-9119 | Suggestion | process | `src/features/composite/render3d/scene_graph.py:396-397, 425-427, 597-598` | medium | flatten_scene/compute_bbox/apply_animation_to_scene 递归遍历无深度保护 |
| AUD-9120 | Suggestion | doc-gap | `src/features/composite/render3d/scene_hierarchy.py:232-243, 249-279` | high | compute_hierarchy_stats 的 hierarchy 参数完全未使用，docstring 声称"避免重复遍历" |
| AUD-9121 | Suggestion | process | `src/features/composite/render3d/scene_hierarchy.py:154-158` | high | 局部 AABB 计算用 `except Exception: pass` 宽泛吞异常 |
| AUD-9122 | Suggestion | process | `src/features/composite/render3d/scene_hierarchy.py:109-115, 135-137` | medium | 同名兄弟节点生成重复 node_id（同 AUD-9117 根因，本文件消费侧） |
| AUD-9200 | Critical | bug | `src/features/composite/render3d/scene_payload.py:366` | medium | ModelPayload.signature 遗漏 material_params，材质控制参数变化不触发重渲染（陈旧输出） |
| AUD-9201 | Suggestion | process | `src/features/composite/render3d/scene_payload.py:110` | high | CameraPayload.signature 把纯 UI 开关 show_safe_areas 纳入签名，切换安全框触发无谓重渲染 |
| AUD-9202 | Suggestion | process | `src/features/composite/render3d/scene_payload.py:213` | medium | LightPayload.signature 用 id() 标识 IBL GL 对象，存在地址复用漏检变化的理论风险 |
| AUD-9203 | Suggestion | process | `src/features/composite/render3d/scene_payload.py:242` | high | GeometryPayload.model_matrix 类型注解 np.ndarray 但默认值为 None |
| AUD-9204 | Suggestion | perf-opportunity | `src/features/composite/render3d/scene_payload.py:277` | medium | GeometryPayload.signature 每次求值对 instance_offsets 全量 astype+tobytes，大网格下为每帧 MB 级拷贝 |
| AUD-9205 | Suggestion | process | `src/features/composite/render3d/selection.py:85` | high | select_objects_box 未实现 SELECT_TOGGLE 分支，框选切换模式静默 no-op |
| AUD-9206 | Suggestion | process | `src/features/composite/render3d/selection.py:112` | medium | 空选择状态下 SUBTRACT 组件选择会留下空 set 条目，is_empty()/signature 误报"有选择" |
| AUD-9207 | Suggestion | process | `src/features/composite/render3d/skeleton.py:161` | medium | build_skeleton_from_joints 不校验 parent_joint_index 合法性，违规输入直接 IndexError |
| AUD-9208 | Suggestion | process | `src/features/composite/render3d/skeleton.py:180` | medium | compute_joint_matrices 与 skinning.compute_skin_joint_matrices 重复实现且缺失回退语义不一致，且前者当前无调用方 |
| AUD-9209 | Suggestion | process | `src/features/composite/render3d/skeleton.py:410` | medium | build_all_bone_weights_summary 未钳制骨骼索引上限，损坏数据可致 O(B×N×4) 近乎挂起 |
| AUD-9210 | Suggestion | process | `src/features/composite/render3d/skeleton_renderer.py:185` | high | 渲染热路径使用 __import__('moderngl') 内联导入而非模块顶部导入 |
| AUD-9211 | Suggestion | process | `src/features/composite/render3d/skeleton_renderer.py:213` | medium | _release_vbos/release 使用裸 except Exception: pass 吞掉 GL 释放异常 |
| AUD-9212 | Suggestion | process | `src/features/composite/render3d/skinning.py:141` | medium | skin_mesh_cpu 对 vertices/normals/uvs 的访问与形状校验顺序不一致，畸形 mesh 抛裸异常而非 SkinningError |
| AUD-9213 | Suggestion | process | `src/features/composite/render3d/volume_grid.py:42` | medium | VolumeGrid 构造器不校验 spacing > 0，负 spacing 使透射率物理错误 |
| AUD-9214 | Suggestion | process | `src/features/composite/render3d/volume_grid.py:82` | medium | save_volume 未强制 .npz 后缀，numpy 会静默追加后缀导致写出文件与请求路径不一致 |
| AUD-9300 | Major | bug | `src/features/composite/runtime.py:107（同型模式见 120、134、151、165、180）` | medium | 六处 `except Exception` 以 debug 级别吞掉可选面板的构建失败，违反“不得宽泛 except 掩盖领域错误” |
| AUD-9301 | Major | bug | `src/features/composite/runtime.py:237-240（关联 src/features/composite/ui/spline_panel.py:198-199、540-544）` | medium | cleanup() 只委托 page.cleanup()，运行时自建面板未被清理，SplinePanel 播放定时器可在页面拆卸后继续触发 |
| AUD-9302 | Suggestion | process | `src/features/composite/runtime.py:74-86` | medium | create() 不可重入且异常路径留下半初始化状态，与“失败返回 False”的契约不符 |
| AUD-9303 | Suggestion | process | `src/features/composite/runtime.py:91、104、117、130、148、162、176` | high | 通过私有属性向页面注入面板，绕过公共 API |
| AUD-9304 | Suggestion | process | `src/features/composite/runtime.py:24-45、199、219、231` | medium | 类型注解薄弱：字段全为 Any、Dict 未参数化、on_enter/load_composite_data 参数无注解 |
| AUD-9305 | Suggestion | doc-gap | `src/features/composite/runtime.py:19-20（关联 src/features/composite/module.py:193-210）` | medium | 类文档声称由 CompositeModule.create_runtime() 创建，实际该路径零调用且一旦启用会重复建页 |
| AUD-9400 | Major | bug | `src/features/composite/ui/backdrop_item.py:707-718` | medium | _choose_color 非模态对话框无引用保持，方法返回即被 Python GC 销毁 |
| AUD-9401 | Major | bug | `src/features/composite/ui/backdrop_item.py:713-717（触发源）、357-365（setColor）、240-253（_notify_changed）` | medium | 取色器实时预览每个颜色 tick 都提交一次完整 undo 组并全图同步（undo 洪泛） |
| AUD-9402 | Major | bug | `src/features/composite/ui/backdrop_item.py:775-791（from_dict）、266-269（apply_state）` | medium | from_dict/apply_state 对持久化数据无键校验，缺键/类型错直接 KeyError/TypeError 中断项目恢复 |
| AUD-9403 | Suggestion | process | `src/features/composite/ui/backdrop_item.py:510-514` | high | contained_node_ids 由 set 转 list，序列化顺序跨会话不确定 |
| AUD-9404 | Suggestion | process | `src/features/composite/ui/backdrop_item.py:460-469（对照 169-175、159-161）` | medium | resize 每个鼠标 tick 重复更新 resize handle 并对 note 全文重排 |
| AUD-9405 | Suggestion | process | `src/features/composite/ui/backdrop_item.py:35-49 及全文件多处` | medium | 构造参数 None 默认值未标 Optional，大量公有方法与事件处理器缺返回类型注解 |
| AUD-9406 | Suggestion | process | `src/features/composite/ui/backdrop_item.py:76、452、489、498` | medium | _was_moved 标记只写不读（死状态） |
| AUD-9407 | Suggestion | process | `src/features/composite/ui/asset_dependency_panel.py:146-150（对照 119-122、139-144）` | medium | set_operation_message 永久覆盖 summary 样式，update_report 不恢复中性色 |
| AUD-9408 | Suggestion | process | `src/features/composite/ui/asset_dependency_panel.py:169-175` | medium | “角色”列直接显示原始 role slug，未使用已本地化的 purpose 字段 |
| AUD-9409 | Suggestion | doc-gap | `src/features/composite/ui/__init__.py:4-8` | medium | 文档字符串声称本包提供 CompositeViewer/CompositeViewerBar/PixelInspectorBar，实际并未导入或导出 |
| AUD-9500 | Critical | risk | `src/features/composite/ui/composite_page.py:1562` | medium | `_retry_renderer_init` 引用未定义的 `viewer_node_id`，重试路径必抛 NameError 崩溃 |
| AUD-9501 | Major | bug | `src/features/composite/ui/composite_page.py:2003` | high | `_add_ai_generate_node` 使用未导入的 `AIGenerateNode`，一旦被调用即 NameError（当前为埋雷死代码） |
| AUD-9502 | Major | bug | `src/features/composite/ui/composite_page.py:6760-6768（同型逻辑 6707–6712 trim 路径共用同一累加器）` | medium | 参数防抖累加器跨参数/跨节点混包：80ms 内修改第二个参数会沿用第一个参数的 old_value，且首条修改丢失 undo 记录 |
| AUD-9503 | Major | risk | `src/features/composite/ui/composite_page.py:7152-7157, 7210-7215` | medium | `_undo_add_node` / `_redo_remove_node` 删除节点未提交 CLEANUP_NODE，渲染线程侧 GPU 资源在 undo/redo 路径泄漏 |
| AUD-9504 | Major | bug | `src/features/composite/ui/composite_page.py:1477, 2735, 2742（唯一赋值点 6212 属性 setter、7446` | medium | `_properties_panel` 从不在 `__init__` 初始化，`on_enter`/`_on_project_settings_requested` 等直接属性读取，绑定前即 AttributeError |
| AUD-9505 | Major | risk | `src/features/composite/ui/composite_page.py:1872-1909（cleanup 主体）；重试链 1303-1304、1463、1582-1583` | medium | `cleanup()` 不停止各单发/防抖定时器，cleanup 后 `_renderer_retry_timer` 触发会重建并启动新的渲染线程（或先撞上 AUD-9500） |
| AUD-9506 | Major | bug | `src/features/composite/ui/composite_page.py:2232-2233` | medium | `load_composite_data` 对外部持久化数据的 fps/duration 直接 float()/int() 转换，非数值即抛异常逃逸 Qt 槽 |
| AUD-9507 | Suggestion | process | `src/features/composite/ui/composite_page.py:1478` | medium | `QTimer.singleShot(0, lambda: ...)` 违反本文件自述的 T-P1-71 lambda-定时器禁令 |
| AUD-9508 | Suggestion | risk | `src/features/composite/ui/composite_page.py:5484-5491` | medium | `_build_render_request` 每次求值都对全图执行 `to_dict()` 序列化做变更检测，UI 线程热路径 O(图规模) 开销 |
| AUD-9509 | Suggestion | process | `src/features/composite/ui/composite_page.py:1734, 5828-5829` | high | 热路径逐帧 INFO 级调试日志刷屏 |
| AUD-9510 | Suggestion | process | `src/features/composite/ui/composite_page.py:3543-3551, 6267-6277` | medium | bypass/enabled 切换改变持久化状态但无 undo 记录、无编辑起始信号 |
| AUD-9511 | Suggestion | process | `src/features/composite/ui/composite_page.py:9411-9464（paint stroke）, 6881-6920（batch param）, 9040-9068（active camera）` | medium | 三类真实用户编辑缺 `_emit_user_edit_once()`，不触发 Comp Clip promotion |
| AUD-9512 | Suggestion | process | `src/features/composite/ui/composite_page.py:2498-2515（_prepare_nodes_for_removal 三连）, 4577-4578, 5136-5137, 8182-8184, 1867-1870, 6524-6525` | medium | 多处宽泛 `except Exception: pass` 静默吞错，违反 .clinerules“不得宽泛 except 掩盖领域错误” |
| AUD-9513 | Suggestion | process | `src/features/composite/ui/composite_page.py:2328（` | medium | 死代码与私有成员穿透 |
| AUD-9514 | Suggestion | process | `src/features/composite/ui/composite_page.py:490（` | medium | 类型注解缺失（Suggestion 级维度） |
| AUD-9600 | Major | bug | `src/features/composite/ui/composite_render_dialog.py:517-518（控件定义 381-391）` | medium | 帧范围无 start<=end 交叉验证，倒置区间会静默产生空渲染/负数总帧数 |
| AUD-9601 | Suggestion | process | `src/features/composite/ui/composite_render_dialog.py:37-42` | high | 跨层导入 shared 模块的下划线私有符号 |
| AUD-9602 | Suggestion | process | `src/features/composite/ui/composite_render_dialog.py:285-301（注释 "Quality (JPEG/WebP)"）、513` | high | 质量控件对所有图像格式恒显示且恒写入导出选项 |
| AUD-9603 | Suggestion | process | `src/features/composite/ui/composite_render_dialog.py:493、510-512、527-529` | high | base_name 未校验路径分隔符等非法字符 |
| AUD-9604 | Suggestion | process | `src/features/composite/ui/composite_render_dialog.py:112-119` | high | 类型注解不完整：bare tuple 参数与未注解 parent |
| AUD-9700 | Major | bug | `src/features/composite/ui/composite_viewport.py:8730` | medium | PAINT 工具笔画无法绘制也无法提交——press 从未 start_drag，move/release 分发不可达 |
| AUD-9701 | Major | bug | `src/features/composite/ui/composite_viewport.py:8664` | medium | ROI 工具交互式框选死路——drag 矩形永为 0 尺寸且提交路径不可达 |
| AUD-9702 | Major | bug | `src/features/composite/ui/composite_viewport.py:9598` | medium | 像素检查器 UV 映射未扣除工具栏/播放条占位区，读取的像素位置系统性偏移 |
| AUD-9703 | Major | bug | `src/features/composite/ui/composite_viewport.py:3038` | medium | GL 上下文重建时仅重置 ODT LUT 句柄，其余 owned 纹理/FBO 保留陈旧 ID |
| AUD-9704 | Major | bug | `src/features/composite/ui/composite_viewport.py:11015` | medium | set_state 恢复持久化数值无类型校验，畸形工程数据将中断整个恢复流程 |
| AUD-9705 | Major | bug | `src/features/composite/ui/composite_viewport.py:3866` | medium | A/B 快照在 makeCurrent 之前调用 glIsTexture，无当前上下文时 GL 调用先行失败 |
| AUD-9706 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:2929` | high | get_state/set_state 在同一类中定义两次，前一对被完全遮蔽 |
| AUD-9707 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:1054` | medium | _probe_context_sharing 引用从未挂到 self 的属性，一旦调用必抛 AttributeError |
| AUD-9708 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:3762` | medium | cleanup() 无任何调用方，且 VBO/VAO/Program/Checker 纹理删除不带 makeCurrent 保护 |
| AUD-9709 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:11047` | medium | set_state 校验 render_pass 时引用本类从未定义的 `_available_render_passes` |
| AUD-9710 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:1428` | medium | 多处 `except Exception: pass` 吞掉领域错误，违反宽泛 except 禁令 |
| AUD-9711 | Suggestion | doc-gap | `src/features/composite/ui/composite_viewport.py:11078` | medium | `_space_pressed` 恒返回 False，文档承诺的 Space+左键平移分支不可达 |
| AUD-9712 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:5071` | high | `_should_show_safe_areas` 中 if/else 两分支完全相同（死条件） |
| AUD-9713 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:732` | high | 工具栏曝光步进从 QLabel 文本反解析当前值，脆弱耦合 |
| AUD-9714 | Suggestion | perf-opportunity | `src/features/composite/ui/composite_viewport.py:5253` | high | 3D 模式每帧重复扫描节点图并按象限重复合并全局配置 |
| AUD-9715 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:1544` | high | CPU 回退上传路径把 HDR 线性数据钳制并量化为 8-bit，且像素检查器仍标注 "linear" |
| AUD-9716 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:1381` | medium | GL 未初始化时 `set_texture` 直接采用渲染器纹理 ID，违反自述的 owned-copy 不变式 |
| AUD-9717 | Suggestion | process | `src/features/composite/ui/composite_viewport.py:1709` | high | 大量公共/私有方法缺少返回类型注解，信号载荷用裸 object |
| AUD-9800 | Major | bug | `src/features/composite/ui/connection_item.py:724-727` | medium | 环检测 DFS 依赖 PortItem 不存在的 `_connections` 属性，恒返回 False（环防护形同虚设） |
| AUD-9801 | Suggestion | process | `src/features/composite/ui/connection_item.py:691（对照 652、749）` | medium | Alt+拖拽（disconnect 模式）下 `_would_create_cycle` 使用未同步的 `_reconnect_src` 判方向，环检测方向错误 |
| AUD-9802 | Suggestion | process | `src/features/composite/ui/connection_item.py:762-764、776-779` | high | `_remove_from_port`/`_add_to_port` 为静默 no-op 的死代码 |
| AUD-9803 | Suggestion | process | `src/features/composite/ui/connection_item.py:911-919` | medium | 最近点搜索的贪心单侧递归在曲线回折时可漏掉真正最近点 |
| AUD-9804 | Suggestion | process | `src/features/composite/ui/connection_item.py:39、51（` | high | 多处参数/属性缺类型注解（Suggestion 级） |
| AUD-9805 | Suggestion | process | `src/features/composite/ui/graph_editor_view.py:342-349、355-363` | medium | 表达式求值器懒加载使用裸 `except Exception` 掩盖真实错误（违反"不得宽泛 except"铁律） |
| AUD-9806 | Suggestion | process | `src/features/composite/ui/graph_editor_view.py:407-410（对照 169-170、233-238）` | medium | `resizeEvent` 无条件重置视口，忽略 `_user_adjusted`，与本文件其余路径的用户缩放保留语义矛盾 |
| AUD-9807 | Suggestion | process | `src/features/composite/ui/graph_editor_view.py:640-652（对照` | medium | `_abort_active_drag` 恢复快照后不重排 `series.points`，与 `_cancel_drag` 不一致，可致曲线暂时绘制异常 |
| AUD-9808 | Suggestion | process | `src/features/composite/ui/graph_editor_view.py:839-845（调用点）与 1056-1078（候选构建）` | high | 拖动吸附为每点每 move 全量重建候选列表，O(选中点 × 总关键帧) |
| AUD-9809 | Suggestion | process | `src/features/composite/ui/graph_editor_view.py:728（` | high | 部分方法缺返回类型注解（Suggestion 级） |
| AUD-9900 | Suggestion | process | `src/features/composite/ui/graph_expression_editor.py:290-299` | medium | 表达式开关切换无宿主回滚路径，宿主拒绝后按钮状态与节点状态脱钩 |
| AUD-9901 | Suggestion | process | `src/features/composite/ui/graph_expression_editor.py:272-274` | high | `target()` 返回裸 `tuple`，缺少参数化类型注解 |
| AUD-9902 | Suggestion | process | `src/features/composite/ui/graph_layout.py:8` | high | 未使用的导入 `QGraphicsItem` |
| AUD-9903 | Suggestion | process | `src/features/composite/ui/graph_layout.py:52、81-82` | high | `auto_layout` 死变量 `node_ids` 与死代码 `layer_height` |
| AUD-9904 | Suggestion | process | `src/features/composite/ui/graph_layout.py:289-304` | medium | `cleanup_overlaps` 50 次尝试上限后静默保留重叠 |
| AUD-9905 | Major | bug | `src/features/composite/ui/graph_model.py:383、400-405、418-427、429-434（` | medium | 曲线采样先按关键帧全跨度生成、最后才裁剪到可视区，成本随数据跨度而非视口缩放 |
| AUD-9906 | Major | bug | `src/features/composite/ui/graph_model.py:793-800（另见 808-817 两处宽泛 except）` | medium | 表达式求值器不可用时宽泛 `except Exception` 静默回退且 `has_error=False`，误导用户以为表达式生效 |
| AUD-9907 | Major | bug | `src/features/composite/ui/graph_model.py:874-879` | medium | `build_graph_series` 对 frame/value 的 `float()` 无防护，损坏的工程数据可令异常逃逸到 UI 构建路径 |
| AUD-9908 | Suggestion | doc-gap | `src/features/composite/ui/graph_model.py:514-527` | high | `compute_speeds` 返回按帧排序后的顺序，与 docstring 承诺的"与 points 同长度同顺序"不符 |
| AUD-9909 | Suggestion | process | `src/features/composite/ui/graph_model.py:461-478` | medium | `_to_authoritative_keyframes` 将 float 帧取整为 int，亚帧关键帧预览漂移、相邻帧可退化为同帧 |
| AUD-9910 | Suggestion | process | `src/features/composite/ui/graph_model.py:437-438（` | medium | 若干内部函数类型注解缺失 |
| AUD-9911 | Suggestion | process | `src/features/composite/ui/graph_navigation.py:25-36` | medium | `_normalise_viewport` 防 NaN 未防 ±inf，inf 缩放可进入书签并持久化为非标准 JSON |
| AUD-9912 | Suggestion | process | `src/features/composite/ui/graph_node_search_popup.py:164-166（文本）vs 413-415（行为）` | medium | 帮助文本与 Shift+Enter 实际行为相反 |
| AUD-9913 | Suggestion | process | `src/features/composite/ui/graph_node_search_popup.py:446-460` | medium | `keyPressEvent` 中 Ctrl+Up/Ctrl+Down 分支不可达（死代码） |
| AUD-9914 | Suggestion | process | `src/features/composite/ui/graph_node_search_popup.py:100、119-121、139、147、154、164-166` | high | 全部 UI 字符串硬编码英文，未走 I18N |
| AUD-9915 | Suggestion | process | `src/features/composite/ui/graph_node_search_popup.py:38-39、428、467、473` | high | `"NodeItem"`/`"NodeGraph"` 前向引用从未导入（无 TYPE_CHECKING 块），事件处理参数缺类型注解 |
| AUD-9916 | Suggestion | risk | `src/features/composite/ui/graph_view.py:105-112（对照安装路径 114-118）` | medium | `set_graph_view(None)` 重置回占位页时旧 placeholder 移出布局后未销毁（泄漏/潜在重叠） |
| AUD-9917 | Suggestion | doc-gap | `src/features/composite/ui/graph_view.py:40-44（承诺）、151-174（仅转发 timeline）、96-122（set_graph_view 不连线）` | medium | Graph 画布信号未按类 docstring 承诺转发到宿主信号面 |
| AUD-9918 | Major | bug | `src/features/composite/ui/group_commands.py:148-162` | medium | `UpdateGroupCommand` 浅快照 + 原位 `update`：redo 直接污染 `_old_data`，第二次 undo 恢复的是新值 |
| AUD-9919 | Major | bug | `src/features/composite/ui/group_commands.py:70-81（浅拷贝）、95-109（原位 append/remove）` | medium | `UngroupCommand` 浅拷贝与 `AddToGroupCommand` 原位修改共享同一 `node_ids` 列表，undo/redo 交错序列丢失组成员 |
| AUD-9920 | Major | bug | `src/features/composite/ui/group_commands.py:13-15（模块用法契约）；实际调用方 src/features/composite/ui/node_editor_actions.py:1046、1072-1073、1093-1094（直接 redo 后丢弃）` | medium | 组操作全部绕过 UndoStack：命令对象 `redo()` 后即被丢弃，分组/解组/加组不可撤销 |
| AUD-9921 | Suggestion | process | `src/features/composite/ui/group_commands.py:247、46-50` | low | `create_group_from_selection` 用 8 字符 uuid 片段作 group_id，碰撞时静默覆盖现有组 |
| AUD-9922 | Suggestion | process | `src/features/composite/ui/group_commands.py:53、78、103、131、159、192、225（` | high | 多处 `undo()` 及个别字段缺类型注解 |

↓ = 复核后降级（AUD-0310、AUD-1214、AUD-36100）。完整证据与修复建议见各批次报告。
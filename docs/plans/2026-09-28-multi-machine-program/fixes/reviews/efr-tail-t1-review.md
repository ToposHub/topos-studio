# 复查报告：efr-tail-t1（rollup 清尾 T1：63 条带行号单项）

- 提交：`5baa8c8b31a091804d1fd1ed82a0d56a362e39a4`（47 files，+513/−159）＋补交 closeout `9984d5939`（AUD-8505 断言更新 + 报告补交）
- 执行报告：`docs/plans/2026-09-28-multi-machine-program/fixes/reports/efr-tail-t1.md`
- 复查日期：2026-10-02 ｜ 只读复查，未运行 pytest

## 结论：PASS-with-notes

## 1. 范围核对（含跨提交归属梳理）

主提交 47 个源文件 = 本报告 46 项清单 − 2 项（test_run_trace.py 实际落入 teaching 提交 9240787a4、tests/composite/test_topos_cache_payload.py 落入 closeout）＋ 3 个他报告文件：

| 打包进本提交的文件 | 归属报告 | 内容核对 |
|---|---|---|
| src/app/app_context.py、feature_registry.py | efr-small-s2 | 仅删 `field`/`Any`，与 s2 声明一致 ✓ |
| src/features/edit/core/commands.py（+245 行） | **efr-tail-t2** 的 AUD-16216（9 命令类 to_dict/from_dict + 宏登记 8 行） | 抽验 diff：纯增量序列化方法 + `_MACRO_SUBCOMMAND_TYPES` 登记，apply/undo 未动 ✓ |

closeout 9984d5939 仅含 t1 报告 + test_topos_cache_payload.py 两断言 `file_bytes→bytes`——与报告"已披露的 2 断言同步"一致。全部改动可溯源到本组 8 报告并集；无越界文件、无 REMEDIATION_PLAN.md/FIXLOG.md 触碰。

## 2. FIXED 项抽查明细（37+5 项中抽查 19 项，全部在位）

| ID | 抽查内容 | 结果 |
|---|---|---|
| AUD-0901 | video_denoise_gl RenderSurface `__slots__` 含 components/dtype；get() 校验尺寸+components+dtype | ✓ 在位 |
| AUD-13021 | assistant_session_store 三处方法尾部重复 `_sync_catalog_to_cache()` 已删，留 AUD 注释（:526/539/554）；_touch 内保留 | ✓ |
| AUD-16104 | clip_params CropParams 5 字段 float()、SpeedParams float()/bool() 强转，带 AUD 注释 | ✓ |
| AUD-16107 | is_normal_speed/effective_duration_factor 已删（残留命中仅为 :499 AUD 注释），留指向 timeline_duration_for_source_duration() | ✓ |
| AUD-16109 | clip_params 模块头 `if TYPE_CHECKING: import numpy as np`（带 pragma 注释） | ✓ |
| AUD-18304 | `_STYLE_FIELD_SET: frozenset[str] = frozenset(STYLE_FIELDS)`（:85），`style[f]` 直接索引保留（拒绝静默回退，判断正确） | ✓ |
| AUD-18814 | timeline_ops clip_handle_headroom(:3082)/nearest_marker_frame(:3102)/build_numeric_trim_command(:3185) 注解在位 | ✓ |
| AUD-2013 | ai_edit_controller `_teaching_html_generate_system_prompt` 死属性已删（残留仅 :3887 AUD 注释；tests/ai/test_teaching_animation_phase5.py:609 为测试自设属性，非读取断言，与报告一致） | ✓ |
| AUD-20818 | CandidateMarkerItem 内 `self._pixels_per_frame/_fps` 死存储已删（Overlay 类的同名活跃属性不受影响，逐类核实） | ✓ |
| AUD-22713 | test_run_trace 断言改为 `len==1 and startswith("s:event_1:")` 位置契约（在 teaching 提交中，diff 核对一致） | ✓ |
| AUD-24801 | 7 个 tracking 垫片统一：re-export 循环跳过 dunder 与 ModuleType（coordinate_map 实读核实，`import types as _types`+`del`） | ✓ |
| AUD-26002 | scripting/examples 统一 `topos.print(...)`（basic_grade/composite_batch_params/composite_build_graph 实读核实） | ✓ |
| AUD-26309 | effect_catalog DCTL `preview_cost = str(plugin.get("preview_cost") or "medium")`，带契约说明注释 | ✓ |
| AUD-8505 | topos_cache_codec 两处 `"bytes": int(file_bytes)`（:197/:266）与 disk_cache_store 消费键对齐；closeout 两断言同步 | ✓ |
| AUD-64019 | clamp_node ParamDef("channels", "str", "rgba") + 注释 | ✓ |
| AUD-7800 | composite/preferences 注释与 tooltip 文案对齐 "ocio" 工厂默认 | ✓ |
| AUD-9602(部分) | composite_render_dialog 注释按实际行为修正（恒显示恒写入），带挂账说明 | ✓ |
| AUD-8508(部分) | `TRACKER_ANALYZE_CANCELLED` 常量生产/消费两侧共用（worker:31/140 + page:3822/3823） | ✓ |
| AUD-28400 等 | preset 9 目录 build() -> dict 抽验 2 个（kinetic_pop_title/pie_donut_chart）在位 | ✓ |

行为邻接项说明（均已披露且理由成立）：AUD-16104 强转属计划行明确要求的反序列化硬化（良构输入不变）；AUD-0901 缓存键校验扩展（现状无调用方传非默认参数）；AUD-8505 键名对齐激活了原本死代码的 override 路径并同步测试；AUD-26309 值读取字段修正（全仓无运行时消费方）。单项真实性均成立。

## 3. DEFERRED 理由核对表（20 条逐条）

| ID | 主题 | 报告理由 | 复查判定 |
|---|---|---|---|
| AUD-14810 | toposctl 未知 flag 静默忽略 | 改 raise = CLI 错误语义变更 | ✓（台账原文即"静默吞掉"） |
| AUD-15613 | blend_rgb 无条件 clip(0,1) | 去 clip = CPU 像素输出变更（HDR 域） | ✓（台账：CPU/GPU 分叉决策） |
| AUD-15806 | 交付命名去重大小写 | 改变交付文件命名行为 | ✓ |
| AUD-16219 | 命令栈 coalesce/undo 回退 | 新功能 + undo 行为变更 | ✓ |
| AUD-16420 | quality 非法值校验 | raise = 错误语义变更 | ✓ |
| AUD-16507 | 探测缓存清理范围扩大 | 运行时行为变更 | ✓ |
| AUD-16610 | opacity 进 shader 前钳制 | 渲染输出变更 | ✓（台账原文对照一致） |
| AUD-2310 | denoise/deblur 处理器合并 | ~200 行重构等价性风险 | ✓ |
| AUD-2518 | 双重编码判定 | 台账自标"需复核"，语义决策 | ✓ |
| AUD-3108 | 跨模块私有成员治理 | 需设计公共 API | ✓ |
| AUD-22207 | plugin_status 对齐回启 | UI 行为变更 + 冲突区规避 | ✓ |
| AUD-23117 | executor_id=TTS_PROVIDER | 台账注明"受控"约定，改路由 = 行为变更 | ✓（台账原文对照一致） |
| AUD-30019 | 模板参数元字符拒绝 | 改变接受输入集 | ✓ |
| AUD-5311 | 静态最大边界→动态 | 像素输出变更 | ✓ |
| AUD-5312 | SSBO mirror 边缘策略 | 像素输出变更 | ✓ |
| AUD-6210 | stopped_reason 反向覆盖等 | 传播行为变更 | ✓ |
| AUD-6813 | group 参数↔属性双轨 | 行为变更 | ✓ |
| AUD-8407 | on_worker_error 双调用守卫 | 错误回调次数 = 事件流契约 | ✓（台账：called-once 契约） |
| AUD-9001 | model_import_cache 容量上限 | 缓存行为变更 | ✓ |
| AUD-21801 | features/__init__ eager import | 懒加载 = 导入期副作用变更 | ✓（台账原文对照一致） |

20/20 条 DEFERRED 均为渲染行为/错误语义/信号契约/并发/重构风险类，机械批不做此类决策完全正确；台账对照抽 6 条（14810/15613/16610/8407/21801/23117）原文一致，无编造。

## 4. 红线

- 裸 except：无新增 except（AUD-16104 用强转不捕获；AUD-26309 无 try） ✓
- 信号 connect：唯一含 connect 的改动为 `setCurrentIndex(2) → setCurrentIndex(self._PAGE_MAIN)`（AUD-19509 常量化），一一对应参数重写，连接本身未增删 ✓
- GLSL/色彩矩阵：grade_node 仅 HELP_TEXT 文案；shaders.py 不在改动清单（5311/5312 DEFERRED） ✓
- 公共 API：零改名 ✓

## 5. py_compile（8 文件）

clip_params / timeline_ops / assistant_session_store / coordinate_map / pass_fusion / ai_edit_controller / commands / qualifier_panel → 全部 OK。

## 6. 综合判定

**PASS-with-notes**。Notes：① commands.py（t2 的 AUD-16216）与 s2 两文件被打进本提交、本批 test_run_trace 落入 teaching 提交、报告与断言补交靠 closeout——提交/报告非双射（打包现象，内容全部可溯源）；② FIXED/DEFERRED 真实性全部核实通过。

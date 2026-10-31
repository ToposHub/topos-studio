# Batch-032 静态审查报告 — app/feature_flags.py + app/feature_registry.py

- 日期: 2026-09-28
- 审查范围: `src/app/feature_flags.py`（21 行，全文通读）、`src/app/feature_registry.py`（221 行，全文通读）
- 问题编号区间: AUD-3200 – AUD-3205
- 汇总: Critical 0 / Major 2 / Suggestion 4
- 交叉验证: 已核对 `src/shared/feature_flags.py`、`src/app/page_contract.py`、`src/app/module_loader.py`、`src/main.py`、`src/app/controllers/page_controller.py`、`src/app/controllers/menu_toolbar_controller.py`、`src/shared/i18n/i18n.py` 及各 `src/features/*/module.py` 实现以确认影响面。

---

## src/app/feature_flags.py (行数: 21)

### 发现

- [AUD-3200] [Suggestion] 兼容包装层在生产与测试代码中均无调用方，且每次调用重复重建 flags（无缓存）
  - 位置: src/app/feature_flags.py:15-21
  - 证据:
    ```python
    def get_enabled_features() -> dict[str, bool]:
        flags = load_feature_flags()
        return {f: flags.is_enabled(f) for f in DEFAULT_FEATURE_FLAGS}


    def is_feature_enabled(name: str) -> bool:
        return load_feature_flags().is_enabled(name)
    ```
  - 影响: 全仓 grep（排除 src/shared 自身）未发现任何 `from src.app.feature_flags import` 或 `DEFAULT_FEATURES` 引用，`tests/architecture/test_product_presets.py` 中的同名 `get_enabled_features` 来自 `src.products`，即本包装层是零调用的死代码；同时每次调用都经 `load_feature_flags()` 重读约 4 个环境变量并重建 dict，无缓存（量级很小，非热点）。
  - 建议: 按 docstring 的迁移计划在确认无外部脚本依赖后删除该包装；若保留，可在模块级缓存 flags 并在环境变更时失效，避免逐调用重建。
  - 置信度: 高

### 通过项
- 线程安全：纯函数式转发，无可变共享状态
- GPU 资源生命周期：不涉及 GPU/ModernGL 资源
- GLSL·HDR 正确性：不涉及
- 信号防递归：不涉及 Qt 控件/信号
- 状态提交一致性：只读查询，不涉及 undo/redo/revision/持久化
- 异常与取消路径：转发调用，无自身异常处理需求；无吞异常的宽泛 except
- 输入验证：`is_enabled` 对未知 flag 的宽松默认行为属 `src/shared/feature_flags.py` 的既有设计（有注释说明），本包装层无额外验证义务
- 性能模式：每次调用重建 flags 成本极低（非热点路径），仅作 AUD-3200 附注
- 类型注解：完整（`dict[str, bool]`、`str`、`bool`）

---

## src/app/feature_registry.py (行数: 221)

### 发现

- [AUD-3201] [Major] `register_save_handlers` 调用未按 R2-4 意图做异常隔离，异常可沿注册链上抛导致半注册状态
  - 位置: src/app/feature_registry.py:89-90
  - 证据:
    ```python
            # [R2-4] 页面/动作注册与 i18n/ai_providers 同样加 try 隔离：
            # 单个模块的注册抛异常此前会中断整个注册链 → 应用启动 FATAL
            try:
                pages = module.register_pages(self._context)
                for page in pages:
                    self.register_page(page)
            except Exception as e:
                logger.warning(f"Failed to register pages for {module.feature_id}: {e}")

            try:
                module.register_actions(self._context)
            except Exception as e:
                logger.warning(f"Failed to register actions for {module.feature_id}: {e}")
            ...
            if self._context.save_router:
                module.register_save_handlers(self._context)
    ```
  - 影响: 同文件中 pages/actions/ai_providers（68-87 行）以及后来的 `register_all_save_handlers`（204-210 行，对同一 `register_save_handlers` 已加 try 隔离）均有隔离，唯独 89-90 行未隔离，与紧邻注释声明的 R2-4 修复意图不一致。若模块在 `save_router` 已存在时注册（当前主启动路径 save_router 为 None 使该分支休眠，但 `src/app/main_window.py:425` 创建 SaveRouter 后任何后续/动态注册即会进入）且其 `register_save_handlers` 抛异常：经 `module_loader._register_spec` 的宽捕获路径时模块已被加入 `_modules`（55 行）且 pages 已注册，但注册被上报为失败（半注册不一致状态、R2-8 误报失败提示）；经 `register_color_module(guarded=False)` 主路径时异常直接上抛为启动 FATAL——正是注释声称已消除的故障模式。
  - 建议: 与其余注册步骤对齐，将 90 行包入 try/except 并 `logger.warning` 隔离；或统一删除 89-90 行、完全依赖 `register_all_save_handlers` 的后置注册。
  - 置信度: 中（代码不对称性为客观事实；实际触发需要 save_router 存在时注册模块且处理器抛异常，当前主启动路径该分支不执行）

- [AUD-3202] [Major] `on_page_enter`/`on_page_leave` 分发无异常隔离，单模块异常可中断页面切换并阻断其余模块通知
  - 位置: src/app/feature_registry.py:176-177, 190-191
  - 证据:
    ```python
        for module in self._modules.values():
            module.on_page_enter(page_id, self._context)


        for module in self._modules.values():
            module.on_page_leave(page_id, self._context)
    ```
  - 影响: `CompositeModule.on_page_enter` 含真实逻辑（`self._page.on_enter()`，src/features/composite/module.py:177-182），并非全部为空实现；一旦任一模块钩子抛异常：(1) 迭代顺序在后的模块（edit/deliver/teaching_animation/ai_workflow 等）收不到通知；(2) 异常沿 `page_controller.switch_to_page` 无 try 的调用点上抛（page_controller.py:192 的 leave 在 195 行布局保存之前、286 行的 enter 在 292 行起的 dock 标题栏刷新/视口黑屏修复之前），页面切换半途中止——离开侧当前页布局未保存、进入侧 dock/视口未完成初始化。本文件注册路径已按 R2-4 全面隔离，唯独运行期钩子分发未隔离，防护不对称。
  - 建议: 在循环体内对每个 `module.on_page_enter/on_page_leave` 包 try/except（`logger.warning` + 继续），与注册路径的隔离语义对齐。
  - 置信度: 中（无隔离为客观事实且调用点无外层防护；触发依赖某个模块钩子抛异常，当前多数钩子是空实现）

- [AUD-3203] [Suggestion] `reload_i18n` 忽略 `I18N.reload_locale` 的失败返回值，失败时仍记录成功日志
  - 位置: src/app/feature_registry.py:219-221
  - 证据:
    ```python
        from src.shared.i18n.i18n import I18N
        I18N.reload_locale(locale_code)
        logger.info(f"Reloaded i18n for locale {locale_code} across {len(self._modules)} modules")
    ```
  - 影响: `reload_locale` 返回 `bool`（基础 locale 文件加载失败时返回 False，见 src/shared/i18n/i18n.py:297-320），此处被忽略且无条件打 "Reloaded" 成功日志；调用方 `menu_toolbar_controller.change_language`（677 行）随即用新旧混合的翻译继续弹重启对话框，无任何失败信号，排障时日志会误导。
  - 建议: 接收返回值，失败时 `logger.warning` 并可考虑回退 `I18N.load_locale` 或向调用方返回失败状态。
  - 置信度: 高

- [AUD-3204] [Suggestion] `register_module` 返回值无任何调用方消费，False 的两种语义不可区分
  - 位置: src/app/feature_registry.py:37-53, 92
  - 证据:
    ```python
        if not module.is_available():
            logger.info(f"Feature module '{module.feature_id}' is not available, skipping registration")
            return False

        if module.feature_id in self._modules:
            logger.warning(f"Feature module '{module.feature_id}' already registered")
            return False
    ```
  - 影响: "模块不可用（flag 关闭）" 与 "重复注册" 均返回 False，调用方（module_loader.py:70、109 两处）均不接收返回值、只依赖异常判断成败，形成无人消费的死信号；未来调用方一旦消费该布尔值将无法区分两种情况（不可用属正常，重复注册属配置错误）。
  - 建议: 要么让 module_loader 消费返回值以区分跳过原因，要么改为显式枚举/异常表达注册结果，消除歧义死信号。
  - 置信度: 高

- [AUD-3205] [Suggestion] 未使用的 `Any` 导入
  - 位置: src/app/feature_registry.py:2
  - 证据:
    ```python
    from typing import Dict, List, Optional, Any
    ```
  - 影响: 全文件仅此行出现 `Any`（`_modules`/`_pages`/`_context` 及全部方法签名均使用具体类型），死导入增加阅读噪音。
  - 建议: 删除 `Any`。
  - 置信度: 高

### 通过项
- 线程安全：registry 在主线程启动阶段构建（module_loader/main.py），`on_page_enter/leave`、`reload_i18n` 均由 UI 线程的 page_controller/menu_toolbar_controller 调用，单线程访问，无需加锁
- GPU 资源生命周期：不创建/持有任何 ModernGL 或纹理资源，模块 UI 创建经 factory 延迟进行
- GLSL·HDR 正确性：不涉及着色器与像素数据
- 信号防递归：本文件不触碰 Qt 控件，不发信号；`reload_i18n` 由 change_language 显式调用、无信号回环
- 状态提交一致性：save handler 注册走 `register_all_save_handlers` 后置路径（main.py:275，在 `app_context.save_router` 赋值后调用）且逐模块隔离，各模块 `set_callbacks` 幂等；本文件自身不操作 undo/redo/revision
- 输入验证：`register_page` 重复 page_id 有告警去重；`get_page`/`get_module` 对未知 id 返回 None；`reload_i18n` 的 locale 合法性由 `I18N.reload_locale` 内部判定（缺陷见 AUD-3203）
- 性能模式：`get_all_pages` 每次排序（N 为页数个位数）无影响；字典查找 O(1)；无热点循环
- 类型注解：除 AUD-3205 的死导入外，dataclass 字段与全部方法签名注解完整

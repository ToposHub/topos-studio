# batch-338 审查报告 — src/shared/i18n

批次: batch-338 [P2] | 编号区间: AUD-33800–AUD-33899 | 审查日期: 2026-09-28

---

## src/shared/i18n/__init__.py (行数: 3)

### 发现

- 无

### 通过项

- 纯 re-export（无逻辑）：从 `src.shared.i18n.i18n` 导出 `I18N`、`tr`、`tr_or_none`；`__all__` 与实际导出一致，无循环导入、无副作用。

---

## src/shared/i18n/i18n.py (行数: 422)

### 发现

- [AUD-33800] [Major] tr() 主路径与回退路径的占位符格式化只捕获 KeyError，ValueError/IndexError 会逃逸到调用方
  - 位置: src/shared/i18n/i18n.py:167-171（主路径）、src/shared/i18n/i18n.py:184-189（回退路径）
  - 证据:
    ```python
    # 主路径（169 行只捕获 KeyError）
    if isinstance(value, str) and kwargs:
        try:
            return value.format(**kwargs)
        except KeyError as e:
            logger.warning(f"Warning: Missing format argument {e} for key '{key}'")
            return value
    ```
    对比同函数 default 分支（150-154 行）已知需捕获三种异常：
    ```python
    if kwargs:
        try:
            return str(default).format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return str(default)
    ```
  - 影响: 已实测验证 `"abc {".format(x=1)` 抛 `ValueError: Single '{' encountered`、`"}".format()` 抛 `ValueError`、`"{0}".format()` 抛 `IndexError`。翻译 JSON 属外部数据，某条译文含孤立 `{`/`}` 或位置占位符时，`tr()` 会直接向调用点抛异常；`tr()` 被 UI 大量调用（如 src/app/controllers/file_controller.py:408、420 等），异常沿栈传播可致界面操作崩溃/未处理异常对话框。default 分支已证明作者知晓该风险，但主路径与回退路径遗漏。
  - 建议: 两处 `except KeyError` 改为 `except (KeyError, IndexError, ValueError)`，与 default 分支对齐；或加载时校验译文占位符与 kwargs 兼容。
  - 置信度: 高

- [AUD-33801] [Major] 加载的翻译 JSON 不校验根类型，非 dict 根在模块合并路径抛未捕获异常
  - 位置: src/shared/i18n/i18n.py:96-97、123（json.load 直接赋值）；277-282、287-294（`_do_load_module_dir` 的 except 只含 JSONDecodeError/OSError）；325-326（`_deep_merge` 的 `dict(base)`/`override.items()`）
  - 证据:
    ```python
    with open(file_path, "r", encoding="utf-8") as f:
        cls._translations = json.load(f)   # 根可为 list/str/number，仍返回 True
    ...
    with open(locale_file, "r", encoding="utf-8") as f:
        module_translations = json.load(f)
    cls._translations = cls._deep_merge(cls._translations, module_translations)
    ...
    except (json.JSONDecodeError, OSError) as e:
    ...
    def _deep_merge(cls, base: dict, override: dict) -> dict:
        result = dict(base)          # base 为 list 时抛 ValueError/TypeError
        for key, value in override.items():   # override 为 list 时抛 AttributeError
    ```
  - 影响: `json.load` 接受任意合法 JSON 根。若某 locale 文件根不是对象（手工编辑/截断产生，如 `["menu", ...]`），`load_locale` 仍返回 True；随后任一 feature 模块激活调用 `load_module_translations`（src/features/color/module.py:132、edit:191、composite:223、deliver:140、ai_workflow:94、teaching_animation:104 全部经过此路径）时，`_deep_merge` 内 `dict(base)` 或 `override.items()` 抛 ValueError/TypeError/AttributeError——均不在捕获的 `(json.JSONDecodeError, OSError)` 内，异常从 `load_module_translations` 传播，模块激活失败甚至启动崩溃。注：`tr()` 自身对非 dict 根会 TypeError 被 174 行捕获而降级，不崩溃。违反铁律"外部数据须验证"。
  - 建议: 每次 `json.load` 后校验 `isinstance(result, dict)`，不满足则记日志、置 `{}` 并返回 False；同时 `_do_load_module_dir` 的捕获列表补上 TypeError/ValueError/AttributeError 作为兜底。
  - 置信度: 中（触发需畸形翻译文件，但全部模块激活路径依赖该合并，防线缺失是确定性事实）

- [AUD-33802] [Suggestion] 类级共享翻译状态无锁，reload_locale 两步更新存在中间态窗口
  - 位置: src/shared/i18n/i18n.py:310-317（reload_locale 先换基础词典、再逐个重合并模块）；263-264（`_module_locales_dirs` 检查-后-追加）；279/290/292（`cls._translations = cls._deep_merge(cls._translations, ...)` 无锁读-改-写）
  - 证据:
    ```python
    # Step 1: Reload base locale (resets _translations and _fallback_translations)
    success = cls.load_locale(locale_code)
    ...
    # Step 2: Re-merge all tracked module translations
    for module_dir in cls._module_locales_dirs:
        cls._do_load_module_dir(module_dir)
    ```
  - 影响: 运行时语言切换（src/app/feature_registry.py:220、src/app/controllers/menu_toolbar_controller.py:679）期间，Step 1 与 Step 2 之间并发的 `tr()` 会读到"只有基础词典、缺模块键"的中间态，短暂返回 key/default（自愈但界面闪现未翻译文本）；并发调用 `load_module_translations` 的读-改-写可能丢失一次合并。缓解因素：词典更新是原子重绑定且 `_deep_merge` 不原地修改嵌套 dict（旧引用读者安全）；`src/core` 渲染线程未导入 i18n，未找到非 UI 线程调用 `tr()` 的直接证据，当前实际风险低。
  - 建议: 若后续有 worker 线程调用 tr()，为写路径加 `threading.Lock`；或把 reload 改为"先在局部变量构建完整词典、最后一次重绑定"以消除中间态。
  - 置信度: 中

- [AUD-33803] [Suggestion] 使用已弃用的 locale.getdefaultlocale()
  - 位置: src/shared/i18n/i18n.py:364
  - 证据:
    ```python
    system_locale = locale.getdefaultlocale()[0]
    ```
  - 影响: 该 API 自 Python 3.11 起弃用（DeprecationWarning，计划 3.15 移除）；当前产生告警噪音，未来 Python 升级后该检测分支失效（有 try/except 与 en_US 回退兜底，不致崩溃，仅语言自动检测退化）。
  - 建议: 改用 `locale.getlocale()[0]`，或删除该分支直接依赖环境变量（Method 2）与 QLocale（Method 3）。
  - 置信度: 高

- [AUD-33804] [Suggestion] _instance 为从未使用的死属性，误导单例设计意图
  - 位置: src/shared/i18n/i18n.py:22
  - 证据:
    ```python
    _instance: Optional['I18N'] = None
    ```
  - 影响: 全文件无任何读写；实际实现是纯 classmethod 类级单例，残留声明会误导读者以为存在实例单例/延迟实例化机制。
  - 建议: 删除该属性；若为预留需注释说明。
  - 置信度: 高

- [AUD-33805] [Suggestion] get_system_locale 两处宽泛 except Exception
  - 位置: src/shared/i18n/i18n.py:371-372、392-393
  - 证据:
    ```python
    except Exception as e:
        logger.error(f"Warning: locale.getdefaultlocale() failed: {e}")
    ...
    except Exception as e:
        logger.error(f"Warning: QLocale detection failed: {e}")
    ```
  - 影响: 与 .clinerules"不得宽泛 except"约束不符。此处有日志与 en_US 回退、非静默吞错，但会把属性拼写错误等编程 bug 一并归类为"检测失败"。
  - 建议: 收窄为 `(OSError, ValueError, ImportError)`（Method 3 含 PyQt6 导入故需 ImportError）。
  - 置信度: 中

- [AUD-33806] [Suggestion] 类型注解不完整/风格不统一
  - 位置: src/shared/i18n/i18n.py:323（`_deep_merge(cls, base: dict, override: dict) -> dict` 裸 dict）、132/203/400/415（`**kwargs` 无注解）、23-24（`Dict[str, Any]`）与 51（`list[str]`）混用两种风格、10（typing import 位于 logger 赋值之后，PEP8 E402）
  - 证据:
    ```python
    def _deep_merge(cls, base: dict, override: dict) -> dict:
        """Deep merge override into base dict. Returns merged dict."""
    ...
    _module_locales_dirs: list[str] = []
    ```
  - 影响: 裸 dict 丢失键值类型信息；`typing.Dict` 与内建泛型混用降低一致性；无行为影响，纯质量改进。
  - 建议: 统一为 `Dict[str, Any]`；`**kwargs` 注解为 `Any`；将 typing import 移至文件顶部其他 import 旁。
  - 置信度: 高

### 通过项

- GPU 资源生命周期：纯翻译工具类，不涉及任何 OpenGL/ModernGL 资源。
- GLSL·HDR 正确性：不涉及着色器与 HDR 数据处理。
- 信号防递归：无 Qt 信号连接，无控件更新路径。
- 状态提交一致性：无 undo/redo/revision/持久化状态；`get_available_locales()` 返回 `SUPPORTED_LOCALES.copy()` 防外部篡改类常量。
- 异常与回退路径（除 AUD-33800/33801 外）：FileNotFoundError/JSONDecodeError 均有日志与回退；`load_locale` 回退到 fallback locale 的递归在 `locale_code == _fallback_locale` 时终止，无死循环；`_load_fallback_translations` 失败降级为 `{}` 而非崩溃。
- 输入验证（locale 代码部分）：`load_locale` 对 `locale_code` 有 `SUPPORTED_LOCALES` 白名单校验并自动回退（89-91 行）；`_locale_for_language` 对空串/大小写安全（341-347 行）。
- 性能模式：`tr()` 为 O(key 深度) 字典查找，无逐次 IO/编译；deep merge 仅发生在语言切换与模块注册（低频）；`get_locales_dir` 惰性初始化正确，无重复路径计算热点。
- `tr_or_none` 对 `_translations` 未加载、key 缺失两种情况的返回语义与 docstring 一致。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 2 | AUD-33800, AUD-33801 |
| Suggestion | 5 | AUD-33802, AUD-33803, AUD-33804, AUD-33805, AUD-33806 |

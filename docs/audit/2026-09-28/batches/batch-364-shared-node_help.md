# batch-364 — src/shared/node_help//

编号区间：AUD-36400–AUD-36499
审查日期：2026-09-28
文件清单：`src/shared/node_help/__init__.py` (123)、`src/shared/node_help/dialog.py` (159)

---

## src/shared/node_help/__init__.py (行数: 123)

### 发现

- [AUD-36401] [Suggestion] 语言读取失败时直接放弃，未按文档声明的解析顺序回退 en_US
  - 位置: src/shared/node_help/__init__.py:85
  - 证据:
    ```python
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return None
    return None
    ```
  - 影响: 文档字符串（72–74 行）声明解析顺序为「当前语言文档 → en_US 回退」，但当首选语言文件存在却读取失败（权限/IO 错误）时，`except OSError: return None` 直接终止，不再尝试 candidates 中的 en_US 回退文件，用户看到空白帮助而非英文回退。
  - 建议: 将 `return None` 改为 `continue`，让循环继续尝试下一个候选路径；仅当全部候选失败时才返回 None。
  - 置信度: 高

- [AUD-36402] [Suggestion] `_current_app_locale` 使用宽泛 `except Exception`，可能掩盖 I18N 模块的真实缺陷
  - 位置: src/shared/node_help/__init__.py:57
  - 证据:
    ```python
    try:
        from src.shared.i18n.i18n import I18N
        return I18N.get_current_locale()
    except Exception:
        return FALLBACK_LANG
    ```
  - 影响: 违反「不得宽泛 except 掩盖领域错误」铁律的边缘案例：`get_current_locale()` 内部的真实 bug（如 AttributeError/KeyError）会被静默吞掉，帮助文档语言无声回退为英文，i18n 缺陷难以被发现。注释说明这是刻意的 best-effort 设计（便于单元测试/未初始化场景），故仅列 Suggestion。
  - 建议: 收窄为 `except (ImportError, AttributeError):`，或至少在 except 分支记录 debug 日志。
  - 置信度: 高

- [AUD-36403] [Suggestion] `has_node_help` 为返回布尔值读取并解码整个文档文件
  - 位置: src/shared/node_help/__init__.py:120
  - 证据:
    ```python
    def has_node_help(node_type: str,
                      locale_code: Optional[str] = None) -> bool:
        """True when a usage document exists for ``node_type`` (any language)."""
        return get_node_help(node_type, locale_code=locale_code) is not None
    ```
  - 影响: 布尔判断触发完整文件 I/O + UTF-8 解码；调用方（node_properties_panel.py:1415、scene_cut_dialog.py:2156）先 `has_node_help` 再在对话框内重新 `get_node_help`，同一文件被读取两次。帮助文档较小，实际开销可忽略，但模式不佳。
  - 建议: 增加基于 `os.path.isfile(candidates)` 的轻量存在性检查，`get_node_help` 保留完整读取。
  - 置信度: 高

- [AUD-36404] [Suggestion] `node_type` 未做路径安全校验即拼入文件路径
  - 位置: src/shared/node_help/__init__.py:81
  - 证据:
    ```python
    candidates = [os.path.join(docs_dir, f"{node_type}.{lang}.md")]
    if lang != FALLBACK_LANG:
        candidates.append(os.path.join(docs_dir, f"{node_type}.{FALLBACK_LANG}.md"))
    ```
  - 影响: 若 `node_type` 含 `/`、`\` 或 `..`，路径可越出 docs_dir（如读取 docs_dir 之外的 `*.en_US.md` 文件）。当前调用链中 node_type 均来自内部节点注册表，未发现外部注入路径，属防御性缺失而非现实漏洞。
  - 建议: 对 `node_type` 增加白名单校验（如仅允许 `[a-z0-9_]`），或拒绝含路径分隔符/`..` 的值。
  - 置信度: 低（机制确定，但未发现现实触发路径）

### 通过项
- 线程安全：纯函数式读取，无共享可变状态，无并发风险
- GPU 资源生命周期：本模块不涉及任何 GPU/ModernGL 资源
- GLSL/HDR 正确性：不涉及着色器与 HDR 数据
- 状态提交一致性：无 undo/redo/revision/持久化状态
- `get_node_help` 对空 `node_type` 有验证（76–77 行）；`open` 有 OSError 防护
- `available_langs` 对 `os.listdir` 的 OSError 有防护（107–110 行），排序/去重逻辑正确
- PyInstaller frozen 路径（`sys._MEIPASS`）处理正确且带回退
- 类型注解完整（所有公开函数均有参数与返回值注解）

---

## src/shared/node_help/dialog.py (行数: 159)

### 发现

- [AUD-36405] [Suggestion] `__init__` 的 `parent` 参数缺少类型注解
  - 位置: src/shared/node_help/dialog.py:61
  - 证据:
    ```python
    def __init__(self, node_type: str, node_title: str, parent=None):
    ```
  - 影响: 与项目强类型规范不符；`parent` 实际类型为 `Optional[QWidget]`，IDE/类型检查器无法推断。
  - 建议: 改为 `parent: Optional[QWidget] = None` 并导入 QWidget。
  - 置信度: 高

### 通过项
- 信号防递归：`addItem` 先于 `currentIndexChanged.connect`（84–86 行），构造期无信号触发；`set_language` 的程序化 `setCurrentIndex` 仅单次触发 `_on_lang_changed`，加载路径无重入控件操作，不存在递归
- 线程安全：纯 UI 线程控件，无非 UI 线程访问
- GPU 资源生命周期：不涉及 GPU 资源
- GLSL/HDR 正确性：不涉及
- 状态提交一致性：只读帮助查看器，无 undo/持久化状态
- 异常与取消路径：`get_node_help` 返回 None 时 `setMarkdown("")` 优雅降级为空白页；`available_langs` 为空时 combo 为空、`current_lang()` 返回空串且 `_load_lang` 安全处理（调用方均先经 `has_node_help` 防护）
- 输入验证：`findData` 未命中时 `set_language` 走直接加载分支，不引发异常
- 性能模式：`_apply_typography` 按块遍历文档，O(文档长度)，对帮助文档规模可接受；语言切换仅触发一次加载
- 排版逻辑：`block.length() - 1` 正确排除段落分隔符；headingLevel 0/1/2/3+ 分支覆盖完整
- `setWindowFlags` 在对话框显示前调用，去掉帮助按钮的时机正确

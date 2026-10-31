# batch-042 审查报告 — 主题 src/app//（AUD-4200–AUD-4299）

审查人按 docs/audit/2026-09-28/assignments/INSTRUCTIONS.md 九维度执行。

## src/app/update_checker.py (行数: 94)
### 发现
- [AUD-42001] [Suggestion] 环境变量 TOPOS_TEST_MODE 在生产环境会静默禁用更新检查并伪造 no_update
  - 位置: src/app/update_checker.py:64-68
  - 证据:
    ```python
    def run(self):
        # Skip network access during tests to avoid thread leaks and segfaults
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TOPOS_TEST_MODE"):
            self.no_update.emit()
            return
    ```
  - 影响: 用户环境中若残留 TOPOS_TEST_MODE（或以测试模式启动的打包版本），更新检查被完全跳过且 UI 收到"无更新"的假信号，用户无从知晓。
  - 建议: 将测试旁路移到构造参数（如 `UpdateChecker(test_mode=True)`）或仅依赖 PYTEST_CURRENT_TEST；至少在跳过时输出一条日志。
  - 置信度: 中（属测试稳定性的有意设计，但环境变量对生产不可控）。
- [AUD-42002] [Suggestion] 版本兜底字面量 "3.0.0" 依赖人工同步，打包/冻结环境下可能以错误本地版本比较
  - 位置: src/app/update_checker.py:15, 18-31
  - 证据:
    ```python
    _FALLBACK_VERSION = "3.0.0"

    def _resolve_version() -> str:
        try:
            import importlib.metadata as _md
            return _md.version("topos")
        except Exception:
            pass
    ```
  - 影响: 当发行元数据名不匹配（distribution 名未必是 "topos"）且 pyproject.toml 不在 parents[2]（如 PyInstaller 冻结环境）时，本地版本恒为 3.0.0；若真实版本已高于/低于 3.0.0，会出现误报或漏报更新。
  - 建议: 兜底前先 logger.warning 提示版本源不可用；或从单一构建注入常量（build hook 写入）替代人工同步。
  - 置信度: 中（注释已自认人工同步，风险取决于发布流程）。
- [AUD-42003] [Suggestion] parse_version 只取前三段数字，忽略预发布与四段版本号
  - 位置: src/app/update_checker.py:40-45
  - 证据:
    ```python
    def parse_version(version_str: str) -> tuple:
        """Parse a version string like 'v1.2.3' into (1, 2, 3)."""
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_str)
        if match:
            return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return (0, 0, 0)
    ```
  - 影响: "v1.2.3-rc.1" 与 "1.2.3" 判为相等（预发布不视为更新）；"1.2.3.4" 第四段被忽略；解析失败统一 (0,0,0) 会令任何远端版本都"更新"。
  - 建议: 改用预发布感知的比较（如 packaging.version.Version），并对解析失败显式返回 None 跳过比较。
  - 置信度: 高（行为可由正则直接推断，实际影响取决于发布 tag 风格）。
- [AUD-42004] [Suggestion] 注释/文档写 GitHub，实际端点为 Gitee
  - 位置: src/app/update_checker.py:36-37, 54
  - 证据:
    ```python
    # GitHub API endpoint for releases
    RELEASES_URL = "https://gitee.com/api/v5/repos/xiaoduoshare/topos/releases/latest"
    ...
    """Async update checker – queries GitHub/Gitee Releases API."""
    ```
  - 影响: 注释与真实端点不符，误导维护者排障（如按 GitHub API 文档调试鉴权/限流行为）。
  - 建议: 更正注释为 Gitee API v5。
  - 置信度: 高
### 通过项
- 线程安全：QThread.run 中执行阻塞网络调用（timeout=10），信号从工作线程 emit 经队列连接回 UI 线程，UI 不被阻塞，不涉及 OpenGL。
- GPU 资源生命周期：不涉及。
- GLSL·HDR 正确性：不涉及。
- 信号防递归：单向发射 update_available/no_update/error，无接收回路。
- 状态提交一致性：不涉及 undo/redo/持久化。
- 异常与取消路径：HTTPError 404 特判为"无发布"，其余 HTTP 错误与其他异常均经 error 信号外抛并 logger.debug，未静默吞掉；urlopen 设置了超时。
- 输入验证：远端 JSON 字段用 .get 带默认值，tag 为空时走 no_update 分支；版本比较失败不会崩溃。
- 性能模式：一次性后台线程，无热路径。
- 类型注解：模块级函数与构造参数均有注解（run/run() 为 Qt 重载可接受）。

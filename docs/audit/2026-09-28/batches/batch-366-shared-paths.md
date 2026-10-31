# batch-366 审查报告：src/shared/paths（AUD-36600–AUD-36699）

审查人维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

## src/shared/paths/__init__.py (行数: 3)

### 发现

- [AUD-36603] [Suggestion] __all__ 未导出 get_hdri_dir，包表面不完整
  - 位置: src/shared/paths/__init__.py:1-2
  - 证据:
    ```python
    from src.shared.paths.paths import get_project_root, get_lut_dir, get_models_dir, get_cache_dir

    __all__ = ["get_project_root", "get_lut_dir", "get_models_dir", "get_cache_dir"]
    ```
  - 影响: paths.py 定义了 `get_hdri_dir`（50-74 行）但包级导出缺失，调用方被迫深路径导入（node_properties_panel.py:4093、scene3d_node.py:633 均写 `from src.shared.paths.paths import get_hdri_dir`），与包的其余导出风格不一致，重构时易遗漏。
  - 建议: 将 `get_hdri_dir` 加入 import 与 `__all__`。
  - 置信度: 高

### 通过项
- 其余为纯 re-export，无逻辑。

## src/shared/paths/paths.py (行数: 118)

### 发现

- [AUD-36601] [Major] get_project_root 回溯层级少一层，返回 <root>/src 而非项目根，luts/models/cache 全部解析到源码树内
  - 位置: src/shared/paths/paths.py:24（直接影响 39、88、110 行的 lut/models/cache 拼接）
  - 证据:
    ```python
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ```
    同文件 get_hdri_dir 的注释自述了该 bug（57-58 行）：
    ```python
        注：直接从本文件（src/shared/paths/paths.py）回溯 4 层到项目根，
        不复用 get_project_root()（它只回溯 3 层到 src/，是预存 bug）。
    ```
  - 影响: 本文件位于 `<root>/src/shared/paths/paths.py`，3 层 dirname 只到 `<root>/src`。仓库实证：根目录 `luts/` 含真实 .cube（07.cube、cinematic.cube 等），而 `get_lut_dir()` 返回 `<root>/src/luts`——一个被本函数自动创建的空目录（已存在于仓库）；raw_panel.py:219 与 lut_gallery.py:36-44 从该空目录枚举 `.cube`，RAW 面板下拉与 LUT 画廊在开发模式下永远列不出任何 LUT。`get_cache_dir()` 使 scene_cut 缓存写入 `<root>/src/cache/scene_cut`（目录已实际存在），缓存被写进源码树、位置错位；`get_models_dir()` 同理指向 `<root>/src/models`。frozen 模式之外的所有布局均错位一级。
  - 建议: `get_project_root()` 改为 4 层 dirname 与 get_hdri_dir 对齐（或二者共用单一 root 解析常量，删除旁路）；清理/迁移已写入 `src/luts`、`src/cache` 的残留数据；补一个以已知文件锚点断言根目录的单元测试。
  - 置信度: 高（代码注释自证 + 仓库目录状态实证 + 调用点核实）

- [AUD-36602] [Major] 四处 `except Exception: pass` 吞掉目录创建错误，违反“不得宽泛 except 掩盖领域错误”铁律
  - 位置: src/shared/paths/paths.py:42-45、69-72、94-97、113-116（get_lut_dir / get_hdri_dir / get_models_dir / get_cache_dir 各一处，代码完全相同）
  - 证据:
    ```python
        if not os.path.exists(lut_dir):
            try:
                os.makedirs(lut_dir, exist_ok=True)
            except Exception:
                pass
    ```
  - 影响: 目录创建失败（只读卷、权限不足、路径被同名文件占位等 OSError）被静默吞掉后，函数仍返回不存在的路径；调用方在更远处以更模糊的形式失败（LUT 列表为空、缓存写入 FileNotFoundError），且无任何日志可供排查，掩盖了真正的领域错误。
  - 建议: 窄化为 `except OSError as e:` 并 `logger.warning(f"无法创建目录 {path}: {e}")`，保持“尽力创建、失败仍返回路径”的语义但留下诊断痕迹。
  - 置信度: 高（规则违反确凿；实际触发依赖环境异常）

- [AUD-36604] [Suggestion] frozen 模式以 dirname(sys.executable) 为根，PyInstaller onedir/.app 布局下资源目录未必在此处
  - 位置: src/shared/paths/paths.py:21-22
  - 证据:
    ```python
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
    ```
  - 影响: packaging/pyinstaller/*.spec 以 `a.datas` 打包资源。PyInstaller 6+ onedir 将 datas 放在可执行文件旁的 `_internal/` 子目录，macOS .app 包则可能位于 `Contents/Frameworks` 或 `Contents/Resources`；`dirname(sys.executable)` 与资源实际落点不一定重合，打包版可能同样解析到空资源目录（与 36601 叠加）。
  - 建议: 打包场景优先使用 `sys._MEIPASS`（onefile）或在 spec 中显式注入资源根环境变量。
  - 置信度: 低（取决于最终打包布局，未实际构建验证）

### 通过项
- 线程安全：无共享可变状态，全为纯函数 + 幂等 `os.makedirs(exist_ok=True)`，可任意线程调用。
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性：不涉及（纯路径工具，无 Qt/OpenGL/undo）。
- 输入验证：`get_models_dir(subdir)` 的 subdir 仅被内部以常量（"denoise"/"deblur"）调用，路径拼接风险低；get_project_root 对 `sys.frozen` 有防御性 getattr。
- get_hdri_dir 本身回溯 4 层正确（注释明确说明，根目录 `hdri/` 存在佐证）。
- 性能模式：O(1) 路径拼接，无性能问题。
- 类型注解：全部函数有参数与 `-> str` 返回注解。

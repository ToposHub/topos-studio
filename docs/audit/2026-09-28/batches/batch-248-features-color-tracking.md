# batch-248 [P2] src/features/color/tracking//

编号区间：AUD-24800–AUD-24899

## src/features/color/tracking/__init__.py (行数: 26)

### 发现

（无）

### 通过项
- 通过（re-export 层）：导出 `build_corner_pin_export` / `validate_corner_pin` / `write_corner_pin_export` 三个符号，已核实目标实现存在于 `src/shared/tracking/corner_pin.py:239 / 275 / 341`，导入不会 AttributeError；`__all__` 与之一致。
- 文档说明准确：docstring 明确纯算法已迁 `src.shared.tracking`，本包保留 Qt/会话耦合部分（worker/service/store/evaluator 不在本批）。
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交 / 异常路径 / 输入验证 / 性能：无逻辑，不适用。

## src/features/color/tracking/coordinate_map.py (行数: 14)

### 发现
- [AUD-24801] [Suggestion] 垫片 re-export 循环会把实现模块的 `__all__` 与第三方模块别名一并拷入垫片命名空间
  - 位置: src/features/color/tracking/coordinate_map.py:11-14（同款垫片模式同样适用于本批其余 6 个文件：corner_pin.py / feature_detector.py / frame_sampler.py / homography_stability.py / merge.py / point_manager.py，以及 batch-250 的 types.py）
  - 证据:
    ```python
    # Re-export private/underscore names too (some callers reach into helpers).
    for _name in dir(_impl):
        if _name not in dir():
            globals()[_name] = getattr(_impl, _name)
    ```
  - 影响: `dir(_impl)` 除私有助手外还包含实现模块导入的第三方名（numpy/cv2 等别名）与 `__all__` 自身，全部进入垫片 globals：垫片命名空间比「私有名 re-export」注释宣称的更宽，`from 垫片 import os` 之类意外可用，静态分析与重构工具会把依赖面算大。另 `_name not in dir()` 对 list 做 O(n) 查找，整体 O(n²)（导入期一次性、量级小，仅记录不计 severity）。
  - 建议: 循环改用 `vars(_impl)` 并过滤 `startswith('_')` 的已知私有助手白名单，或为垫片显式声明 `__all__`。
  - 置信度: 高（Python 语义确定；影响属质量层面）

### 通过项
- 通过（无逻辑）：star-import + 定向 re-export 的兼容垫片，目标 `src.shared.tracking.coordinate_map` 存在（已核实目录），无自有控制流。

## src/features/color/tracking/corner_pin.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同款兼容垫片，re-export `src.shared.tracking.corner_pin`；AUD-24801 的模式问题同样适用，不重复计。目标模块存在（corner_pin.py 380+ 行实现）。

## src/features/color/tracking/feature_detector.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同款兼容垫片，re-export `src.shared.tracking.feature_detector`（目标存在）；AUD-24801 同样适用，不重复计。

## src/features/color/tracking/frame_sampler.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同款兼容垫片，re-export `src.shared.tracking.frame_sampler`（目标存在）；AUD-24801 同样适用，不重复计。

## src/features/color/tracking/homography_stability.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同款兼容垫片，re-export `src.shared.tracking.homography_stability`（目标存在）；AUD-24801 同样适用，不重复计。

## src/features/color/tracking/merge.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同款兼容垫片，re-export `src.shared.tracking.merge`（目标存在）；AUD-24801 同样适用，不重复计。

## src/features/color/tracking/point_manager.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：同款兼容垫片，re-export `src.shared.tracking.point_manager`（目标存在）；AUD-24801 同样适用，不重复计。

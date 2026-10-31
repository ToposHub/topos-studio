# batch-385 审查报告：src/shared/utils（AUD-38500–AUD-38599）

审查人维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

## src/shared/utils/__init__.py (行数: 3)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export，导出 `get_largest_inscribed_rect`，`__all__` 与导出一致。

## src/shared/utils/geometry_math.py (行数: 99)

### 发现

- [AUD-38501] [Critical] 0°/90° 边界分支的宽高约束互换，返回的“安全区域”可大于原图，裁切框获准越出图像边界
  - 位置: src/shared/utils/geometry_math.py:67-75（0° 分支）、77-83（90° 分支，同一互换错误）
  - 证据:
    ```python
        if sin_a < 1e-6:
            # At 0°, the inscribed rect equals the original bounds
            # But we need to respect aspect ratio
            if aspect_ratio >= w / h:
                # Aspect ratio is wider than image, constrain by height
                return float(h * aspect_ratio), float(h)
            else:
                # Aspect ratio is taller than image, constrain by width
                return float(w), float(w / aspect_ratio)
    ```
  - 影响: 数学上 0° 时正确解为 `w_c = min(w, h*ar)`：当 ar ≥ w/h（预设比图像更宽）时受宽度限制，应返回 `(w, w/ar)`；当 ar < w/h 时受高度限制，应返回 `(h*ar, h)`——代码两分支恰好写反。反例：1000×1000 图 + 2:1 预设 → 代码返回 (2000, 1000)，宽度超出原图一倍；1920×1080 图 + 1:1 预设 → 返回 (1920, 1920)，高度超出原图 77%。该分支恰在最常见路径上触达（rotation_val=0 + 固定比例预设）。下游 canvas.py:439（`_apply_crop_constraints`）用该结果做位置钳制：canvas.py:466-476 `max_offset = (safe - final)/2`，被膨胀的 safe 尺寸允许裁切框中心偏移到图像外，应用裁切时采样到图像外区域（渲染错误：黑边/透明边/越界裁切结果）。90° 分支（80-83 行）存在同样的互换错误，仅当角度 ≈89.9999°（mod 90° 后 cos < 1e-6）时触达，实际影响小但同源。
  - 建议: 删除两个特判分支，统一走通用公式（`sin_a < eps` 时公式极限自然退化），或直接交换两分支的返回值；补单元测试：`(1000, 1000, 0, 2.0) → (1000, 500)`、`(1920, 1080, 0, 1.0) → (1080, 1080)`、任意角度返回值必须满足 `w_c <= w` 且 `h_c <= h`。
  - 置信度: 高（推导明确，且下游调用点 canvas.py:439/466-476 已核实）

- [AUD-38502] [Suggestion] 类型注解不正确：aspect_ratio: float = None 应为 Optional[float]，返回类型 tuple 未参数化
  - 位置: src/shared/utils/geometry_math.py:15
  - 证据:
    ```python
    def get_largest_inscribed_rect(w: float, h: float, angle_degrees: float, aspect_ratio: float = None) -> tuple:
    ```
  - 影响: 默认值 None 与 `float` 注解冲突（mypy/pyright strict 模式报错）；裸 `tuple` 无元素信息，调用方无法获得返回形状提示。
  - 建议: 改为 `aspect_ratio: Optional[float] = None`（或 `float | None`）、`-> tuple[float, float]`。
  - 置信度: 高

- [AUD-38503] [Suggestion] docstring 示例数值与实际返回不符，误导维护者
  - 位置: src/shared/utils/geometry_math.py:46-52
  - 证据:
    ```python
            >>> get_largest_inscribed_rect(1920, 1080, 15, aspect_ratio=16/9)
            (1732.5..., 974.5...)
            >>> get_largest_inscribed_rect(1920, 1080, 45)
            (792.0..., 792.0...)
    ```
  - 影响: 按实现计算，45°/默认比例实际返回 ≈(977.5, 550.3)（保持 16:9），`(792.0, 792.0)` 是正方形且与任何参数组合都对不上；15° 示例实际 ≈(1727.4, 971.4)，与所写 (1732.5, 974.5) 也有偏差。示例显然未经运行验证，会掩盖 38501 一类的几何错误并误导后续维护。
  - 建议: 以真实运行输出修正全部示例，并接入 doctest 由 CI 校验。
  - 置信度: 高

### 通过项
- 输入验证：`w <= 0 or h <= 0` 返回 (0.0, 0.0)；`aspect_ratio` 为 None 或 <= 0 时回退图像自身比例。
- 角度归一化：`abs(radians(angle)) % (pi/2)` 正确处理正/负角与 >90° 角（如 270° 归一到 0°）。
- 通用分支（88-97 行）公式正确：双投影约束取 min，代入 h_c = w_c/ar 的推导与实现一致。
- 线程安全：纯函数、无共享状态，可任意线程调用。
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常路径：不涉及（纯数学工具，无 IO、无 Qt、无 OpenGL、无状态）。
- 性能模式：O(1) 纯算术，无热路径问题；`abs(wr)/abs(hr)`（99 行）冗余但无害（wr、hr 恒为正）。

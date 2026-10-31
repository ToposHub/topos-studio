# 最终剩余问题优化计划：架构边界 / GPU Frame 生命周期 / 底层 GL-YUV 测试闭环（2026-06-04）

## 0. 当前基准

本计划承接 `explicit_comp_clip_and_topos_color_unification_plan_2026-06-03.md`、`explicit_comp_clip_remaining_optimization_plan_2026-06-03.md` 以及本轮验收后的剩余问题。

当前 Edit / Color / Composite 主链路状态：

- 显式 Comp Clip 创建与打开：核心测试已通过。
- `Timeline Clip Input -> Topos Color -> Output` 默认图：核心测试已通过。
- Color 页面修改 Topos Color 参数、Edit / Composite 上下文切换：核心测试已通过。
- Render Cache 生命周期模型、混合帧率 cadence、Program / Source Viewer 缩放平移：核心测试已通过。

本轮已通过的关键验收命令：

```bash
.venv/bin/python -m pytest \
  tests/app/test_composite_graph_open_context.py \
  tests/app/test_no_pending_comp_clip_context_in_page_switch.py \
  tests/edit/test_comp_clip_promotion_lifecycle.py \
  tests/edit/test_comp_clip_reset.py \
  tests/edit/test_comp_clip_timeline_preview.py \
  tests/edit/test_comp_clip_timeline_export.py \
  tests/edit/test_render_cache_job_lifecycle.py \
  tests/edit/test_mixed_fps_playback_cadence.py \
  tests/edit/test_program_viewer_transform_contract.py \
  tests/edit/test_source_viewer_transform_contract.py \
  tests/composite/test_default_graph_topos_color_contract.py \
  tests/composite/test_timeline_clip_input_render_contract.py \
  tests/composite/test_composite_graph_render_service_contract.py \
  tests/composite/test_no_sync_readback_on_page_switch.py \
  tests/project/test_topos_schema_v3.py \
  tests/project/test_comp_clip_save_integrity.py -q
```

结果：

```text
260 passed
```

全量测试最新基准：

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
6308 passed, 42 failed, 84 skipped
```

剩余 42 个失败不是同一类问题，不能用一次性大改解决。后续 AI 必须按本文阶段顺序处理。

---

## 1. 剩余问题总表

| ID | 优先级 | 失败数量 | 问题 | 性质 | 完成标准 |
|---|---:|---:|---|---|---|
| R-A1 | P0 | 1 | `src/app` 直接 import `src.features.*.core/render/adapters` | 架构边界问题 | `test_app_layer_has_no_forbidden_feature_imports` 通过 |
| R-B1 | P0 | 2 | `GpuDecodedFrame.__del__` 测试期望析构自动释放，但生产代码选择只报警 | 生命周期策略冲突 | 策略明确，测试与实现一致，不能跨线程释放 GPU 资源 |
| R-C1 | P2 | 1 | `test_nested_with_statements` 本身未使用 `nonlocal`，断言永远失败 | 测试代码错误 | 测试改为真实验证嵌套 context cleanup |
| R-D1 | P1 | 3 | `VideoTexturePool` real texture 测试拿到 `MagicMock.size` | GL 测试夹具/能力检测问题 | real GL 测试只跑真实 context，mock 测试使用 deterministic fake texture |
| R-D2 | P1 | 7 | `video_upload_command` 纹理 filter/size、连续上传性能失败 | GL 测试夹具 + 性能阈值问题 | 常量一致，性能测试不再使用硬 1s 脆弱阈值 |
| R-D3 | P1 | 3 | `yuv_chroma_sampling_pixels` readback 返回 `MagicMock` | GL readback fake/skip 问题 | readback 返回 bytes 或 real GL 不可用时 skip |
| R-D4 | P1 | 15 | `yuv_gpu_upload` 纹理 size/readback/shader 创建失败 | GL/YUV fake context 不完整 | YUV uploader 单元测试可在 fake GL 下稳定通过 |
| R-D5 | P1 | 1 | `yuv_temp_texture_release` texture pool key 使用 MagicMock width/height | GL fake texture 属性问题 | pool key 使用真实 int，释放后纹理可复用 |
| R-D6 | P1 | 6 | `yuv_upload_api` size/glo/readback 失败 | GL fake context 不完整 | API 测试稳定，不依赖 MagicMock 默认行为 |

---

## 2. Phase 1：修复 App 层架构边界（R-A1）

### 2.1 问题现状

失败测试：

```text
tests/architecture/test_boundary_guardrails.py::TestAppLayerForbiddenImports::test_app_layer_has_no_forbidden_feature_imports
```

当前违规 import 示例：

```text
src/app/controllers/edit_controller.py
  from src.features.edit.core.composite_graph_data import CompositeGraphData
  from src.features.edit.core.render_cache import ...
  from src.features.edit.core.render_cache_worker import RenderCacheWorker

src/app/controllers/file_controller.py
  from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
  from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

src/app/controllers/composite_controller.py
  from src.features.composite.render.composite_renderer import CompositeCommand
  from src.features.composite.core.runtime_context import CompositeRuntimeContext
  from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
```

`src/app` 是产品 shell，不应该直接依赖 feature 内部 core/render/adapters。否则产品裁剪、模块隐藏、Color-only 构建都会重新变脆。

### 2.2 目标架构

```text
src/app/controllers/*
  -> src.features.edit.facade
  -> src.features.composite.facade
  -> feature 内部 core/render/ui
```

`src/app` 只允许依赖：

```text
src.features.edit.facade
src.features.edit.module
src.features.composite.facade
src.features.composite.module
src.features.color.facade / module
```

不允许：

```text
src.features.edit.core.*
src.features.edit.ui.*
src.features.composite.core.*
src.features.composite.render.*
src.features.composite.adapters.*
```

### 2.3 具体实施步骤

#### 步骤 1：建立 Edit facade

文件：

```text
src/features/edit/facade.py
```

新增或扩展 facade API：

```python
def load_composite_graph_data(data: dict) -> object:
    """Return CompositeGraphData from serialized dict."""

def create_render_cache_manifest(...):
    """Create RenderCacheManifest without app importing core class."""

def start_render_cache_worker(...):
    """Create and return RenderCacheWorker or a small adapter object."""

def create_edit_gpu_decode_manager():
    """Factory for EditGpuDecodeManager."""

def create_edit_gpu_preview_renderer(main_context, parent=None):
    """Factory for EditGpuPreviewRenderer."""

def create_edit_render_task(...):
    """Factory for EditRenderTask."""

def create_timeline_clip_frame_provider():
    """Factory for TimelineClipFrameProvider."""
```

注意：

- app 层如果只需要 dict，不要把 core dataclass 类型泄漏到 app。
- facade 返回对象可以是 feature 内部对象，但 app 文件不能 import 其具体类。
- facade 内部可以 import `src.features.edit.core.*`。

#### 步骤 2：建立 Composite facade

文件：

```text
src/features/composite/facade.py
```

新增或扩展：

```python
def make_composite_command(name: str, payload: dict):
    """Return CompositeCommand without app importing render.composite_renderer."""

def make_timeline_runtime_context(...):
    """Return CompositeRuntimeContext.for_timeline_clip(...)."""
```

如果当前 `src/features/composite/facade.py` 已存在，必须复用，不要新建平行 facade。

#### 步骤 3：替换 app controllers 的深层 import

涉及文件：

```text
src/app/controllers/edit_controller.py
src/app/controllers/file_controller.py
src/app/controllers/composite_controller.py
```

替换规则：

```text
from src.features.edit.core.xxx import Y
  -> from src.features.edit import facade as edit_facade

from src.features.composite.core/render.xxx import Y
  -> from src.features.composite import facade as composite_facade
```

不要用局部 import 绕过扫描。测试是文本扫描，局部 import 一样会失败。

#### 步骤 4：避免类型注解重新引入深层 import

如果 app 层只为了类型注解引入 core 类型，改用：

```python
from typing import Any, Protocol
```

或在 facade 暴露 `Protocol`。不要在 app 层写：

```python
if TYPE_CHECKING:
    from src.features.edit.core...
```

因为当前 guardrail 扫描纯文本，`TYPE_CHECKING` 也会被抓到。

### 2.4 验证方法

先跑：

```bash
.venv/bin/python -m pytest \
  tests/architecture/test_boundary_guardrails.py::TestAppLayerForbiddenImports::test_app_layer_has_no_forbidden_feature_imports -q
```

再跑 app / Edit / Composite 关键链路：

```bash
.venv/bin/python -m pytest \
  tests/app/test_composite_graph_open_context.py \
  tests/edit/test_comp_clip_timeline_preview.py \
  tests/edit/test_comp_clip_timeline_export.py \
  tests/edit/test_render_cache_job_lifecycle.py \
  tests/composite/test_timeline_clip_input_render_contract.py -q
```

完成标准：

```text
0 forbidden imports
关键链路测试全部通过
```

---

## 3. Phase 2：统一 GpuDecodedFrame 生命周期策略（R-B1）

### 3.1 问题现状

失败测试：

```text
tests/media/test_gpu_frame.py::TestGpuDecodedFrame::test_frame_destructor_releases
tests/media/test_gpu_frame.py::TestGpuDecodedFrameQueueLifetime::test_frame_destructor_auto_release
```

当前生产代码：

```python
def __del__(self):
    if not self.release.is_released:
        logger.warning("leaked ... Call safe_release_gpu_frame() from the render thread.")
```

测试期望：

```python
del frame
mock_callback.assert_called_once()
```

这是策略冲突，不是简单断言错误。

### 3.2 必须坚持的原则

GPU resource release 不能默认在 `__del__` 执行，因为：

- `__del__` 可能发生在任意 Python GC 线程。
- release callback 可能包含 OpenGL / CVPixelBuffer / IOSurface / D3D11 操作。
- 这些操作通常必须在渲染线程或拥有对应 context 的线程执行。

因此不要直接把 `GpuDecodedFrame.__del__` 改成无条件：

```python
self.release.release()
```

这会让测试通过，但可能重新引入真实播放崩溃。

### 3.3 推荐策略

引入显式释放策略字段：

文件：

```text
src/shared/media/gpu_frame.py
```

修改 `GpuFrameRelease`：

```python
@dataclass
class GpuFrameRelease:
    retain_token: object | None = None
    release_callback: Optional[Callable[[], None]] = None
    fence: object | None = None
    destructor_policy: str = "warn_only"
```

允许值：

```text
warn_only
  默认。__del__ 只报警，不释放真实 GPU 资源。

thread_safe
  release_callback 明确是线程安全的，__del__ 可调用 release()。
  只用于 tests、CPU-only fake、或 release callback 已经封送到安全线程。

enqueue
  __del__ 把 release 请求投递到全局 release queue，由渲染线程 drain。
  如果项目已有 render-thread command queue，优先接入；否则本阶段可以先不实现。
```

`GpuDecodedFrame.__del__` 行为：

```python
if release.is_released:
    return
if release.destructor_policy == "thread_safe":
    release.release()
elif release.destructor_policy == "enqueue":
    enqueue_gpu_release(release)
else:
    logger.warning(...)
```

### 3.4 更新调用点

搜索：

```bash
rg -n "GpuFrameRelease\\(|GpuDecodedFrame\\(" src tests
```

生产代码默认不传 `destructor_policy`。

测试里如果要验证析构自动释放，应显式写：

```python
release = GpuFrameRelease(
    release_callback=mock_callback,
    destructor_policy="thread_safe",
)
```

并新增测试验证默认策略：

```python
def test_destructor_warn_only_by_default(caplog):
    release = GpuFrameRelease(release_callback=mock_callback)
    frame = make_frame(release=release)
    del frame
    mock_callback.assert_not_called()
    assert "leaked" in caplog.text
```

### 3.5 验证方法

```bash
.venv/bin/python -m pytest tests/media/test_gpu_frame.py -q
.venv/bin/python -m pytest tests/media/test_resource_leak.py tests/media/test_video_frame_queue.py -q
```

手工验证：

1. 打开视频并播放 30 秒。
2. 快速切换 Edit / Color / Composite。
3. 观察日志不能出现 GPU frame 重复释放或崩溃。
4. 停止播放、关闭项目，确认没有持续增长的 GPU frame leak warning。

---

## 4. Phase 3：修复错误的资源清理测试（R-C1）

### 4.1 问题现状

失败测试：

```text
tests/video/test_resource_cleanup_on_exception.py::TestExceptionPathResourceCleanup::test_nested_with_statements
```

当前测试代码在嵌套类方法里写：

```python
inner_released = False

class InnerResource:
    def __exit__(...):
        inner_released = True
```

这在 Python 中会创建局部变量，不会修改外层变量，所以断言永远失败。

### 4.2 实施步骤

文件：

```text
tests/video/test_resource_cleanup_on_exception.py
```

把布尔变量改成 dict/list 或 `nonlocal` 可见状态：

```python
state = {"outer": False, "inner": False}

class OuterResource:
    def __exit__(self, exc_type, exc_val, exc_tb):
        state["outer"] = True

class InnerResource:
    def __exit__(self, exc_type, exc_val, exc_tb):
        state["inner"] = True
```

不要修改生产代码来适配这个测试。

### 4.3 验证方法

```bash
.venv/bin/python -m pytest tests/video/test_resource_cleanup_on_exception.py -q
```

---

## 5. Phase 4：重建底层 GL / YUV 测试夹具（R-D1 ~ R-D6）

### 5.1 问题现状

剩余大多数失败都不是 Edit / Color / Composite 业务逻辑，而是底层 GL/YUV 测试环境不可靠。

典型症状：

```text
assert tex.size == (1920, 1080)
实际 tex.size 是 MagicMock

np.frombuffer(data, dtype=np.float16)
实际 data 是 MagicMock，不是 bytes

assert y_tex.glo > 0
实际 y_tex.glo 是 MagicMock

assert 'y_plane' in shader
实际 shader 是 ctx.program() 返回的 MagicMock
```

根因：

1. 测试以为拿到了 real ModernGL context，但实际是 MagicMock context。
2. MagicMock texture 没有稳定的 `size/width/height/filter/glo/read()` 行为。
3. 有些测试应该是 unit test，却依赖 real GL readback。
4. 有些测试应该是 real GL integration test，却没有可靠 skip 条件。

### 5.2 目标

建立两套明确测试模式：

```text
Unit GL fake mode
  使用 deterministic fake ModernGL context。
  不需要真实 GPU。
  texture.size/filter/glo/read/program 都有稳定行为。

Real GL integration mode
  只在真实 ModernGL context 可用时运行。
  如果 ctx 是 MagicMock 或 readback 不是 bytes，必须 skip。
```

不要让同一测试同时依赖 MagicMock 和真实 GL 行为。

### 5.3 新增 Fake ModernGL 工具

新增文件：

```text
tests/video/fakes/fake_moderngl.py
```

实现：

```python
class FakeTexture:
    def __init__(self, size, components, dtype="f1", data=None):
        self.size = tuple(size)
        self.width = int(size[0])
        self.height = int(size[1])
        self.components = components
        self.dtype = dtype
        self.glo = next_id()
        self.filter = (LINEAR, LINEAR)
        self._data = bytes(...)

    def write(self, data):
        self._data = bytes(data)

    def read(self, *args, **kwargs):
        return self._data or zero_bytes_for_size(...)

    def release(self):
        self.released = True


class FakeProgram:
    def __init__(self, vertex_shader="", fragment_shader=""):
        self.vertex_shader = vertex_shader
        self.fragment_shader = fragment_shader

    def __contains__(self, text):
        return text in self.vertex_shader or text in self.fragment_shader


class FakeContext:
    def texture(self, size, components, dtype="f1", data=None):
        return FakeTexture(size, components, dtype, data)

    def buffer(self, data=None, reserve=0):
        return FakeBuffer(data=data, reserve=reserve)

    def program(self, vertex_shader="", fragment_shader=""):
        return FakeProgram(vertex_shader, fragment_shader)
```

常量：

```python
LINEAR = 9729
NEAREST = 9728
```

注意：

- FakeTexture 的 `size` 必须是 `(width, height)`。
- FakeTexture 的 `glo` 必须是正整数。
- `read()` 必须返回 bytes，不允许返回 MagicMock。
- FakeProgram 支持 `'y_plane' in shader` 这类测试。

### 5.4 新增 GL capability helper

新增文件：

```text
tests/video/gl_test_utils.py
```

实现：

```python
def is_magic_mock_context(ctx) -> bool:
    return ctx.__class__.__module__.startswith("unittest.mock")

def require_real_gl_context(ctx):
    if is_magic_mock_context(ctx):
        pytest.skip("real GL context unavailable; got MagicMock")
    if not hasattr(ctx, "texture") or not hasattr(ctx, "program"):
        pytest.skip("invalid GL context")
```

所有 real GL 测试开头调用：

```python
require_real_gl_context(gl_context)
```

### 5.5 改造测试分类

#### Unit fake 测试

这些测试应改用 `FakeContext`：

```text
tests/video/test_video_texture_pool.py
tests/video/test_video_upload_command.py::TestNoMipmapForVideo
tests/video/test_video_upload_command.py::TestP4AVideoPlanesUpload
tests/video/test_yuv_gpu_upload.py::TestYUVTexturePool
tests/video/test_yuv_gpu_upload.py::TestYUVGPUUploader
tests/video/test_yuv_upload_api.py::TestYUVGPUUpload
tests/video/test_yuv_upload_api.py::TestYUVUploadIntegration
```

替换方式：

```python
from tests.video.fakes.fake_moderngl import FakeContext, LINEAR

ctx = FakeContext()
```

如果测试需要 patch `moderngl.LINEAR`，统一 patch 成 `LINEAR`，不要让它是 MagicMock。

#### Real GL integration 测试

这些测试如果必须 readback 真实像素，应保留 real GL，但加 skip：

```text
tests/video/test_yuv_chroma_sampling_pixels.py
tests/video/test_yuv_gpu_upload.py::test_upload_yuv_planar_12bit_neutral_gray_renders_correctly
tests/video/test_yuv_gpu_upload.py::test_r16ui_12bit_neutral_gray_renders_correctly
tests/video/test_yuv_temp_texture_release.py
```

开头加：

```python
require_real_gl_context(gl_context)
```

并在 readback 后检查：

```python
raw = fbo.read(...)
if not isinstance(raw, (bytes, bytearray, memoryview)):
    pytest.skip("real GL readback unavailable")
```

### 5.6 修复 texture pool key

失败示例：

```text
FramebufferManager._texture_pool key = (MagicMock width, MagicMock height, MagicMock components, MagicMock dtype)
```

涉及文件可能是：

```text
src/shared/gpu/gl_backend.py
src/shared/video/video_texture_pool.py
src/shared/video/yuv_gpu_upload.py
```

实施要求：

1. 创建 texture 时记录请求尺寸：

```python
requested_key = (int(width), int(height), int(components), str(dtype))
```

2. 释放回池时不要从 `tex.width/tex.height/tex.dtype` 重新推断，优先使用创建时记录的 metadata。
3. 如果必须从 texture 读属性，先做类型校验：

```python
if not isinstance(tex.width, int):
    width = requested_width
```

### 5.7 修复性能阈值测试

失败：

```text
TestFrameSequenceUpload.test_continuous_upload_performance
assert elapsed < 1.0
实际 1.02s
```

处理方式：

- 不要把单机性能测试作为普通单元测试硬阈值。
- 改成结构性断言：

```python
assert uploaded_count == expected
assert avg_upload_ms < reasonable_per_frame_budget
```

或加 marker：

```python
@pytest.mark.benchmark
```

默认测试只验证不会异常、资源释放正确。

### 5.8 验证方法

分阶段跑：

```bash
.venv/bin/python -m pytest tests/video/test_video_texture_pool.py -q
.venv/bin/python -m pytest tests/video/test_video_upload_command.py -q
.venv/bin/python -m pytest tests/video/test_yuv_gpu_upload.py -q
.venv/bin/python -m pytest tests/video/test_yuv_upload_api.py -q
.venv/bin/python -m pytest tests/video/test_yuv_chroma_sampling_pixels.py -q
.venv/bin/python -m pytest tests/video/test_yuv_temp_texture_release.py -q
```

完成标准：

```text
Unit fake GL tests: pass
Real GL tests: pass on capable machine, skip on unavailable context
No MagicMock bytes/size/filter/glo assertions
```

---

## 6. Phase 5：重新跑全量并回归主链路

### 6.1 全量测试

```bash
.venv/bin/python -m pytest -q
```

目标：

```text
0 failed
允许 skip，但 skip 必须有明确 reason
```

### 6.2 主链路必须保持全绿

```bash
.venv/bin/python -m pytest \
  tests/app/test_composite_graph_open_context.py \
  tests/app/test_no_pending_comp_clip_context_in_page_switch.py \
  tests/edit/test_comp_clip_promotion_lifecycle.py \
  tests/edit/test_comp_clip_reset.py \
  tests/edit/test_comp_clip_timeline_preview.py \
  tests/edit/test_comp_clip_timeline_export.py \
  tests/edit/test_render_cache_job_lifecycle.py \
  tests/edit/test_mixed_fps_playback_cadence.py \
  tests/edit/test_program_viewer_transform_contract.py \
  tests/edit/test_source_viewer_transform_contract.py \
  tests/composite/test_default_graph_topos_color_contract.py \
  tests/composite/test_timeline_clip_input_render_contract.py \
  tests/composite/test_composite_graph_render_service_contract.py \
  tests/composite/test_no_sync_readback_on_page_switch.py \
  tests/project/test_topos_schema_v3.py \
  tests/project/test_comp_clip_save_integrity.py -q
```

目标：

```text
全部通过
```

### 6.3 手工验收

1. 启动 app。
2. 导入视频素材到 Edit。
3. 右键显式创建 Comp Clip。
4. 打开 Composite，确认图为 `Timeline Clip Input -> Topos Color -> Output`。
5. 添加一个明显合成效果，返回 Edit，确认预览能看到合成结果或 cache 状态明确。
6. 去 Color 调色，返回 Composite，确认 Topos Color 节点输出更新且画面不翻转。
7. 在 Edit 中切换不同 clip，再打开 Composite，确认上下文切换到当前 clip。
8. 播放含不同帧率素材的时间线，确认无明显抖动。
9. 拖拽 Program / Source Viewer，确认滚轮缩放、中键平移可用。
10. 打开 Preferences，确认 render cache 设置不会破坏项目保存。

---

## 7. 不要做的事

1. 不要为了通过 guardrail 在 app controller 里加 `# noqa`，测试是纯文本扫描，且架构问题仍存在。
2. 不要把 `GpuDecodedFrame.__del__` 改成无条件 release。
3. 不要让 MagicMock 继续伪装成 real GL context。
4. 不要把 real GPU readback 测试放在普通 unit test 里无条件运行。
5. 不要因为底层 YUV 测试失败去改 Edit / Color / Composite 主链路。
6. 不要一次性重写 YUV uploader；先修测试夹具和能力检测，再判断是否有真实生产 bug。

---

## 8. 建议执行顺序

```text
1. R-C1：修复明显错误的 nested context 测试
2. R-A1：建立/扩展 facade，清除 app 深层 feature import
3. R-B1：明确 GpuDecodedFrame destructor policy，并同步测试
4. R-D1 ~ R-D6：建立 FakeContext + real GL skip 工具
5. 修复 YUV / texture pool 单测
6. 跑全量 pytest
7. 跑手工 Edit / Color / Composite 验收
```

这样排序的原因：

- R-C1 最小，能马上减少噪声。
- R-A1 会影响 app controllers，需要早做，避免后续 AI 继续写深层 import。
- R-B1 是真实资源安全策略，必须先定规则。
- R-D 系列很多失败是同一测试夹具根因，先建 fake/skip 基础设施，再逐个测试文件收口。

---

## 9. 当前结论

当前产品主功能已经比上一轮稳定很多，尤其 Edit / Color / Composite 的显式 Comp Clip 与 Topos Color 主链路已经有比较扎实的测试保护。

剩余问题的重点不再是“Composite 页面能不能显示”这一类用户交互主链路，而是：

1. 产品模块化边界还没彻底收口。
2. GPU frame 生命周期策略需要和测试统一。
3. 底层 GL / YUV 测试体系需要从 MagicMock 过渡到 deterministic fake + real GL skip。

完成本计划后，项目才可以真正进入“全量测试无红灯”的状态。

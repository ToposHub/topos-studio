# EFR 报告 — src/shared/media 机械清理（E-rollup 模块行）

- 日期：2026-10-01
- 范围：`src/shared/media/`（含 `macos_vt/`、`windows_hw/` 子包，246 个 .py 文件，约 46,370 行）
- 性质：纯机械清理，零行为变化
- 分支：dev（工作树留改动，未 add/commit）

---

## 一、分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 删除未使用 import | **56 条** | 分布在 28 个文件；AST + 全仓核实后删除 |
| 删除死代码 | **1 处** | `_length_prefixed_to_annex_b`（vt_compression.py，全仓零引用） |
| 补类型注解 | **12 处** | 仅补可证明的 `-> None`；主动跳过 `@pyqtSlot run` |
| 命名收敛 | **0 处** | 主动放弃：media 模块被 timeline/render 广泛调用，公共 API 风险不可接受 |
| 小清理（冗余变量/重复字面量） | **0 处** | 主动放弃：无法在不引入行为歧义的前提下机械证明安全 |
| 既有裸 except | **0 处** | 模块内本就为零（`grep -rn "except:" src/shared/media/` = 0 命中） |
| **改动文件总数** | **30 个** | 28 个（import 清理）+ vt_compression.py（死代码）+ errors.py（仅注解） |

### 未使用 import 的甄别漏斗（232 → 56）

AST 扫描（模块级绑定 + `__all__` 豁免）共发现 232 个候选，逐一经全仓 alias 精确核实（`from <模块> import N` 再导出、`import <模块>` 后属性访问链、`import *`、`# noqa`）后：

| 处置 | 数量 | 依据 |
|------|------|------|
| **删除** | 56 | 模块级、全仓无任何再导出/属性访问路径 |
| 保留：`from __future__ import annotations` | 72 | 删除会改变注解求值语义（行为红线），AST 误报 |
| 保留：`__init__.py` 再导出面 | 65 | 公共 API（`src.shared.media` / `windows_hw` 包出口） |
| 保留：惰性/嵌套作用域 import | 10 | 函数/类体内，可能是延迟加载防护 |
| 保留：`try/except ImportError` 防护 | 18 | 可选依赖防护，按任务要求保留 |
| 保留：`TYPE_CHECKING` 块 | 7 | 零运行时成本，保守保留 |
| 保留：de-facto 再导出 | 4 | 他处 `from <该模块> import 该名`（如 ffmpeg_source 的 COLOR_*_MAP 被 `__init__.py` 再导出） |

删除后复扫：232 → 176，**未引入任何新的未使用 import**；多名字合并行（如
`from typing import Optional, Callable, TYPE_CHECKING`）按原顺序重建，最小 diff。

---

## 二、抽样证据

### 1. 未使用 import（56 条 / 28 文件）

AST 判定 + 全仓 alias 解析核实，示例：

- `adapter.py:53` `from .factory import MediaBackend` — `MediaBackend` 在 adapter.py 内零引用；全仓无 `from ...adapter import MediaBackend`（`__init__.py` 直连 factory 再导出，不经过 adapter）
- `disk_proxy_service.py:58,60` `import os` / `import time` — 模块内无任何 `os.`/`time.` 属性链（AST 属性链扫描证实）
- `hwaccel.py:40` `from .ffmpeg_source import GOP_CODECS, INTRA_CODECS` — 全仓无经 hwaccel 的再导出
- `macos_vt/vt_session.py:40` `import time` — 同上核实
- `windows_hw/hw_planar_source.py` `from ...media.frame_path import RenderFramePath` — 仅保留同行的 `DecodePath`

多名字行重建示例：
```diff
-from .factory import MediaSourceFactory, MediaBackend
+from .factory import MediaSourceFactory
-from typing import Optional, Callable, TYPE_CHECKING
+from typing import Optional, TYPE_CHECKING
```

### 2. 死代码（1 处）

`src/shared/media/macos_vt/vt_compression.py` 的 `_length_prefixed_to_annex_b`（原 338-353 行）：

- 本文件 AST 零 Name/Attribute 引用
- 全仓全文件类型 grep：仅命中 `docs/audit/2026-09-28/ISSUES.md`（AUD-35210 本身即判定其为死代码）与 coverage.json 机器数据
- 无 `from ...vt_compression import *`

### 3. 类型注解（12 处，全部为可证明的 `-> None`）

判定规则：函数体无任何 `return`/`yield`（隐式返回 None）、非 Protocol/abstractmethod/overload、非生成器：

- `errors.py`：4 个异常类 `__init__`
- `thumbnail_provider.py`：`LRUCache.__init__/put/remove/clear`、`ThumbnailRequest.__init__`、`ThumbnailProvider.__init__`
- `macos_vt/iosurface_gl_bridge.py`：`_retain_cv_buffer` / `_release_cv_buffer`（仅声明级改动，未触碰 CoreVideo 调用序列）

主动跳过：`ThumbnailRequest.run`（`@pyqtSlot()` 装饰 — PyQt 会从注解推导槽签名，避免触碰 Qt 运行时元数据）。

---

## 三、验证输出

### 1. py_compile（全过）

```
$ git diff --name-only -- src/shared/media/ | xargs .venv/bin/python -m py_compile
PY_COMPILE OK (30 files)
```

### 2. 定向测试（相关既有测试文件）

批次 1（对应 import 清理热点文件）：

```
tests/media/test_disk_proxy.py            （→ disk_proxy_service.py，7 条删除）
tests/media/test_media_source_factory.py  （→ factory.py / adapter.py）
tests/media/test_decode_worker.py         （→ decode_queue.py）
tests/media/test_hwaccel_vendor_routing.py（→ hwaccel.py）
=> 88 passed, 1 skipped（平台门控）, 0 failed
```

批次 2（对应注解/死代码文件）：

```
tests/media/test_thumbnail_cache_key.py     （→ thumbnail_provider.py）
tests/media/test_macos_vt_compression.py    （→ vt_compression.py）
tests/media/test_macos_vt_zero_copy.py      （→ iosurface_gl_bridge.py）
=> 91 passed, 2 skipped, 3 failed
```

批次 2 的 3 个失败为 **既有失败，与本清理无关**，证据链：

1. 失败用例均为 `use_default_factory=True` 路径（`test_timeline_exporter_native_bridge_multi_frame[True-h264|True-hevc]`、`test_timeline_exporter_native_bridge_multitrack_surface[True]`）；`[False-*]` 变体全部通过
2. 以 `git archive HEAD` 导出纯净 HEAD 副本（零工作树改动、零本任务改动）+ 相同未跟踪测试素材（`tests/test_videos/`）运行同 3 个用例 → **完全相同的 3 个失败**
3. 失败路径涉及 `TimelineExporter` 默认工厂（src/features/edit/…，非本任务范围，且该文件在工作树中含其他任务的并行改动）

### 3. 裸 except 终检（零命中）

```
$ grep -rn "except:" src/shared/media/
（无输出）
bare-except hits: 0
```

本任务未引入任何 except；既有裸 except 数为零，无需记录豁免项。

### 4. 删除后复扫

```
未使用 import 候选：232 → 176（-56），新增候选：0
```

---

## 四、改动文件清单（30 个）

<details>
<summary>import 清理（28 个文件，56 条删除）</summary>

| 文件 | 删除数 |
|------|--------|
| src/shared/media/disk_proxy_service.py | 7 |
| src/shared/media/macos_vt/vt_session.py | 6 |
| src/shared/media/hwaccel.py | 5 |
| src/shared/media/macos_vt/source.py | 4 |
| src/shared/media/proxy_manager.py | 3 |
| src/shared/media/macos_vt/iosurface_gl_bridge.py | 3 |
| src/shared/media/adapter.py | 2 |
| src/shared/media/error_handling.py | 2 |
| src/shared/media/proxy_generator.py | 2 |
| src/shared/media/thumbnail_provider.py | 2 |
| src/shared/media/windows_hw/windows_zero_copy_probe.py | 2 |
| src/shared/media/macos_vt/vt_zero_copy_probe.py | 2 |
| src/shared/media/cache.py | 1 |
| src/shared/media/color_decode.py | 1 |
| src/shared/media/decode_queue.py | 1 |
| src/shared/media/factory.py | 1 |
| src/shared/media/ffmpeg_source.py | 1 |
| src/shared/media/frame_cache.py | 1 |
| src/shared/media/macos_vt/cv_metal_bridge.py | 1 |
| src/shared/media/macos_vt/packet_demux.py | 1 |
| src/shared/media/proxy_manifest.py | 1 |
| src/shared/media/windows_hw/d3d11_decoder.py | 1 |
| src/shared/media/windows_hw/d3d11_export_capability.py | 1 |
| src/shared/media/windows_hw/d3d11_passthrough_chain.py | 1 |
| src/shared/media/windows_hw/d3d11_pipeline.py | 1 |
| src/shared/media/windows_hw/d3d11_render_graph.py | 1 |
| src/shared/media/windows_hw/hw_planar_source.py | 1 |
| src/shared/media/windows_hw/native_bridge.py | 1 |

</details>

| 其他文件 | 改动 |
|----------|------|
| src/shared/media/macos_vt/vt_compression.py | 删除死代码 `_length_prefixed_to_annex_b`（16 行） |
| src/shared/media/errors.py | 4 处 `-> None` 注解 |
| （注解另覆盖 thumbnail_provider.py、iosurface_gl_bridge.py，已计入上表） | |

---

## 五、红线自查

- [x] 零行为变化：仅 import 行删除、死代码删除、`-> None` 注解；解码器调用序列、线程契约、601/709 矩阵相关代码未触碰（iosurface 仅加注解，属允许的声明级改动）
- [x] 未引入裸 `except:`，未修改任何既有 except 语义
- [x] 未重排函数；多名字 import 行按原顺序重建，最小 diff
- [x] 未改任何公共 API 名称（命名收敛项主动放弃）
- [x] 分支保持 dev；无 git add/commit/push；未修改 REMEDIATION_PLAN.md

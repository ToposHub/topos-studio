# 复查报告：efr-shared-media（E-rollup 机械清理批）

- 复查对象：提交 `7d1ddce4d39eabffe3aa6f5689bb604dfa866635`（模块行 `src/shared/media/`，含 macos_vt/、windows_hw/ 子包，92 条 process 级）
- 执行报告：`fixes/reports/efr-shared-media.md`
- 复查日期：2026-10-01（GLM5.3 复查会话，只读复核，未运行 pytest，未做任何 git 写操作）

## 七项判定

| # | 检查项 | 判定 | 依据 |
|---|--------|------|------|
| 1 | 越界 | **PASS** | 30 个 .py 全部在 `src/shared/media/`（含两子包）+ 1 个报告文件；tests/ 零触碰；REMEDIATION_PLAN.md 零触碰 |
| 2 | 未用导入删除 | **PASS** | 声称 56 条：46 名脚本核验 + 多行名手工核验，零代码残留；**再导出链逐一实查通过**（最高风险项，见明细） |
| 3 | 死代码删除 | **PASS** | `_length_prefixed_to_annex_b`：全仓 .py 零命中（唯一非代码命中为 audit/coverage 元数据，与报告取证一致） |
| 4 | 注解零行为 | **PASS** | 12 处 `-> None` 全声明级；`@pyqtSlot run` 主动跳过正确（PyQt 会从注解推导槽签名，防 Qt 元数据漂移） |
| 5 | 红线 | **PASS** | 零裸 except（模块本就 0 且无新增）；信号导入行重建保留 pyqtSignal（删 QMetaObject/Q_ARG）；CoreVideo/VideoToolbox/解码器调用序列零触碰（iosurface 仅加注解）；色彩矩阵/601/709 相关代码零触碰；公共 API 零改名 |
| 6 | py_compile | **PASS** | 全部 30 个改动源文件在当前 HEAD 下 `py_compile` 通过 |
| 7 | 综合结论 | **PASS-with-notes** | 清理本身全部通过；附 2 条 Note（见发现） |

## 抽查明细

### 未用导入（56 条）——含再导出链专项（shared/media 最高风险面）

脚本核验 46 名零代码残留。3 个告警澄清：
- adapter.py `MediaOpenError`：L119 为 docstring 参数说明文字，非代码。
- vt_session.py `time`：2 命中为注释内 "real-time"（连字符构成词边界），非代码。
- d3d11_passthrough_chain.py `ExportGpuTrail`：L80 `trail: "ExportGpuTrail"` 为引号注解，且文件有 `from __future__ import annotations`（L24），运行时零求值——行为安全（见 Note 2）。

多行删除手工核验（文件内零残留）：disk_proxy_service（os/time 等 7 条）、hwaccel（GOP_CODECS/INTRA_CODECS 等 5 条）、vt_session（time/field/Callable/List/Dict/Any 6 条，重建行 `from dataclasses import dataclass`、`from typing import Optional` 与 diff 一致）、proxy_manager（ProxyFormat/ProxyResolution/generate_proxy_key 3 条）。

**再导出链实查（关键项）**：

| 声称 | 实查结果 |
|------|---------|
| "`__init__.py` 直连 factory 再导出 MediaBackend，不经过 adapter" | 属实：`src/shared/media/__init__.py` 的 `from .factory import (MediaBackend, ...)` 直连；`from .adapter import (...)` 块仅 VideoDecoderAdapter/VideoDecoderWrapper，不含 MediaBackend |
| "GOP_CODECS/INTRA_CODECS 无经 hwaccel 再导出" | 属实：`__init__.py` 从 `.ffmpeg_source` 直连导入 GOP_CODECS/INTRA_CODECS（L171-172）；`from .hwaccel import (...)` 块不含二者；factory.py:1085 从 hwaccel 惰性导入的是 HardwareCapability 等 5 名，不含被删名 |
| "ffmpeg_source 的 COLOR_*_MAP 被 `__init__.py` 再导出故保留" | 属实：`__init__.py` ffmpeg_source 块含 COLOR_PRIMARIES_MAP/COLOR_TRANSFER_MAP/COLOR_SPACE_MAP/COLOR_RANGE_MAP（色彩元数据再导出面未被触碰） |

### 死代码

`_length_prefixed_to_annex_b`（vt_compression.py，16 行）：全仓 .py（src/tests/scripts/tools/apps）零命中；非 .py 零命中。净删除 def/class = 1，恰为该项。

### 注解（12 处 `-> None`）

errors.py 4 个异常类 `__init__`、thumbnail_provider LRUCache/ThumbnailRequest/ThumbnailProvider `__init__` 与 put/remove/clear、iosurface_gl_bridge `_retain_cv_buffer`/`_release_cv_buffer`——diff 显示仅签名行追加 `-> None`，CoreVideo 缓冲调用序列原样。`ThumbnailRequest.run`（@pyqtSlot）确认未加注解，跳过决策正确。

### 测试失败声明

报告记录批次 2 有 3 个失败并用 `git archive HEAD` 纯净副本复现证明为预存（TimelineExporter 默认工厂路径，非本批范围）。本次复查按规程不重跑 pytest；该取证方法（纯净副本对照）逻辑成立，且中心已统一复跑全绿。判定：非本提交引入。

## 发现

- **Critical：0；Suggestion：0；Note：2**
- Note 1（信息）：3 个预存测试失败的取证写法规范，但失败路径涉及 `src/features/edit/` 的 TimelineExporter——建议中心确认该预存失败已在后续批次修复（当前中心复跑全绿，应为已消化）。
- Note 2（一致性瑕疵）：d3d11_passthrough_chain.py 删除了 `ExportGpuTrail` 的导入，但签名保留引号注解 `"ExportGpuTrail"`。按本批自己声明的规则（"引号注解前向引用 = 有引用 = 保留导入"）应保留该导入；实际因 future-annotations 惰性求值而零行为影响，仅类型工具（pyright/mypy）会对此报错。不要求整改，记录备查。

## 结论

**PASS-with-notes**。56 条导入删除含再导出链逐项实查全部安全；死代码取证属实；注解与 @pyqtSlot 跳过决策正确；解码器调用序列与色彩元数据面零触碰；30 文件编译通过。两条 Note 均无行为影响，供中心知悉。

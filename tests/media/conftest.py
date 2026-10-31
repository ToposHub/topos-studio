"""tests/media 共享夹具。

N03（2026-09-27 复验）：完整 `test_topos_*.py` 组合运行曾出现同序列
两次编码摘要不一致 + 会话收尾 SIGSEGV。定位为时间线导出的成功
manifest daemon 线程跨测试泄漏：全量 SHA-256/媒体 probe 可运行数秒，
期间与后续测试的编码并发（污染进程级 native 状态），并在 pytest
卸载阶段与 GC 竞争崩溃。产品侧已提供线程登记表（timeline_export.
wait_for_manifest_writers），此处每例前后排空，恢复测试隔离。
"""

import pytest


@pytest.fixture(autouse=True)
def _drain_export_manifest_writers():
    """每例前后排空后台 manifest 写线程（超时让步，不判失败）。"""
    from src.features.edit.core.timeline_export import (
        wait_for_manifest_writers,
    )
    wait_for_manifest_writers(timeout=15.0)
    yield
    wait_for_manifest_writers(timeout=15.0)

"""生成媒体物化器测试。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from src.features.edit.ai_editing.generated_media_materializer import (
    AsyncGeneratedMediaMaterializer,
    GeneratedMediaMaterializer,
    MaterializeResult,
)
from src.features.edit.ai_editing.generation_models import (
    GeneratedAsset, GenerationModality, GenerationProvenance,
)
from src.features.edit.ai_editing.jobs import JobRegistry, JobStatus


def _asset(handle: str) -> GeneratedAsset:
    return GeneratedAsset(
        asset_id="gen:test/one", request_id="req-1",
        modality=GenerationModality.VIDEO,
        provenance=GenerationProvenance(provider_id="test", model_id="m1"),
        preview_handle=handle,
    )


class _Response:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self._headers = headers or {}

    @property
    def headers(self):
        return self._headers

    def read(self, size: int = -1) -> bytes:
        if not self._payload:
            return b""
        chunk, self._payload = self._payload[:size], self._payload[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_local_file_is_reused(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    result = GeneratedMediaMaterializer(cache_dir=tmp_path / "cache").materialize(
        _asset(str(source)),
    )
    assert result.succeeded
    assert result.path == str(source)


def test_stub_handle_is_rejected(tmp_path: Path):
    result = GeneratedMediaMaterializer(cache_dir=tmp_path).materialize(
        _asset("stub://preview/gen-1"),
    )
    assert not result.succeeded
    assert "虚拟句柄" in result.message


def test_http_handle_is_downloaded_and_cached(tmp_path: Path):
    payload = b"downloaded-media"

    def opener(_request, *, timeout):
        assert timeout == 3.0
        return _Response(payload, {"Content-Length": str(len(payload))})

    materializer = GeneratedMediaMaterializer(
        cache_dir=tmp_path / "cache", opener=opener, timeout=3.0,
    )
    result = materializer.materialize(_asset("https://provider.example/out/video.mp4"))
    assert result.succeeded
    assert Path(result.path).read_bytes() == payload
    assert "gen_test_one" in Path(result.path).name

    cached = materializer.materialize(_asset("https://provider.example/out/video.mp4"))
    assert cached.path == result.path


def test_http_size_limit_leaves_no_partial_file(tmp_path: Path):
    cache = tmp_path / "cache"

    def opener(_request, **_kwargs):
        return _Response(b"0123456789", {"Content-Length": "10"})

    result = GeneratedMediaMaterializer(
        cache_dir=cache, max_bytes=4, opener=opener,
    ).materialize(_asset("https://provider.example/out/video.mp4"))
    assert not result.succeeded
    assert "大小上限" in result.message
    assert list(cache.glob("*")) == []


def test_unsupported_scheme_is_rejected(tmp_path: Path):
    result = GeneratedMediaMaterializer(cache_dir=tmp_path).materialize(
        _asset("data:video/mp4;base64,AAAA"),
    )
    assert not result.succeeded
    assert "不支持" in result.message


class _FakeAsyncMaterializer:
    def materialize_with_progress(self, asset, *, progress, is_cancelled):
        progress(0.4, "测试下载中")
        return MaterializeResult("/tmp/generated-test.mp4", "测试下载完成")


def _wait_for(event: threading.Event) -> None:
    assert event.wait(2.0), "后台生成媒体物化任务未在预期时间内完成"


def test_async_materializer_reports_job_and_completion():
    jobs = JobRegistry()
    completed = []
    done = threading.Event()
    worker = AsyncGeneratedMediaMaterializer(_FakeAsyncMaterializer(), jobs)

    job = worker.start(
        _asset("https://provider.example/out/video.mp4"),
        on_completed=lambda handle, result: (completed.append((handle, result)), done.set()),
    )
    _wait_for(done)

    assert job.status is JobStatus.SUCCEEDED
    assert job.progress == 1.0
    assert completed[0][1].path == "/tmp/generated-test.mp4"
    assert "测试下载中" in job.logs


class _CancellableAsyncMaterializer:
    def __init__(self) -> None:
        self.started = threading.Event()

    def materialize_with_progress(self, asset, *, progress, is_cancelled):
        self.started.set()
        while not is_cancelled():
            time.sleep(0.005)
        return MaterializeResult(None, "生成媒体下载已取消", cancelled=True)


def test_async_materializer_honors_job_cancellation():
    jobs = JobRegistry()
    materializer = _CancellableAsyncMaterializer()
    completed = []
    done = threading.Event()
    worker = AsyncGeneratedMediaMaterializer(materializer, jobs)

    job = worker.start(
        _asset("https://provider.example/out/video.mp4"),
        on_completed=lambda handle, result: (completed.append((handle, result)), done.set()),
    )
    assert materializer.started.wait(2.0)
    assert jobs.cancel(job.job_id)
    _wait_for(done)

    assert job.status is JobStatus.CANCELLED
    assert completed[0][1].cancelled

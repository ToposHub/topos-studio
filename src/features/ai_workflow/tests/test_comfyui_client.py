"""RealComfyClient + ComfyProfileRegistry 测试（批次 3）。

验收：

- HTTP 映射：submit → ``POST /prompt``；状态 → ``/history/{id}`` + ``/queue``；
  取消 → ``POST /interrupt``；产物 → ``/history`` outputs + ``/view`` 下载
  （fake opener 注入，不依赖真实 server）。
- executor 消费面：``submit/get_job/cancel/job_outputs`` 与 FakeComfyServer
  协议一致（签名 + 行为）。
- 下载 sha256 边下边算；cloud 凭据经 CredentialStore 组 Bearer 头。
- ComfyProfileRegistry 配置 roundtrip。
"""
from __future__ import annotations

import io
import json
from typing import Any

import pytest

from src.features.ai_workflow.adapters.comfyui_client import RealComfyClient
from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyConnectionProfile,
    ComfyJobState,
)
from src.features.ai_workflow.adapters.comfyui_profiles import ComfyProfileRegistry


def _local_profile() -> ComfyConnectionProfile:
    return ComfyConnectionProfile(
        profile_id="local-default", kind="local",
        base_url="http://127.0.0.1:8188",
    )


class _FakeResponse:
    def __init__(self, body: bytes | str) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._stream = io.BytesIO(body)

    def read(self, limit: int = -1) -> bytes:
        return self._stream.read(limit if limit > 0 else -1)

    def __enter__(self):
        return self

    def __exit__(self, *args: Any) -> None:
        self._stream.close()


class _ScriptedOpener:
    """按 (method, path) 返回预设响应的 fake opener。

    ``strict=True`` 时对未注册路由抛 KeyError（fail-loud）——默认行为（静默
    返回空 JSON）会掩盖路由拼写/回归（AUD-23501）。
    """

    def __init__(
        self, routes: dict[tuple[str, str], Any], *, strict: bool = False,
    ) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.strict = strict

    def __call__(self, request: Any, timeout: float = 0.0) -> _FakeResponse:
        from urllib.parse import urlsplit
        split = urlsplit(request.full_url)
        key = (request.get_method(), split.path)
        self.calls.append(key)
        if self.strict and key not in self.routes:
            raise KeyError(f"unexpected request (no scripted route): {key}")
        body = self.routes.get(key)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, _FakeResponse):
            return body
        if callable(body):
            body = body(request)
        if isinstance(body, _FakeResponse):
            return body
        if isinstance(body, str):
            body = body.encode("utf-8")
        return _FakeResponse(body or b"{}")


def _client(
    routes: dict[tuple[str, str], Any],
    *,
    profile: ComfyConnectionProfile | None = None,
    credential_store: Any = None,
    download_dir: Any = None,
    strict: bool = False,
) -> tuple[RealComfyClient, _ScriptedOpener]:
    opener = _ScriptedOpener(routes, strict=strict)
    client = RealComfyClient(
        profile or _local_profile(), credential_store,
        opener=opener, sleep=lambda _s: None,
        download_dir=download_dir,
    )
    return client, opener


class TestSubmit:
    def test_submit_posts_prompt_and_returns_queued_ref(self) -> None:
        client, opener = _client({
            ("POST", "/prompt"): json.dumps({"prompt_id": "abc"}),
        })
        ref = client.submit(
            payload={"1": {"class_type": "CheckpointLoaderSimple"}},
            workflow_snapshot_hash="sha256:wf",
        )
        assert ref.external_job_id == "abc"
        assert ref.state == ComfyJobState.QUEUED
        assert ref.workflow_snapshot_hash == "sha256:wf"
        assert ("POST", "/prompt") in opener.calls

    def test_submit_failure_raises(self) -> None:
        import urllib.error
        client, _ = _client({
            ("POST", "/prompt"): urllib.error.HTTPError(
                "u", 400, "bad workflow", {}, io.BytesIO(b"node_errors"),  # type: ignore[arg-type]
            ),
        })
        with pytest.raises(RuntimeError):
            client.submit(payload={}, workflow_snapshot_hash="h")


class TestStateMapping:
    def test_history_success_maps_to_success(self) -> None:
        client, _ = _client({
            ("GET", "/history/abc"): json.dumps({
                "abc": {"status": {"status_str": "success"},
                        "outputs": {"9": {"images": [
                            {"filename": "out.png", "subfolder": "", "type": "output"},
                        ]}}},
            }),
        })
        # 手工注入 job 记录（避免 submit 路由；私有耦合集中在 _inject_job）
        _inject_job(client, "abc")
        ref = client.get_job("abc")
        assert ref.state == ComfyJobState.SUCCESS
        assert ref.progress_fraction == 1.0

    def test_queue_pending_maps_to_queued(self) -> None:
        """/queue queue_pending 含 job → QUEUED（AUD-23501：真实状态映射）。

        注入态必须是**非 QUEUED**（RUNNING）且断言确实请求了 ``/queue``——
        旧行为注入态即期望态（QUEUED），即使 client 完全不请求 /queue 断言
        也通过（状态保持注入值），映射回归不可检测。strict opener 使未注册
        路由 fail-loud，杜绝静默空 JSON 掩盖路由拼写错误。"""
        client, opener = _client({
            ("GET", "/history/abc"): {},
            ("GET", "/queue"): json.dumps({
                "queue_running": [], "queue_pending": [["abc"]],
            }),
        }, strict=True)
        # 注入 RUNNING（非 QUEUED）：只有 /queue 命中 pending 才会回到 QUEUED
        _inject_job(client, "abc", state=ComfyJobState.RUNNING)
        assert client.get_job("abc").state == ComfyJobState.QUEUED
        assert ("GET", "/queue") in opener.calls, "必须轮询 /queue 才能得出 QUEUED"

    def test_queue_running_maps_to_running(self) -> None:
        """/queue queue_running 含 job → RUNNING（真实状态映射，strict 路由）。"""
        client, opener = _client({
            ("GET", "/history/abc"): {},
            ("GET", "/queue"): json.dumps({
                "queue_running": [["abc"]], "queue_pending": [],
            }),
        }, strict=True)
        _inject_job(client, "abc")
        assert client.get_job("abc").state == ComfyJobState.RUNNING
        assert ("GET", "/queue") in opener.calls

    def test_history_error_maps_to_failed(self) -> None:
        client, _ = _client({
            ("GET", "/history/abc"): json.dumps({
                "abc": {"status": {"status_str": "error"}, "outputs": {}},
            }),
        })
        _inject_job(client, "abc")
        assert client.get_job("abc").state == ComfyJobState.FAILED

    def test_transient_poll_error_returns_cached_state(self) -> None:
        import urllib.error
        calls = {"n": 0}

        def flaky(request: Any, timeout: float = 0.0) -> _FakeResponse:
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("boom")
            return _FakeResponse(json.dumps({
                "abc": {"status": {"status_str": "success"}, "outputs": {}},
            }))

        client = RealComfyClient(
            _local_profile(), None, opener=flaky, sleep=lambda _s: None,
        )
        _inject_job(client, "abc")
        first = client.get_job("abc")  # 不抛；返回缓存状态
        assert first.state in (ComfyJobState.QUEUED, ComfyJobState.SUCCESS)
        second = client.get_job("abc")
        assert second.state == ComfyJobState.SUCCESS


class TestCancel:
    def test_cancel_interrupts_running_job(self) -> None:
        # AUD-23004：RUNNING 任务 → POST /interrupt（打断当前执行）
        client, opener = _client({("POST", "/interrupt"): {}})
        _inject_job(client, "abc", state=ComfyJobState.RUNNING)
        ref = client.cancel("abc")
        assert ref.state == ComfyJobState.CANCELLED
        assert ("POST", "/interrupt") in opener.calls

    def test_cancel_dequeues_queued_job(self) -> None:
        # AUD-23004：/interrupt 不会把 QUEUED 任务移出队列；QUEUED 任务必须
        # DELETE /queue（按 prompt_id 出队），否则 server 仍会执行该 workflow。
        client, opener = _client({("DELETE", "/queue"): {}})
        _inject_job(client, "abc")  # state=QUEUED
        ref = client.cancel("abc")
        assert ref.state == ComfyJobState.CANCELLED
        assert ("DELETE", "/queue") in opener.calls
        assert ("POST", "/interrupt") not in opener.calls

    def test_cancel_queued_falls_back_to_interrupt_on_delete_failure(self) -> None:
        # 出队失败（HTTP 400 等）→ 退回 POST /interrupt 尽力而为
        import io
        import urllib.error
        client, opener = _client({
            ("DELETE", "/queue"): urllib.error.HTTPError(
                "u", 400, "bad request", {}, io.BytesIO(b""),  # type: ignore[arg-type]
            ),
            ("POST", "/interrupt"): {},
        })
        _inject_job(client, "abc")  # state=QUEUED
        ref = client.cancel("abc")
        assert ref.state == ComfyJobState.CANCELLED
        assert ("DELETE", "/queue") in opener.calls
        assert ("POST", "/interrupt") in opener.calls

    def test_cancel_terminal_job_noop(self) -> None:
        client, _ = _client({})
        _inject_job(client, "abc", state=ComfyJobState.SUCCESS)
        assert client.cancel("abc").state == ComfyJobState.SUCCESS


class TestOutputs:
    def test_job_outputs_downloads_and_hashes(self, tmp_path) -> None:
        payload = b"png-bytes-here"
        client, opener = _client({
            ("GET", "/history/abc"): json.dumps({
                "abc": {"status": {"status_str": "success"},
                        "outputs": {"9": {"images": [
                            {"filename": "out.png", "subfolder": "", "type": "output"},
                        ]}}},
            }),
            ("GET", "/view"): payload,
        }, download_dir=tmp_path)
        _inject_job(client, "abc")
        outputs, hashes, cost = client.job_outputs("abc")
        assert len(outputs) == 1
        out = outputs[0]
        assert out.filename == "out.png"
        assert out.content_hash.startswith("sha256:")
        import hashlib
        assert out.content_hash == "sha256:" + hashlib.sha256(payload).hexdigest()
        assert hashes[out.filename] == out.content_hash
        assert cost == 0.0
        assert (tmp_path / (out.local_path.split("/")[-1])).exists()

    def test_output_url_is_http_view(self) -> None:
        client, _ = _client({})
        from src.features.ai_workflow.adapters.comfyui_models import (
            ComfyOutputArtifact,
        )
        out = ComfyOutputArtifact(
            node_id="9", output_index=0, artifact_kind="image",
            filename="out.png", content_hash="sha256:x", bytes_size=1,
        )
        url = client.output_url(None, out)
        assert url.startswith("http://127.0.0.1:8188/view?")
        assert "filename=out.png" in url


class TestAuth:
    def test_cloud_bearer_header(self) -> None:
        captured: dict[str, Any] = {}

        def capture(request: Any, timeout: float = 0.0) -> _FakeResponse:
            captured["auth"] = request.get_header("Authorization")
            return _FakeResponse(json.dumps({"prompt_id": "abc"}))

        profile = ComfyConnectionProfile(
            profile_id="cloud-1", kind="cloud",
            base_url="https://comfy.example.com",
            credential_ref="topos:comfy-cloud-1",
        )
        store = type("S", (), {"get_secret": lambda self, ref: "sk-test"})()
        client = RealComfyClient(profile, store, opener=capture)
        client.submit(payload={}, workflow_snapshot_hash="h")
        assert captured["auth"] == "Bearer sk-test"


class TestTestConnection:
    def test_system_stats(self) -> None:
        client, _ = _client({
            ("GET", "/system_stats"): json.dumps({
                "system": {"comfyui_version": "0.3.30"},
            }),
        })
        ok, message = client.test_connection()
        assert ok
        assert "0.3.30" in message


class TestProtocolConsistency:
    def test_real_implements_fake_surface(self) -> None:
        """executor 消费面协议一致：签名 + 行为（AUD-23505）。

        仅 ``callable(getattr(...))`` 锁不住参数漂移（如 ``submit`` 丢掉
        keyword-only 的 ``workflow_snapshot_hash``）；这里用
        ``inspect.signature`` 逐参数对照 name/kind/default，并对照
        ``job_outputs`` 的返回形状（三元组）。
        """
        import inspect

        from src.features.ai_workflow.adapters.comfyui_executor import (
            FakeComfyServer,
        )
        fake = FakeComfyServer(profile_id="p")
        client = RealComfyClient(_local_profile(), None, sleep=lambda _s: None)

        def _shape(fn: Any) -> list[tuple[str, Any, Any]]:
            return [
                (p.name, p.kind, p.default)
                for p in inspect.signature(fn).parameters.values()
            ]

        for method in ("submit", "get_job", "cancel", "job_outputs"):
            fake_method = getattr(fake, method, None)
            real_method = getattr(client, method, None)
            assert callable(fake_method), method
            assert callable(real_method), method
            fake_shape = _shape(fake_method)
            real_shape = _shape(real_method)
            assert fake_shape == real_shape, (
                f"{method} 签名漂移：fake={fake_shape} real={real_shape}"
            )
        # 行为形状：两侧 job_outputs 都返回 (outputs, hashes, cost) 三元组
        # （Real 侧三元组解包由 TestOutputs 覆盖，这里对照 Fake 侧）。
        ref = fake.submit(payload={}, workflow_snapshot_hash="h")
        out = fake.job_outputs(ref.external_job_id)
        assert isinstance(out, tuple) and len(out) == 3


class TestProfileRegistry:
    def test_config_roundtrip(self, tmp_path) -> None:
        registry = ComfyProfileRegistry()
        registry.add(_local_profile())
        registry.set_active("local-default")

        config = _FakeConfig()
        registry.save_to_config(config)

        restored = ComfyProfileRegistry()
        restored.load_from_config(config)
        assert restored.get("local-default") is not None
        assert restored.active_profile_id == "local-default"
        assert restored.active_profile.base_url == "http://127.0.0.1:8188"

    def test_bad_entries_skipped(self) -> None:
        config = _FakeConfig()
        config.values[ComfyProfileRegistry.CONFIG_KEY_PROFILES] = [
            "not-a-dict", {"profile_id": "x"},  # 缺必填字段的半成品
        ]
        registry = ComfyProfileRegistry()
        registry.load_from_config(config)
        assert registry.list() == []


# ---------------- 辅助 ---------------- #


def _inject_job(
    client: RealComfyClient,
    job_id: str,
    *,
    state: ComfyJobState = ComfyJobState.QUEUED,
) -> Any:
    """把手工构造的 job 记录注入 ``client._jobs``（AUD-23503：集中私有耦合）。

    ``_jobs`` 字典与 ``_JobRecord`` 构造签名都是被测模块的**私有**实现细节，
    全文件仅此一处直接触碰：实现重构 record 字段（如新增 progress 默认值）时
    只需改这里。注意：手工注入的记录可能偏离真实 ``submit`` 产出的记录——
    新增状态类测试优先走 ``client.submit`` 路由（见 TestSubmit）。
    """
    from src.features.ai_workflow.adapters.comfyui_client import _JobRecord

    record = _JobRecord(
        external_job_id=job_id,
        profile_id=client.profile_id,
        workflow_snapshot_hash="sha256:wf",
        state=state,
        submit_time_iso="2026-08-22T00:00:00Z",
    )
    client._jobs[job_id] = record
    return record


class _FakeConfig:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self.values[key] = value

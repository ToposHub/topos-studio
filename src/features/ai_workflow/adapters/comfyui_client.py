"""RealComfyClient —— 真实 ComfyUI server 的 HTTP 客户端（批次 3 / plan C 系列）。

> 实现 :class:`FakeComfyServer` 被 :class:`ComfyWorkflowExecutor` 消费的同一
> server 协议（``submit`` / ``get_job`` / ``cancel`` / ``job_outputs`` / 可选
> ``output_url``），纯标准库 ``urllib.request`` 轮询——不引入 websocket/三方
> HTTP 依赖（与 llm_client / http_generation_provider 一致）。

ComfyUI 官方 HTTP API 映射：

- 提交：``POST /prompt``（body ``{"prompt": <api-format>, "client_id": ...}``）。
- 状态：``GET /history/{prompt_id}``（``status.status_str``：
  success/error/cancelled + outputs）+ ``GET /queue``（queued/running 判定）。
- 取消：QUEUED 任务 ``DELETE /queue``（按 prompt_id 出队）；RUNNING 任务
  ``POST /interrupt``（打断当前执行）；出队失败退回 interrupt 尽力而为。
- 产物：``GET /history`` outputs → 逐个 ``GET /view?filename=&subfolder=&type=``
  流式下载到缓存目录，边下边算 sha256（真实 server 不报 hash——下载时计算
  一次并记录，缓存复用时复验；HASH_MISMATCH 语义降级为下载自校验）。
- 能力：``GET /object_info`` → ``ComfyServerCapabilities``（class_type 集合）。
- 连接测试：``GET /system_stats``。

可测性（项目约定）：构造参数注入 ``opener``（默认 ``urllib.request.urlopen``）、
``sleep``、``monotonic``；测试用 fake opener 返回预设响应。

线程模型：全同步；生产经 ``GenerationService.request_generation_async`` →
JobWorkerPool 在 worker 线程执行，不在 client 内起线程。

错误分类：内部 ``_ComfyHttpError``（照抄 http_generation_provider 的
``_HttpAdapterError`` 模式）；HTTP 异常在 executor 边界统一映射到既有
``ComfyExecutorErrorCode`` 冻结集合（不扩码表）。

红线：cloud 凭据只经 ``CredentialStore``（``credential_ref``），绝不内联；
本地 localhost 免凭据；下载文件落在项目缓存目录（与 generated_media 同源）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyConnectionProfile,
    ComfyJobRef,
    ComfyJobState,
    ComfyOutputArtifact,
)

_LOG = logging.getLogger(__name__)

#: JSON 响应体上限（/object_info 可能较大；2MB 与 http_generation_provider 一致）。
_MAX_JSON_BYTES = 2 * 1024 * 1024
#: 单个产物下载上限（64MB）。
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
#: 下载块大小。
_DOWNLOAD_CHUNK = 1024 * 1024
#: 重试退避上限（与 http_generation_provider 一致）。
_MAX_BACKOFF_S = 8.0
_RETRY_STATUSES = (408, 429, 500, 502, 503, 504)


class _ComfyHttpError(Exception):
    """HTTP 层错误（executor 边界再映射到 ComfyExecutorErrorCode）。"""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


class _JobRecord:
    """client 侧 job 记录（对齐 FakeComfyServer._FakeJob 的消费面）。"""

    def __init__(
        self,
        external_job_id: str,
        profile_id: str,
        workflow_snapshot_hash: str,
        state: str,
        submit_time_iso: str,
    ) -> None:
        self.external_job_id = external_job_id
        self.profile_id = profile_id
        self.workflow_snapshot_hash = workflow_snapshot_hash
        self.state = state
        self.progress_fraction = 0.0
        self.progress_message = ""
        self.submit_time_iso = submit_time_iso
        self.outputs: List[ComfyOutputArtifact] = []
        self.actual_output_hashes: Dict[str, str] = {}
        self.actual_cost_units = 0.0
        self.outputs_fetched = False


class RealComfyClient:
    """真实 ComfyUI server 客户端（FakeComfyServer 的生产替换）。

    用法（宿主装配）::

        client = RealComfyClient(profile, credential_store)
        caps = client.capabilities()          # GET /object_info
        executor = ComfyWorkflowExecutor(
            server=client, server_capabilities=caps,
        )
    """

    def __init__(
        self,
        profile: ComfyConnectionProfile,
        credential_store: Any = None,
        *,
        opener: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        request_timeout_s: float = 30.0,
        max_retries: int = 2,
        download_dir: str | os.PathLike | None = None,
    ) -> None:
        self._profile = profile
        self._credential_store = credential_store
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._timeout = float(request_timeout_s)
        self._max_retries = int(max_retries)
        self._client_id = uuid.uuid4().hex
        self._jobs: Dict[str, _JobRecord] = {}
        if download_dir is not None:
            self._download_dir = Path(download_dir)
        else:
            from src.features.edit.ai_editing.model_pack_manager import (
                default_cache_dir,
            )
            self._download_dir = default_cache_dir() / "comfyui"

    # ---------------- 基本属性 ---------------- #

    @property
    def profile_id(self) -> str:
        return self._profile.profile_id

    # ---------------- server 协议（executor 消费面） ---------------- #

    def submit(
        self,
        *,
        payload: Mapping[str, Any],
        workflow_snapshot_hash: str,
    ) -> ComfyJobRef:
        prompt_id = uuid.uuid4().hex
        body = {
            "prompt": dict(payload),
            "client_id": self._client_id,
            "prompt_id": prompt_id,
        }
        try:
            resp = self._request_json(
                "POST", "/prompt", body=body,
            )
        except _ComfyHttpError as e:
            raise RuntimeError(f"/prompt failed: {e.message}") from e
        returned_id = str((resp or {}).get("prompt_id") or prompt_id)
        record = _JobRecord(
            external_job_id=returned_id,
            profile_id=self.profile_id,
            workflow_snapshot_hash=workflow_snapshot_hash,
            state=ComfyJobState.QUEUED,
            submit_time_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._jobs[returned_id] = record
        return self._ref(record)

    def get_job(self, external_job_id: str) -> ComfyJobRef:
        record = self._jobs.get(external_job_id)
        if record is None:
            raise KeyError(external_job_id)
        try:
            self._refresh_state(record)
        except _ComfyHttpError:
            # 轮询韧性：瞬时网络错误返回最后已知状态，不让 poll 循环崩。
            _LOG.debug("comfy poll failed for %s; using cached state",
                       external_job_id)
        return self._ref(record)

    def cancel(self, external_job_id: str) -> ComfyJobRef:
        record = self._jobs.get(external_job_id)
        if record is None:
            raise KeyError(external_job_id)
        if record.state in ComfyJobState.TERMINAL:
            return self._ref(record)
        if record.state == ComfyJobState.QUEUED:
            # AUD-23004：ComfyUI 的 ``POST /interrupt`` 只打断**正在执行**的
            # 任务，不会把排队中的任务移出队列。QUEUED 任务必须走
            # ``DELETE /queue``（按 prompt_id 出队），否则 server 仍会执行该
            # workflow（本地浪费 GPU / 云 profile 浪费费用）。出队失败再退回
            # /interrupt 尽力而为。
            try:
                self._request_json(
                    "DELETE", "/queue",
                    body={"delete": [record.external_job_id]},
                )
            except _ComfyHttpError as e:
                _LOG.warning(
                    "comfy queue delete failed for %s: %s",
                    record.external_job_id, e.message,
                )
            else:
                record.state = ComfyJobState.CANCELLED
                record.progress_message = "dequeued (cancel requested)"
                return self._ref(record)
        try:
            self._request_json("POST", "/interrupt")
        except _ComfyHttpError as e:
            _LOG.warning("comfy interrupt failed: %s", e.message)
        else:
            record.state = ComfyJobState.CANCELLED
            record.progress_message = "interrupt requested"
        return self._ref(record)

    def job_outputs(
        self, external_job_id: str,
    ) -> Tuple[List[ComfyOutputArtifact], Dict[str, str], float]:
        """下载产物并返回 (outputs, 实际 hash 表, 实际成本)。

        真实 ComfyUI 不报 content hash / cost：下载时计算 sha256 记录进
        ``actual_output_hashes``（executor 用它做下载完整性校验；未下载成功时
        表为空，executor 按 HASH_MISMATCH 拒绝——不回退到期望值）；cost 恒 0
        （本地算力不计费）。下载失败抛 :class:`_ComfyHttpError` 且**不**置
        ``outputs_fetched``，下一次调用重试。
        """
        record = self._jobs.get(external_job_id)
        if record is None:
            raise KeyError(external_job_id)
        if not record.outputs_fetched:
            # AUD-23002：下载失败（可重试的 NETWORK_ERROR 等）不得置
            # ``outputs_fetched=True``——否则瞬时网络错误被固化为"无产物"或携带
            # 占位 hash 的候选。失败时抛出（半成品文件已清理），由 executor
            # 边界映射为 SERVER_FAILED；下一轮 fetch 可重试。
            try:
                self._refresh_state(record)
                self._download_outputs(record)
            except _ComfyHttpError:
                _LOG.warning(
                    "comfy outputs fetch failed for %s; will retry on next call",
                    external_job_id,
                )
                raise
            record.outputs_fetched = True
        return (
            list(record.outputs),
            dict(record.actual_output_hashes),
            float(record.actual_cost_units),
        )

    # ---------------- 能力 / 连接测试 ---------------- #

    def capabilities(self) -> Any:
        """``GET /object_info`` → :class:`ComfyServerCapabilities`。"""
        from src.features.ai_workflow.adapters.comfyui_importer import (
            ComfyServerCapabilities,
        )

        resp = self._request_json("GET", "/object_info") or {}
        class_types = frozenset(str(k) for k in resp.keys())
        models: set[str] = set()
        for info in resp.values():
            if not isinstance(info, dict):
                continue
            # 真实 /object_info 结构（AUD-23001）：每个节点的 "input" 是
            # {"required": {输入名: spec}, "optional": {...}}，spec 形如
            # [候选列表, 配置]（loader 类输入的候选列表即 server 模型文件名）
            # 或 [类型名, 配置]（如 ["INT", {...}]）。
            input_sections = info.get("input")
            if not isinstance(input_sections, dict):
                continue
            for section in input_sections.values():
                if not isinstance(section, dict):
                    continue
                for spec in section.values():
                    if (
                        isinstance(spec, list)
                        and spec
                        and isinstance(spec[0], list)
                    ):
                        for candidate in spec[0]:
                            if isinstance(candidate, str) and "." in candidate:
                                models.add(candidate)
        return ComfyServerCapabilities(
            profile_id=self.profile_id,
            available_class_types=class_types,
            available_models=frozenset(models),
        )

    def _empty_capabilities(self) -> Any:
        """无网络的空能力集（装配期占位；bind 时经 ``capabilities()`` 刷新）。"""
        from src.features.ai_workflow.adapters.comfyui_importer import (
            ComfyServerCapabilities,
        )

        return ComfyServerCapabilities(
            profile_id=self.profile_id,
            available_class_types=frozenset(),
            available_models=frozenset(),
        )

    def test_connection(self) -> Tuple[bool, str]:
        """``GET /system_stats`` 探活（设置页「测试连接」）。"""
        try:
            resp = self._request_json("GET", "/system_stats") or {}
        except _ComfyHttpError as e:
            return False, e.message
        version = (resp.get("system") or {}).get("comfyui_version", "unknown")
        return True, f"ComfyUI {version} @ {self._profile.base_url}"

    def output_url(self, req: Any, out: ComfyOutputArtifact) -> str:
        """Real 路径的候选预览句柄（http /view URL，物化器可直接下载）。"""
        params = urllib.parse.urlencode({
            "filename": out.filename,
            "subfolder": getattr(out, "subfolder", "") or "",
            "type": "output",
        })
        return f"{self._profile.base_url.rstrip('/')}/view?{params}"

    # ---------------- HTTP 核心 ---------------- #

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json"}
        if self._profile.is_cloud and self._credential_store is not None:
            secret = None
            try:
                secret = self._credential_store.get_secret(
                    self._profile.credential_ref,
                )
            except Exception:  # noqa: BLE001 — 凭据读取失败按缺失处理
                secret = None
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
    ) -> Any:
        url = f"{self._profile.base_url.rstrip('/')}{path}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error: Optional[_ComfyHttpError] = None
        for attempt in range(self._max_retries + 1):
            request = urllib.request.Request(
                url, data=data, method=method,
                headers=self._headers(),
            )
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    raw = response.read(_MAX_JSON_BYTES + 1)
                if len(raw) > _MAX_JSON_BYTES:
                    raise _ComfyHttpError(
                        "RESPONSE_TOO_LARGE",
                        f"{path} response exceeds {_MAX_JSON_BYTES} bytes",
                    )
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read(2048).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    pass
                code = "HTTP_ERROR"
                if e.code in (401, 403):
                    code = "AUTH_FAILED"
                elif e.code == 404:
                    code = "ENDPOINT_NOT_FOUND"
                last_error = _ComfyHttpError(
                    code, f"{path} -> HTTP {e.code}: {detail[:200]}",
                    retryable=e.code in _RETRY_STATUSES,
                )
                if not last_error.retryable:
                    raise last_error
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = _ComfyHttpError(
                    "NETWORK_ERROR", f"{path} -> {e}", retryable=True,
                )
            if attempt < self._max_retries and last_error is not None:
                self._sleep(min(_MAX_BACKOFF_S, 0.5 * (2 ** attempt)))
        assert last_error is not None
        raise last_error

    # ---------------- 状态映射 ---------------- #

    def _refresh_state(self, record: _JobRecord) -> None:
        history = self._request_json(
            "GET", f"/history/{urllib.parse.quote(record.external_job_id)}",
        ) or {}
        entry = history.get(record.external_job_id)
        if isinstance(entry, dict):
            status = entry.get("status") or {}
            status_str = str(status.get("status_str", "")).lower()
            if status_str == "success":
                record.state = ComfyJobState.SUCCESS
                record.progress_fraction = 1.0
                record.progress_message = "done"
                self._parse_history_outputs(record, entry)
                return
            if status_str == "error":
                record.state = ComfyJobState.FAILED
                record.progress_message = "execution error"
                return
            if status_str in ("cancelled", "canceled"):
                record.state = ComfyJobState.CANCELLED
                record.progress_message = "cancelled"
                return
            # history 有记录但无终态 → running
            record.state = ComfyJobState.RUNNING
            record.progress_fraction = max(record.progress_fraction, 0.5)
            record.progress_message = "executing workflow"
            return
        # 不在 history → 查 queue
        queue = self._request_json("GET", "/queue") or {}
        running = json.dumps(queue.get("queue_running") or [], default=str)
        pending = json.dumps(queue.get("queue_pending") or [], default=str)
        job_marker = record.external_job_id
        if job_marker in running:
            record.state = ComfyJobState.RUNNING
            record.progress_fraction = max(record.progress_fraction, 0.1)
            record.progress_message = "executing workflow"
        elif job_marker in pending:
            record.state = ComfyJobState.QUEUED
            record.progress_message = "queued"
        else:
            # 两处都无（极小窗口）→ 保持当前状态，下轮轮询再定。
            pass

    def _parse_history_outputs(self, record: _JobRecord, entry: dict) -> None:
        outputs = entry.get("outputs") or {}
        artifacts: List[ComfyOutputArtifact] = []
        subfolders: Dict[str, str] = {}
        for node_id, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for kind_key, artifact_kind in (
                ("images", "image"),
                ("gifs", "video"),
                ("videos", "video"),
                ("audio", "audio"),
            ):
                for idx, item in enumerate(node_out.get(kind_key) or []):
                    if not isinstance(item, dict):
                        continue
                    filename = str(item.get("filename") or "")
                    if not filename:
                        continue
                    subfolder = str(item.get("subfolder") or "")
                    if subfolder:
                        subfolders[filename] = subfolder
                    artifacts.append(ComfyOutputArtifact(
                        node_id=str(node_id),
                        output_index=idx,
                        artifact_kind=artifact_kind,
                        filename=filename,
                        content_hash="sha256:pending-download",  # 下载时覆算
                        bytes_size=0,
                    ))
        record.outputs = artifacts
        record._subfolders = subfolders  # noqa: SLF001 — 自身记录

    def _download_outputs(self, record: _JobRecord) -> None:
        if record.state != ComfyJobState.SUCCESS or not record.outputs:
            return
        self._download_dir.mkdir(parents=True, exist_ok=True)
        download_root = self._download_dir.resolve()
        for out in record.outputs:
            subfolder = getattr(record, "_subfolders", {}).get(out.filename, "")
            params = urllib.parse.urlencode({
                "filename": out.filename,
                "subfolder": subfolder,
                "type": "output",
            })
            url = f"{self._profile.base_url.rstrip('/')}/view?{params}"
            request = urllib.request.Request(
                url, method="GET", headers=self._headers(),
            )
            # AUD-23003：filename 来自外部 server 的 /history 响应，直接拼接
            # 可被 "../../../.zshrc" 之类穿越出缓存目录。只保留纯文件名，并断言
            # 落盘路径仍在下载目录内（铁律：外部数据须验证）。
            safe_filename = Path(out.filename).name
            if not safe_filename or safe_filename in (".", ".."):
                raise _ComfyHttpError(
                    "INVALID_FILENAME",
                    f"server returned unsafe output filename {out.filename!r}",
                    retryable=False,
                )
            target = self._download_dir / (
                f"{record.external_job_id[:12]}_{safe_filename}"
            )
            if download_root not in target.resolve().parents:
                raise _ComfyHttpError(
                    "UNSAFE_PATH",
                    f"resolved download path escapes cache dir: {target}",
                    retryable=False,
                )
            digest = hashlib.sha256()
            size = 0
            try:
                with self._opener(request, timeout=self._timeout) as response, \
                        open(target, "wb") as fh:
                    while True:
                        chunk = response.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > _MAX_DOWNLOAD_BYTES:
                            raise _ComfyHttpError(
                                "RESPONSE_TOO_LARGE",
                                f"output {out.filename} exceeds download cap",
                            )
                        digest.update(chunk)
                        fh.write(chunk)
            except urllib.error.HTTPError as e:
                target.unlink(missing_ok=True)  # AUD-23002：不留半成品
                raise _ComfyHttpError(
                    "HTTP_ERROR", f"/view -> HTTP {e.code}", retryable=False,
                ) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                target.unlink(missing_ok=True)  # AUD-23002：不留半成品
                raise _ComfyHttpError(
                    "NETWORK_ERROR", f"/view -> {e}", retryable=True,
                ) from e
            except _ComfyHttpError:
                target.unlink(missing_ok=True)  # 超限中断同样清理
                raise
            object.__setattr__(out, "content_hash", "sha256:" + digest.hexdigest())
            object.__setattr__(out, "bytes_size", size)
            object.__setattr__(out, "local_path", str(target))
            record.actual_output_hashes[out.filename] = "sha256:" + digest.hexdigest()

    def _ref(self, record: _JobRecord) -> ComfyJobRef:
        return ComfyJobRef(
            external_job_id=record.external_job_id,
            profile_id=record.profile_id,
            workflow_snapshot_hash=record.workflow_snapshot_hash,
            state=record.state,
            progress_fraction=record.progress_fraction,
            progress_message=record.progress_message,
            submit_time_iso=record.submit_time_iso,
        )


__all__ = ["RealComfyClient"]

"""Opt-in VST3 stereo hosting. Native plugin code runs in a child process."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import logging
import math
import mmap
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore, Event
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

import numpy as np

logger = logging.getLogger(__name__)
EFFECT_TYPE = "vst3_hosted"
HOST_VERSION = 1
MAX_FRAMES = 8192
MAX_MESSAGE = 512 * 1024
START_TIMEOUT = 15.0
PCM_BYTES = 2 * MAX_FRAMES * np.dtype(np.float32).itemsize
_BUILDS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vst3-load")
_CLEANUP = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vst3-close")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_PROCESS_SLOTS = BoundedSemaphore(24)


class HostedPluginError(RuntimeError):
    """Plugin cannot render the requested signal under this host contract."""


class _WorkerFault(HostedPluginError):
    """Transport failure, distinct from a clean plugin rejection."""


class _Cancelled(HostedPluginError):
    """Explicit cancellation must never quarantine the plugin."""


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise _Cancelled("VST3 operation cancelled")


@dataclass(frozen=True)
class HostedVst3Params:
    fingerprint: str
    plugin_name: str
    parameters: tuple[tuple[str, float], ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HostedVst3Params:
        fingerprint = data.get("fingerprint", "")
        name = data.get("plugin_name", "")
        version = data.get("host_version")
        parameters = data.get("parameters", {})
        if (type(version) is not int or version != HOST_VERSION
                or not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint)
                or not isinstance(name, str) or not 0 < len(name) <= 256
                or not isinstance(parameters, dict) or len(parameters) > 512):
            raise ValueError("invalid VST3 effect identity or version")
        values = []
        for key, value in parameters.items():
            if (not isinstance(key, str) or not 0 < len(key) <= 128
                    or type(value) not in (float, int) or not 0 <= value <= 1):
                raise ValueError("invalid normalized VST3 parameter")
            values.append((key, float(value)))
        return cls(fingerprint, name, tuple(sorted(values)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_version": HOST_VERSION,
            "fingerprint": self.fingerprint,
            "plugin_name": self.plugin_name,
            "parameters": dict(self.parameters),
        }


def backend_issue() -> str:
    if sys.platform not in {"darwin", "linux"} or getattr(sys, "frozen", False):
        return "VST3 hosting needs the macOS or Linux Python build"
    if importlib.util.find_spec("pedalboard") is None:
        return "Install the vst3-host optional dependency to use VST3 effects"
    return ""


def _registry_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Topos" / "VST3"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "topos" / "vst3"


def _entry_path(fingerprint: str) -> Path:
    if not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("invalid VST3 fingerprint")
    return _registry_dir() / f"{fingerprint}.json"


def fingerprint_bundle(path: Path,
                       cancel_check: Callable[[], bool] | None = None) -> str:
    """Hash bundle contents and entry names. Reject linked bundle payloads."""
    if path.suffix.lower() != ".vst3" or not path.exists():
        raise HostedPluginError("Select an installed .vst3 plugin")
    items = [path] if path.is_file() else sorted(path.rglob("*"))
    digest = hashlib.sha256()
    total = 0
    if len(items) > 8192:
        raise HostedPluginError("VST3 bundle exceeds the host file count limit")
    for item in items:
        _check_cancel(cancel_check)
        if item.is_symlink():
            raise HostedPluginError("VST3 bundle contains a linked file")
        if item.is_dir():
            continue
        if not item.is_file():
            raise HostedPluginError("VST3 bundle contains an unsupported file")
        name = item.relative_to(path).as_posix().encode("utf-8")
        size = item.stat().st_size
        total += size
        if total > 2 * 1024 ** 3:
            raise HostedPluginError("VST3 bundle exceeds the host scan size limit")
        digest.update(len(name).to_bytes(4, "little"))
        digest.update(name)
        digest.update(size.to_bytes(8, "little"))
        with item.open("rb") as source:
            while block := source.read(1024 * 1024):
                _check_cancel(cancel_check)
                digest.update(block)
    if total == 0:
        raise HostedPluginError("VST3 plugin bundle is empty")
    return digest.hexdigest()


def _record(fingerprint: str, data: dict[str, Any]) -> None:
    path = _entry_path(fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False,
                                     encoding="utf-8") as target:
        pending = Path(target.name)
        try:
            json.dump(data, target)
            target.close()
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def _local_entry(fingerprint: str) -> dict[str, Any]:
    try:
        path = _entry_path(fingerprint)
        with path.open(encoding="utf-8") as source:
            entry = json.loads(source.read(MAX_MESSAGE + 1))
        if not isinstance(entry, dict):
            raise ValueError("invalid registration")
        return entry
    except (OSError, ValueError) as exc:
        raise HostedPluginError("Scan this exact VST3 build in the FX rack before using it") from exc


def _registered_path(params: HostedVst3Params) -> Path:
    entry = _local_entry(params.fingerprint)
    if entry.get("error"):
        raise HostedPluginError("VST3 is quarantined. Rescan it in the FX rack to retry")
    names = entry.get("names", ())
    path = entry.get("path")
    if not isinstance(names, list) or params.plugin_name not in names or not isinstance(path, str):
        raise HostedPluginError("VST3 component is absent from the local registration")
    result = Path(path)
    if not result.exists():
        raise HostedPluginError("Registered VST3 bundle is missing")
    return result


def params_issue(data: Mapping[str, Any]) -> str:
    try:
        params = HostedVst3Params.from_dict(data)
        _registered_path(params)
        return backend_issue()
    except (ValueError, HostedPluginError) as exc:
        return str(exc)


class HostTransport:
    """One worker, one fixed shared PCM region, one in-flight JSON request."""

    def __init__(self) -> None:
        if issue := backend_issue():
            raise HostedPluginError(issue)
        if not _PROCESS_SLOTS.acquire(blocking=False):
            raise HostedPluginError("VST3 worker limit reached. Bypass unused inserts to continue")
        self._pending = bytearray()
        self._process: subprocess.Popen[bytes] | None = None
        self._pcm_file = None
        self.memory = None
        self._control = None
        self.generation = uuid4().hex
        self.index = 0
        self._closed = False
        try:
            self._pcm_file = tempfile.TemporaryFile()
            self._pcm_file.truncate(PCM_BYTES)
            self.memory = mmap.mmap(self._pcm_file.fileno(), PCM_BYTES)
            self._control, worker = socket.socketpair()
            # A plug-in may bundle a different Qt build. The parent application's
            # SDK and platform-plugin paths are not usable inside that bundle.
            environment = dict(os.environ)
            for name in ("QT_QPA_PLATFORM", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
                environment.pop(name, None)
            with worker:
                self._process = subprocess.Popen(
                    [sys.executable, "-m", "src.features.edit.core.hosted_vst3_worker",
                     str(worker.fileno()), str(self._pcm_file.fileno())],
                    pass_fds=(worker.fileno(), self._pcm_file.fileno()),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                    cwd=Path(__file__).resolve().parents[4],
                    env=environment,
                )
        except BaseException:
            self.close()
            raise

    def request(self, command: str, timeout: float, *,
                cancel_check: Callable[[], bool] | None = None, **args: Any) -> dict[str, Any]:
        if self._closed or self._control is None:
            raise _WorkerFault("The VST3 worker has already been closed")
        index = self.index
        self.index += 1
        try:
            reply = exchange(self._control, self._pending, {
                "command": command, "generation": self.generation, "index": index, **args,
            }, timeout, cancel_check)
            _check_cancel(cancel_check)
            if reply.get("generation") != self.generation or reply.get("index") != index:
                raise _WorkerFault("Unexpected VST3 worker response identity")
            if "error" in reply:
                raise HostedPluginError(str(reply["error"]))
            return reply
        except TimeoutError as exc:
            raise _WorkerFault("VST3 worker missed its processing deadline") from exc
        except (OSError, ValueError) as exc:
            code = self._process.poll() if self._process is not None else None
            suffix = f" (exit code {code})" if code is not None else ""
            raise _WorkerFault(f"VST3 worker exited or broke its control connection{suffix}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            process = self._process
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
        finally:
            if self._control is not None:
                self._control.close()
            if self.memory is not None:
                self.memory.close()
            if self._pcm_file is not None:
                self._pcm_file.close()
            _PROCESS_SLOTS.release()


def _packet(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE:
        raise HostedPluginError("VST3 control message exceeds the host limit")
    return payload + b"\n"


def read_message(control: socket.socket, pending: bytearray,
                 deadline: float | None = None,
                 cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    while (end := pending.find(b"\n")) < 0:
        if len(pending) > MAX_MESSAGE:
            raise HostedPluginError("VST3 worker response exceeds the host limit")
        if deadline is not None:
            _check_cancel(cancel_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            control.settimeout(min(0.05, remaining) if cancel_check is not None else remaining)
        try:
            chunk = control.recv(8192)
        except TimeoutError:
            if cancel_check is None:
                raise
            continue
        if not chunk:
            raise ConnectionError("VST3 worker closed its transport")
        pending.extend(chunk)
    if end > MAX_MESSAGE:
        raise HostedPluginError("VST3 worker response exceeds the host limit")
    payload = json.loads(pending[:end])
    del pending[:end + 1]
    if not isinstance(payload, dict):
        raise HostedPluginError("Invalid VST3 worker response")
    return payload


def exchange(control: socket.socket, pending: bytearray, payload: dict[str, Any],
             timeout: float, cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    control.settimeout(timeout)
    _check_cancel(cancel_check)
    control.sendall(_packet(payload))
    return read_message(control, pending, deadline, cancel_check)


def scan_vst3(path: str, cancel_check: Callable[[], bool] | None = None) -> tuple[HostedVst3Params, ...]:
    """Explicit user-initiated scan. Call from a worker, never a UI or audio thread."""
    candidate = Path(path).expanduser().resolve(strict=True)
    fingerprint = fingerprint_bundle(candidate, cancel_check)
    client = HostTransport()
    try:
        result = client.request("scan", START_TIMEOUT, path=str(candidate), cancel_check=cancel_check)
        names = result["names"]
        if (not isinstance(names, list) or not 0 < len(names) <= 64
                or len(set(names)) != len(names)
                or any(not isinstance(name, str) or not 0 < len(name) <= 256 for name in names)):
            raise HostedPluginError("Plugin did not report a usable VST3 component list")
        _record(fingerprint, {"path": str(candidate), "names": names})
        return tuple(HostedVst3Params(fingerprint, name) for name in names)
    except _WorkerFault as exc:
        _record(fingerprint, {"path": str(candidate), "error": str(exc)})
        raise
    finally:
        client.close()


def open_plugin(params: HostedVst3Params, rate: float,
                cancel_check: Callable[[], bool] | None = None) -> tuple[HostTransport, tuple[dict, ...]]:
    path = _registered_path(params)
    client = HostTransport()
    try:
        reply = client.request("load", START_TIMEOUT, path=str(path), params=params.to_dict(),
                               rate=rate, cancel_check=cancel_check)
        fields = reply.get("fields")
        if not isinstance(fields, list) or len(fields) > 512:
            raise HostedPluginError("Invalid VST3 parameter metadata")
        return client, tuple(fields)
    except _WorkerFault as exc:
        client.close()
        _record(params.fingerprint, {"path": str(path), "error": str(exc)})
        raise
    except BaseException:
        client.close()
        raise


def inspect_plugin(params: HostedVst3Params, rate: float,
                   cancel_check: Callable[[], bool] | None = None) -> tuple[dict, ...]:
    client, fields = open_plugin(params, rate, cancel_check)
    client.close()
    return fields


def _retire(future: Future[tuple[HostTransport, tuple[dict, ...]]]) -> None:
    if not future.cancel() and not future.exception():
        _CLEANUP.submit(future.result()[0].close)


class HostedVst3Effect:
    """EffectChain adapter. Playback issues mute the affected insert and report why."""

    effect_type = EFFECT_TYPE

    def __init__(self, sample_rate: float) -> None:
        self._rate = float(sample_rate)
        self._params: HostedVst3Params | None = None
        self._client: HostTransport | None = None
        self._future: Future[tuple[HostTransport, tuple[dict, ...]]] | None = None
        self._cancel_build = Event()
        self._realtime = False
        self._reset = True
        self._invalid = ""
        self._error = ""

    def enable_async_resources(self) -> None:
        self._realtime = True

    def set_params(self, params: object, sample_rate: float | None = None) -> None:
        rate = self._rate if sample_rate is None else float(sample_rate)
        try:
            if not isinstance(params, Mapping):
                raise ValueError("Missing VST3 parameters")
            state = HostedVst3Params.from_dict(params)
            if not math.isfinite(rate) or not 8000 <= rate <= 192000:
                raise ValueError("Unsupported VST3 sample rate")
        except ValueError as exc:
            self.release_resources()
            self._params = None
            self._invalid = str(exc)
            return
        self._invalid = ""
        if state != self._params or rate != self._rate:
            self.release_resources()
            self._params = state
            self._rate = rate
            self._error = ""
            self._reset = True

    def prepare(self) -> None:
        if (self._realtime and self._params is not None and self._client is None
                and self._future is None and not self._error):
            self._cancel_build = Event()
            self._future = _BUILDS.submit(open_plugin, self._params, self._rate,
                                          self._cancel_build.is_set)

    def bypass(self) -> None:
        self.release_resources()
        self._error = ""
        self._reset = True

    def release_resources(self) -> None:
        if self._future is not None:
            self._cancel_build.set()
            self._future.add_done_callback(_retire)
            self._future = None
        if self._client is not None:
            _CLEANUP.submit(self._client.close)
            self._client = None

    def __del__(self) -> None:
        try:
            self.release_resources()
        except RuntimeError:
            if self._client is not None:
                self._client.close()

    def reset_state(self) -> None:
        self._reset = True

    @property
    def is_active(self) -> bool:
        # Missing/invalid states stay active and fail explicitly.
        return True

    def tail_estimate_samples(self) -> int:
        # Pedalboard doesn't expose the declared tail. Give effects a bounded
        # render window for in-timeline decay. Delivery range remains explicit.
        return math.ceil(self._rate * 10)

    def collect_telemetry(self) -> dict[str, str]:
        detail = self._error or self._invalid
        if self._future is not None and not self._future.done():
            return {"pending": "Loading VST3 plug-in"}
        return {"error": detail} if detail else {}

    def process(self, samples: np.ndarray) -> np.ndarray:
        try:
            if self._error or self._invalid or self._params is None:
                raise HostedPluginError(self._error or self._invalid or "Invalid VST3 effect")
            if samples.ndim != 2 or samples.shape[0] != 2:
                raise HostedPluginError("VST3 insert supports a stereo processing layout only")
            if self._client is None:
                if not self._realtime:
                    self._client, _ = open_plugin(self._params, self._rate)
                else:
                    self.prepare()
                    if self._future is None or not self._future.done():
                        return np.zeros_like(samples)
                    future, self._future = self._future, None
                    self._client, _ = future.result()
            out = np.empty(samples.shape, np.float32)
            offset = 0
            while offset < samples.shape[1]:
                remaining = samples.shape[1] - offset
                frames = min(remaining, MAX_FRAMES)
                if 0 < remaining - frames < 3:
                    frames -= 3 - (remaining - frames)
                block = samples[:, offset:offset + frames]
                if frames <= 2:
                    raise HostedPluginError("VST3 processing requires blocks of at least 3 samples")
                np.copyto(np.ndarray((2, frames), dtype=np.float32,
                                     buffer=self._client.memory), block)
                timeout = min(0.03, max(0.003, frames / self._rate * 0.6)) if self._realtime else 5.0
                reply = self._client.request("process", timeout, frames=frames, reset=self._reset)
                if reply.get("frames") != frames:
                    raise HostedPluginError("Unexpected VST3 block length")
                np.copyto(out[:, offset:offset + frames],
                          np.ndarray((2, frames), dtype=np.float32, buffer=self._client.memory))
                self._reset = False
                offset += frames
            return out
        except (HostedPluginError, OSError, ValueError) as exc:
            self.release_resources()
            if not self._realtime:
                if isinstance(exc, HostedPluginError):
                    raise
                raise HostedPluginError(str(exc)) from exc
            if not self._error:
                self._error = str(exc)
                name = self._params.plugin_name if self._params else "VST3"
                logger.warning("VST3 effect %s muted: %s", name, self._error)
            return np.zeros_like(samples)

"""Child-process entry point for the optional VST3 host."""
from __future__ import annotations

import mmap
from pathlib import Path
import socket
import sys
from typing import Any

import numpy as np

from src.features.edit.core.hosted_vst3 import (
    HostedPluginError, HostedVst3Params, MAX_FRAMES, PCM_BYTES,
    _packet, fingerprint_bundle, read_message,
)


class _PluginWorker:
    def __init__(self, memory: mmap.mmap) -> None:
        self._memory = memory
        self._plugin: Any = None
        self._sample_rate = 48000.0

    @staticmethod
    def _check_latency(plugin: Any) -> None:
        if plugin.reported_latency_samples != 0:
            raise HostedPluginError("This VST3 beta requires a zero-latency effect")

    def execute(self, message: dict[str, Any]) -> dict[str, Any]:
        command = message["command"]
        if command == "scan":
            from pedalboard import VST3Plugin

            return {"names": VST3Plugin.get_plugin_names_for_file(message["path"])}
        if command == "load":
            from pedalboard import load_plugin

            path = Path(message["path"])
            config = HostedVst3Params.from_dict(message["params"])
            if fingerprint_bundle(path) != config.fingerprint:
                raise HostedPluginError("VST3 bundle changed since scan. Scan the new build to add it")
            plugin = load_plugin(str(path), plugin_name=config.plugin_name, initialization_timeout=6.0)
            if not plugin.is_effect or plugin.is_instrument:
                raise HostedPluginError("VST3 instruments cannot be inserted in an audio FX rack")
            self._sample_rate = float(message["rate"])
            parameters = plugin.parameters
            for name, value in config.parameters:
                if name not in parameters:
                    raise HostedPluginError(f"VST3 parameter {name!r} is unavailable")
                parameters[name].raw_value = value
            # Probe actual bus shape and latency at the negotiated rate.
            probe = plugin.process(np.zeros((2, 128), dtype=np.float32),
                                   self._sample_rate, reset=True)
            self._check_latency(plugin)
            if probe.shape != (2, 128) or not np.isfinite(probe).all():
                raise HostedPluginError("Effect cannot process the negotiated stereo layout")
            self._plugin = plugin
            return {"fields": [
                {"id": name, "label": str(parameter.name)[:128],
                 "value": float(parameter.raw_value),
                 "display": str(parameters[name].string_value)[:128]}
                for name, parameter in list(parameters.items())[:512]
            ]}
        if command == "process":
            frames = message["frames"]
            if self._plugin is None or type(frames) is not int or not 2 < frames <= MAX_FRAMES:
                raise HostedPluginError("Invalid VST3 processing request")
            block = np.ndarray((2, frames), dtype=np.float32, buffer=self._memory)
            output = self._plugin.process(block, self._sample_rate, reset=bool(message["reset"]))
            self._check_latency(self._plugin)
            if output.shape != block.shape or not np.isfinite(output).all():
                raise HostedPluginError("VST3 returned invalid stereo PCM")
            np.copyto(block, output)
            return {"frames": frames}
        raise HostedPluginError("Unknown VST3 command")


def main() -> None:
    with socket.socket(fileno=int(sys.argv[1])) as control:
        with mmap.mmap(int(sys.argv[2]), PCM_BYTES) as memory:
            worker = _PluginWorker(memory)
            pending = bytearray()
            while True:
                try:
                    message = read_message(control, pending)
                except ConnectionError:
                    break
                identity = {"generation": message["generation"], "index": message["index"]}
                try:
                    result = worker.execute(message)
                    control.sendall(_packet({**identity, **result}))
                except Exception as exc:
                    control.sendall(_packet({**identity, "error": str(exc)[:1000]}))
                    break


if __name__ == "__main__":
    main()

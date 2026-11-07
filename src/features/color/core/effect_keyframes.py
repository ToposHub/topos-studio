"""Instance-scoped continuous DCTL animation using the existing effect schema."""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence


def effect_animation_schema(instance: Mapping[str, Any], *,
                            schema_provider: Callable[[str], Any] | None = None) -> tuple[Mapping[str, Any], ...]:
    """Only continuous scalar parameters have an interpolation contract here."""
    if instance.get("type", "dctl") != "dctl":
        return ()
    resource_id = str(instance.get("plugin_id", ""))
    if schema_provider is None:
        from src.features.color.core.plugin_manager import get_plugin_manager
        metadata = get_plugin_manager().get_plugin(resource_id)
        schema = metadata.get("params") if metadata else None
    else:
        schema = schema_provider(resource_id)
    return tuple(entry for entry in schema or () if isinstance(entry, Mapping)
                 and entry.get("var_name") and entry.get("type", "slider") in ("slider", "float"))


def evaluate_effect_tracks(tracks: Mapping[str, Any], instances: Sequence[Any],
                           frame: int) -> list[Any] | None:
    """Return new instance/parameter dictionaries; never modify the base snapshot."""
    from src.features.color.core.grade_keyframes import GradeKeyframeTrack
    from src.features.color.core.keyframe_tracks import parse_track_name
    by_instance: dict[str, dict[str, Any]] = {}
    for name, data in tracks.items():
        address = parse_track_name(name)
        if address is None or address.domain != "fx":
            continue
        track = data if isinstance(data, GradeKeyframeTrack) else GradeKeyframeTrack.from_dict(dict(data))
        if track.keyframes:
            by_instance.setdefault(address.owner_id or "", {})[address.field] = track
    if not by_instance:
        return None
    result: list[Any] = []
    for instance in instances:
        if not isinstance(instance, Mapping):
            result.append(instance)
            continue
        patch = by_instance.get(str(instance.get("instance_id", "")))
        if not patch:
            result.append(dict(instance))
            continue
        params = dict(instance.get("params") or {})
        for name, track in patch.items():
            value = track.value_at(frame, params.get(name))
            if value is not None:
                params[name] = float(value)
        result.append({**instance, "params": params})
    return result


def remap_effect_tracks(tracks: Mapping[str, Any], identities: Mapping[str, str], *,
                        drop_missing: bool = False) -> dict[str, Any]:
    """Rebuild effect ownership together with copied or removed instances."""
    from src.features.color.core.keyframe_tracks import parse_track_name
    result = {}
    for name, track in tracks.items():
        address = parse_track_name(name)
        if address is None or address.domain != "fx":
            result[name] = track
            continue
        owner = identities.get(address.owner_id or "")
        if owner is None:
            if not drop_missing:
                result[name] = track
            continue
        destination = f"fx.{owner}.{address.field}"
        if isinstance(track, Mapping) and track.get("param_name") == name:
            track = {**track, "param_name": destination}
        result[destination] = track
    return result

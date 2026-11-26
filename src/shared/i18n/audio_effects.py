"""Display names shared by the Edit and Mixer audio effect libraries."""

from src.shared.i18n.i18n import I18N


_NAME_KEYS = {
    "strip_eq": "audio.fx.parametric_eq",
    "expander": "audio.fx.expander_gate",
    "reverb": "audio.fx.acoustic_reverb",
    "delay": "audio.fx.stereo_delay",
}


def audio_effect_display_name(effect_type: str, default: str, *, use_english: bool = False) -> str:
    name = I18N.tr(
        _NAME_KEYS.get(effect_type, f"audio.fx.{effect_type}"),
        default,
        use_english=use_english,
    )
    return I18N.tr(f"audio.fx.name.{effect_type}", name, use_english=use_english)

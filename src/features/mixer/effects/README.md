# Mixer effect panels

The 24 designs from `Topos test` are tracked in [the source manifest](source_manifest.json). [The panel schemas](parameter_schemas.json) describe the designs' state snapshots. Production processing and serialization continue to use the Edit effect registry and the existing project parameter types.

`ProductionEffectDialog` hosts the designs for existing Clip, Track, Bus and Master effects, including grouped Dynamics and the track Spatial Panner. Controls in the design cards read and write the production model through the bindings. The window uses the panel layout's minimum size so the controls remain visible during resizing. Apply and OK submit through the existing timeline or mixer commands, and Cancel restores the last committed processing. Project metadata travels with the parameter snapshot. The handoff lists the processors and features that still need implementation. [Classic mappings](classic_bindings.py) · [Rack mappings](rack_bindings.py) · [Backend handoff](../../../../docs/plans/2026-11-21-audio-effect-panels-backend-handoff.md)

The project Mixer library opens the Clip Editor through its timeline selection and lists five panels awaiting production processing. These panels use `EffectWindowManager` to cache session state across close and reopen. They use the design catalog, which describes UI availability. The production registry controls insertion, preview and export.

Dropdowns reuse the application's `ChevronComboBox` and its shared popup, including the icon-bearing filter and shaper selectors. Numeric editors reuse the application input styles. The supplied design graphics, knobs and value pills fit into compact rows according to the controls available in each panel. Demonstration meter timers are stopped in hosted panels.

The standalone Mixer UI session also uses the design catalog. Its `EffectBrowserWidget` is owner-scoped and emits `effectChosen(effect_id)`; its rack edits session state.

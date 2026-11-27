# Mixer effect panels

The 24 designs from `Topos test` are tracked in [the source manifest](source_manifest.json). [The panel schemas](parameter_schemas.json) describe the designs' state snapshots. Production processing and serialization continue to use the Edit effect registry and the existing project parameter types.

`ProductionEffectDialog` hosts the designs for existing Clip, Track, Bus and Master effects, including grouped Dynamics and the track Spatial Panner. Controls in the design cards read and write the production model through the bindings. The window uses the panel layout's minimum size so the controls remain visible during resizing. Apply and OK submit through the existing timeline or mixer commands, and Cancel restores the last committed processing. Project metadata travels with the parameter snapshot. The handoff lists the processors and features that still need implementation. [Classic mappings](classic_bindings.py) · [Rack mappings](rack_bindings.py) · [Backend handoff](../../../../docs/plans/2026-11-21-audio-effect-panels-backend-handoff.md)

The project Mixer library opens the Clip Editor through its timeline selection and lists five panels awaiting production processing. These panels use `EffectWindowManager` to cache session state across close and reopen. They use the design catalog, which describes UI availability. The production registry controls insertion, preview and export.

Dropdowns reuse the application's `ChevronComboBox` and its shared popup, including the icon-bearing filter and shaper selectors. Numeric editors reuse the application input styles. The supplied design graphics, knobs and value pills fit into compact rows according to the controls available in each panel. Demonstration meter timers are stopped in hosted panels.

Dynamic EQ, Saturation and Transient Designer use the processing model in the effect snapshot. Double-click empty space in the Dynamic EQ graph or choose **Add Band** from its context menu to add a band. The first addition moves the existing single-band settings and automation to the first band. Click a node to select its band, double-click it to disable the band, or right-click empty space to restore a disabled band.

Effect windows stay above the workspace. The audio engine collects per-instance spectra only for visible mixer windows with their spectrum display enabled. The channel EQ can use the track spectrum before its first insert is committed.

New channel Dynamics groups apply Make Up after all three stages. Older groups retain their stored routing. The True Peak Limiter meters use the limiter's measured output, and its linked mode applies a single gain envelope across channels. Transient Designer sidechain listening is temporary while editing, and its filter can be bypassed independently.

The acoustic reverb graph uses the processor's room geometry for early reflection positions and marks the late decay as a prediction. The delay graph shows the first cross-feedback echo after both channel delays. Delay meters read the two channel peaks at the insert output.

Convolution Reverb decodes the selected WAV and prepares its waveform in the background. True Stereo uses the four LL, LR, RL and RR paths of a four-channel file with the current processor. Mono input folds the two resulting signals back to the host's mono layout. The current processor uses larger FFT partitions for long tails and retains zero algorithm delay across playback and export block sizes. Existing version 4 and earlier instances retain their historical timing.

The standalone Mixer UI session also uses the design catalog. Its `EffectBrowserWidget` is owner-scoped and emits `effectChosen(effect_id)`; its rack edits session state.

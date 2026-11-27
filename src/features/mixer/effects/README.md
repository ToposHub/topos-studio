# Mixer effect panels

The 24 designs from `Topos test` are tracked in [the source manifest](source_manifest.json). [The panel schemas](parameter_schemas.json) describe the designs' state snapshots. Production processing and serialization continue to use the Edit effect registry and the existing project parameter types.

`ProductionEffectDialog` hosts the designs for existing Clip, Track, Bus and Master effects, including grouped Dynamics and the track Spatial Panner. Controls in the design cards read and write the production model through the bindings. The window uses the panel layout's minimum size so the controls remain visible during resizing. Apply and OK submit through the existing timeline or mixer commands, and Cancel restores the last committed processing. Project metadata travels with the parameter snapshot. The handoff lists the processors and features that still need implementation. [Classic mappings](classic_bindings.py) · [Rack mappings](rack_bindings.py) · [Backend handoff](../../../../docs/plans/2026-11-21-audio-effect-panels-backend-handoff.md)

The project Mixer library exposes the registered inserts, the track Spatial Panner, the Audio Clip Editor and three clip processing panels. The production registry controls real-time insertion, preview and export. De-Click / De-Hum, Pitch Correction and Doubler use the selected clip's pre-rendered processing stack.

Dropdowns reuse the application's `ChevronComboBox` and its shared popup, including the icon-bearing filter and shaper selectors. Numeric editors reuse the application input styles. The supplied design graphics, knobs and value pills fit into compact rows according to the controls available in each panel. Demonstration meter timers are stopped in hosted panels.

Dynamic EQ, Saturation and Transient Designer use the processing model in the effect snapshot. Double-click empty space in the Dynamic EQ graph or choose **Add Band** from its context menu to add a band. The first addition moves the existing single-band settings and automation to the first band. Click a node to select its band, double-click it to disable the band, or right-click empty space to restore a disabled band.

Effect windows stay above the workspace. The audio engine collects per-instance spectra only for visible mixer windows with their spectrum display enabled. The channel EQ can use the track spectrum before its first insert is committed.

New channel Dynamics groups apply Make Up after all three stages. Older groups retain their stored routing. The True Peak Limiter meters use the limiter's measured output, and its linked mode applies a single gain envelope across channels. Transient Designer sidechain listening is temporary while editing, and its filter can be bypassed independently.

The acoustic reverb graph uses the processor's room geometry for early reflection positions and marks the late decay as a prediction. The delay graph shows the first cross-feedback echo after both channel delays. Delay meters read the two channel peaks at the insert output.

Convolution Reverb decodes the selected WAV and prepares its waveform in the background. True Stereo uses the four LL, LR, RL and RR paths of a four-channel file with the current processor. Mono input folds the two resulting signals back to the host's mono layout. The current processor uses larger FFT partitions for long tails and retains zero algorithm delay across playback and export block sizes. Existing version 4 and earlier instances retain their historical timing.

Chorus / Flanger shares a fractional-delay processor with signed feedback and one to four voices. Its presets select the mode and its corresponding delay and feedback settings. Switching the mode button retains the edited controls. Modulation depth and base delay are shown in milliseconds. The current processor keeps read-head timing and feedback history consistent across preview and export blocks.

The modulation graph estimates the mixed response of Chorus, Flanger and Phaser at the current LFO phase on mixer channels. Clip settings show the response at a reference phase. New Phaser inserts process each channel separately. Saved Phaser snapshots without the stereo field retain their mono-summed wet signal.

Saturation offers the original WaveShaper and a three-band processor with independent crossovers. Saved multiband inserts with the original crossover retain that sound until Separate Crossovers is selected or a crossover is edited. The transfer inset plots the selected shaper's mathematical curve. The live spectrum subscribes only while its window is visible and SPEC is enabled.

Exciter offers even and odd harmonic generation for each of its two bands. Preserve Dry keeps a delayed copy of the original signal and applies 2× oversampling to the added harmonics. Original retains the historical 2× wet signal, and saved inserts without an oversampling path open in Original mode. The graph plots measured input and output spectra while the window is visible.

Subharmonic uses a selectable detection band in Focused mode. When a source locks, the oscillator adds one and two octaves below the detected frequency. The phases continue into release when the source falls out of range. Legacy restores the historical fixed 45–180 Hz detector and mixing behavior. Both modes pass the dry input without a host delay. The panel shows actual pitch, lock confidence and generated level without subscribing to an FFT meter.

The Audio Clip Editor publishes processed audition audio and waveform bins before its spectrogram and loudness scan. Opening the spectrogram or switching FFT resolution reuses the processed audio. The overview and zoom detail jobs run while the spectrogram and its dock tab are active. Selection processing auditions the selected interval at its timeline offset, with the clip gain, fades and inserts applied to both sides of the comparison.

Open a clip processor from the Mixer library or from **Selection processing** in the clip editor's context menu. Its window identifies the target interval and channels. With no selection it processes the entire clip. Audition and Difference leave the project untouched. Apply commits one undoable edit and uses the same candidate render for the processing stack, playback and export. Changing the target or parameters, closing or minimizing the window, or leaving the clip editor cancels pending work.

De-Click / De-Hum can apply one or both repair steps in a single edit. De-Click estimates the short-term noise floor per channel and protects broadband transients. Pitch Correction tracks a predominant pitch and can use a predefined scale, user-picked target notes and bypass notes. Doubler supports up to four fixed-interval voices and reuses the shifted signal when multiple voices have the same interval and detune. Existing recipes keep their parameters and ranges. Derived PCM is rebuilt for the updated processors.

The standalone Mixer UI session also uses the design catalog. Its `EffectBrowserWidget` is owner-scoped and emits `effectChosen(effect_id)`; its rack edits session state.

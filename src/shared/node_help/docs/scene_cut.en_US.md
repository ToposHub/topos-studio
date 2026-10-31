# Scene Cut Detection

Scene Cut Detection analyzes a video for shot boundaries and lets you review each candidate before adding cuts to the Media Pool.

## Basic workflow

1. Click **Auto Scene Detect** and wait for the analysis to finish.
2. Select a candidate cut in the graph or the scene list.
3. Use the three-panel preview to compare the frame before, current frame, and frame after the cut.
4. Accept valid candidates and reject false positives.
5. Click **Add Cuts to Media Pool** to create virtual subclips from the confirmed cuts.

## Playback and precise positioning

- **Left / Right**: move backward or forward by one frame.
- **Space**: play or pause the preview.
- **Up / Down**: move to the previous or next detected candidate.
- **Previous Frame / Next Frame buttons**: the same one-frame stepping actions as the arrow keys.
- Playback stops automatically at the end of the source.

## Scene list

- **Decision**: accepted, pending, or rejected review state.
- **Score**: detection confidence for the candidate.
- **Type**: candidate classification, such as Hard Cut, Transition, or Flash.
- Selecting a row synchronizes the graph, preview, and current frame position.

## Graph and batch actions

- Drag the threshold line in the graph to adjust the active review boundary.
- Use the batch menu to accept high-confidence candidates, reject warnings, or reset all decisions.
- Detection provides candidates only; you decide which cuts are added to the Media Pool.

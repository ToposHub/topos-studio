#!/usr/bin/env python3
"""
Generate synthetic test video fixtures for Scene Cut Detection testing.

This tool creates synthetic video files designed to test various aspects
of the scene cut detection algorithm, including:
- Hard cuts (frame-to-frame color transitions)
- Flash events (single-frame brightness spikes)
- Dissolve transitions (gradual opacity blending)
- Black gaps (inter-scene black frames)
- Fast motion (continuous pixel movement without cuts)

All fixtures are generated using OpenCV/numpy, no real video sources needed.

Usage:
    # Generate all fixtures
    .venv/bin/python tools/generate_scene_cut_fixtures.py

    # Generate specific fixture
    .venv/bin/python tools/generate_scene_cut_fixtures.py --fixture hard_cut

    # Generate with custom parameters
    .venv/bin/python tools/generate_scene_cut_fixtures.py --fixture flash --fps 30 --duration 5

    # List all fixture types
    .venv/bin/python tools/generate_scene_cut_fixtures.py --list

Output directory: tests/fixtures/scene_cut/

Reference: docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md
    P1-6: Basic fixtures (hard_cut, flash, fast_pan, dissolve, black_gap)
    P8:  Edge cases (vfr, duplicate frames, fps conversion)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "scene_cut"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

logger = logging.getLogger("generate_scene_cut_fixtures")


# =============================================================================
# Fixture Types Registry
# =============================================================================

class FixtureType(Enum):
    """All available fixture types with their descriptions."""
    HARD_CUT = "hard_cut"
    FLASH = "flash"
    DISSOLVE = "dissolve"
    BLACK_GAP = "black_gap"
    FAST_PAN = "fast_pan"
    VFR_CUT = "vfr_cut"
    DUPLICATE_FRAMES = "duplicate_frames"
    BLACK_FADE = "black_fade"


@dataclass
class FixtureSpec:
    """Specification for a single fixture video."""
    name: str
    type: FixtureType
    fps: float = 24.0
    width: int = 640
    height: int = 480
    total_frames: int = 300
    description: str = ""
    expected_cuts: List[int] = field(default_factory=list)
    expected_cut_count: int = 0
    metadata: Dict = field(default_factory=dict)
    generator_func: Optional[Callable] = None

    def get_filename(self) -> str:
        """Generate filename based on fps."""
        fps_int = int(self.fps)
        return f"{self.name}_{fps_int}fps.mp4"

    def get_output_path(self) -> Path:
        """Full output path for this fixture."""
        return FIXTURE_DIR / self.get_filename()


# =============================================================================
# Frame Generators
# =============================================================================

def create_solid_frame(width: int, height: int, color: Tuple[int, int, int]) -> np.ndarray:
    """Create a solid color frame in BGR format."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = color  # BGR ordering for OpenCV
    return frame


def create_gradient_frame(
    width: int,
    height: int,
    start_color: Tuple[int, int, int],
    end_color: Tuple[int, int, int],
    direction: str = "horizontal"
) -> np.ndarray:
    """Create a gradient frame."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)

    if direction == "horizontal":
        for x in range(width):
            ratio = x / (width - 1) if width > 1 else 0
            for c in range(3):
                frame[:, x, c] = int(start_color[c] + (end_color[c] - start_color[c]) * ratio)
    else:  # vertical
        for y in range(height):
            ratio = y / (height - 1) if height > 1 else 0
            for c in range(3):
                frame[y, :, c] = int(start_color[c] + (end_color[c] - start_color[c]) * ratio)

    return frame


def blend_frames(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    alpha: float
) -> np.ndarray:
    """Blend two frames with given alpha (0.0 = full A, 1.0 = full B)."""
    return cv2.addWeighted(frame_a, 1.0 - alpha, frame_b, alpha, 0)


def create_white_flash_frame(
    base_frame: np.ndarray,
    flash_intensity: float = 1.0
) -> np.ndarray:
    """Create a white flash overlay on a base frame."""
    white = np.full_like(base_frame, 255)
    return blend_frames(base_frame, white, flash_intensity)


def create_motion_frame(
    width: int,
    height: int,
    offset_x: int,
    base_color: Tuple[int, int, int] = (100, 100, 100)
) -> np.ndarray:
    """Create a frame with horizontal motion pattern."""
    frame = create_solid_frame(width, height, base_color)

    # Add a vertical stripe that moves
    stripe_width = 60
    stripe_color = (50, 200, 50)  # Green stripe (BGR)
    stripe_x = (offset_x % (width + stripe_width)) - stripe_width // 2

    if 0 <= stripe_x < width:
        end_x = min(stripe_x + stripe_width, width)
        frame[:, stripe_x:end_x] = stripe_color

    # Add some texture for better motion detection
    noise = np.random.randint(0, 20, (height, width, 3), dtype=np.uint8)
    frame = cv2.add(frame, noise)

    return frame


# =============================================================================
# Fixture Generators
# =============================================================================

def generate_hard_cut_fixture(spec: FixtureSpec) -> bool:
    """
    Generate hard_cut fixture: solid color scene transitions.

    Creates a video with distinct scenes of solid colors:
    - Scene 1: Red frames (frames 0-99)
    - Scene 2: Blue frames (frames 100-199)
    - Scene 3: Green frames (frames 200-299)
    - Optional: Yellow frames (frames 300-399)

    Expected cuts at frames: 100, 200 (and 300 if extended)
    """
    output_path = spec.get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        spec.fps,
        (spec.width, spec.height)
    )

    if not writer.isOpened():
        logger.error(f"Failed to open video writer for {output_path}")
        return False

    # Define scene colors (BGR format)
    scenes = [
        ((50, 50, 200), 100),   # Red scene, 100 frames
        ((200, 50, 50), 100),   # Blue scene, 100 frames
        ((50, 200, 50), 100),   # Green scene, 100 frames
    ]

    # If total_frames is more than 300, add more scenes
    remaining = spec.total_frames - 300
    if remaining > 0:
        scenes.append(((200, 200, 50), remaining))  # Yellow scene

    frame_idx = 0
    spec.expected_cuts = []

    for color, duration in scenes:
        # Record cut point at the start of each scene (except first)
        if frame_idx > 0:
            spec.expected_cuts.append(frame_idx)

        for _ in range(duration):
            frame = create_solid_frame(spec.width, spec.height, color)
            writer.write(frame)
            frame_idx += 1

    writer.release()
    spec.expected_cut_count = len(spec.expected_cuts)

    logger.info(
        f"Generated {output_path.name}: {spec.total_frames} frames, "
        f"{spec.expected_cut_count} expected cuts at {spec.expected_cuts}"
    )

    return True


def generate_flash_fixture(spec: FixtureSpec) -> bool:
    """
    Generate flash fixture: single-frame white flash without actual cut.

    Creates a video with a consistent scene that has a brief white flash
    in the middle. The flash should NOT be detected as a hard cut.

    Pattern:
    - Frames 0-149: Consistent gray scene
    - Frame 150: Full white flash
    - Frames 151-299: Same gray scene continues

    Expected: No hard cuts, flash should be suppressed/marked as FLASH
    """
    output_path = spec.get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        spec.fps,
        (spec.width, spec.height)
    )

    if not writer.isOpened():
        logger.error(f"Failed to open video writer for {output_path}")
        return False

    # Base scene: consistent gray
    base_color = (120, 120, 120)  # Gray (BGR)
    base_frame = create_solid_frame(spec.width, spec.height, base_color)

    # Add some texture to make it look more realistic
    noise = np.random.randint(0, 10, (spec.height, spec.width, 3), dtype=np.uint8)
    base_frame = cv2.add(base_frame, noise)

    flash_frame = int(spec.total_frames / 2)  # Flash at middle frame

    for i in range(spec.total_frames):
        if i == flash_frame:
            # Full white flash
            frame = create_solid_frame(spec.width, spec.height, (255, 255, 255))
        else:
            # Use consistent base frame with slight noise variation
            frame = base_frame.copy()
            frame_noise = np.random.randint(0, 5, (spec.height, spec.width, 3), dtype=np.uint8)
            frame = cv2.add(frame, frame_noise)

        writer.write(frame)

    writer.release()
    spec.expected_cuts = []  # No expected cuts
    spec.expected_cut_count = 0
    spec.metadata["flash_frame"] = flash_frame

    logger.info(
        f"Generated {output_path.name}: {spec.total_frames} frames, "
        f"flash at frame {flash_frame}, expected 0 cuts (flash should be suppressed)"
    )

    return True


def generate_dissolve_fixture(spec: FixtureSpec) -> bool:
    """
    Generate dissolve fixture: 12-frame dissolve transition.

    Creates a video with two distinct scenes connected by a dissolve
    transition. The dissolve should be marked as SOFT_TRANSITION,
    not a hard cut.

    Pattern:
    - Frames 0-99: Red scene
    - Frames 100-111: Dissolve from red to blue (12 frames)
    - Frames 112-199: Blue scene

    Expected: Dissolve transition at ~frame 100-111, not hard cut
    """
    output_path = spec.get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        spec.fps,
        (spec.width, spec.height)
    )

    if not writer.isOpened():
        logger.error(f"Failed to open video writer for {output_path}")
        return False

    dissolve_frames = 12
    scene_a_frames = 100
    scene_b_frames = 100

    # Colors (BGR)
    color_a = (50, 50, 200)  # Red
    color_b = (200, 50, 50)  # Blue

    frame_a = create_solid_frame(spec.width, spec.height, color_a)
    frame_b = create_solid_frame(spec.width, spec.height, color_b)

    # Scene A (before dissolve)
    for i in range(scene_a_frames):
        writer.write(frame_a)

    # Dissolve transition
    dissolve_start_frame = scene_a_frames
    for i in range(dissolve_frames):
        alpha = i / (dissolve_frames - 1) if dissolve_frames > 1 else 0.5
        blended = blend_frames(frame_a, frame_b, alpha)
        writer.write(blended)

    # Scene B (after dissolve)
    for i in range(scene_b_frames):
        writer.write(frame_b)

    writer.release()

    spec.total_frames = scene_a_frames + dissolve_frames + scene_b_frames
    spec.expected_cuts = [scene_a_frames]  # Mark the start of transition
    spec.expected_cut_count = 1
    spec.metadata["dissolve_start"] = scene_a_frames
    spec.metadata["dissolve_end"] = scene_a_frames + dissolve_frames
    spec.metadata["dissolve_frames"] = dissolve_frames

    logger.info(
        f"Generated {output_path.name}: {spec.total_frames} frames, "
        f"dissolve at frames {scene_a_frames}-{scene_a_frames + dissolve_frames - 1}, "
        f"should be marked as SOFT_TRANSITION"
    )

    return True


def generate_black_gap_fixture(spec: FixtureSpec) -> bool:
    """
    Generate black_gap fixture: black frames between scenes.

    Creates a video with scenes separated by black gaps. The black gaps
    should be handled appropriately (not necessarily marked as cuts).

    Pattern:
    - Frames 0-99: Red scene
    - Frames 100-119: Black gap (20 frames)
    - Frames 120-219: Blue scene
    - Frames 220-239: Black gap (20 frames)
    - Frames 240-339: Green scene

    Expected: Black gaps should be marked as BLACK_GAP
    """
    output_path = spec.get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        spec.fps,
        (spec.width, spec.height)
    )

    if not writer.isOpened():
        logger.error(f"Failed to open video writer for {output_path}")
        return False

    scene_frames = 100
    black_gap_frames = 20

    # Colors (BGR)
    scenes = [
        (50, 50, 200),   # Red
        (200, 50, 50),   # Blue
        (50, 200, 50),   # Green
    ]
    black_color = (0, 0, 0)

    frame_idx = 0
    spec.expected_cuts = []

    for scene_color in scenes:
        # Scene content
        for _ in range(scene_frames):
            frame = create_solid_frame(spec.width, spec.height, scene_color)
            writer.write(frame)
            frame_idx += 1

        # Black gap (except after last scene)
        if scene_color != scenes[-1]:
            for _ in range(black_gap_frames):
                frame = create_solid_frame(spec.width, spec.height, black_color)
                writer.write(frame)
                frame_idx += 1

    writer.release()

    spec.total_frames = frame_idx
    spec.expected_cuts = [
        scene_frames + black_gap_frames,  # Start of blue scene
        2 * scene_frames + 2 * black_gap_frames,  # Start of green scene
    ]
    spec.expected_cut_count = len(spec.expected_cuts)
    spec.metadata["black_gaps"] = [
        (scene_frames, scene_frames + black_gap_frames),
        (2 * scene_frames + black_gap_frames, 2 * scene_frames + 2 * black_gap_frames),
    ]

    logger.info(
        f"Generated {output_path.name}: {spec.total_frames} frames, "
        f"black gaps at {spec.metadata['black_gaps']}"
    )

    return True


def generate_fast_pan_fixture(spec: FixtureSpec) -> bool:
    """
    Generate fast_pan fixture: continuous fast motion without cuts.

    Creates a video with continuous horizontal motion (pan effect).
    The motion should NOT be detected as cuts; it should be marked
    as FAST_MOTION with lower confidence.

    Pattern:
    - All frames show a moving stripe across a static background
    - Stripe moves 10 pixels per frame (fast motion)

    Expected: No cuts, motion should be suppressed or have low confidence
    """
    output_path = spec.get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        spec.fps,
        (spec.width, spec.height)
    )

    if not writer.isOpened():
        logger.error(f"Failed to open video writer for {output_path}")
        return False

    motion_speed = 10  # pixels per frame

    for i in range(spec.total_frames):
        offset_x = i * motion_speed
        frame = create_motion_frame(spec.width, spec.height, offset_x)
        writer.write(frame)

    writer.release()

    spec.expected_cuts = []
    spec.expected_cut_count = 0
    spec.metadata["motion_speed"] = motion_speed

    logger.info(
        f"Generated {output_path.name}: {spec.total_frames} frames, "
        f"continuous motion at {motion_speed} px/frame, "
        f"expected 0 cuts (motion should be suppressed)"
    )

    return True


def generate_vfr_cut_fixture(spec: FixtureSpec) -> bool:
    """
    Generate vfr_cut fixture: variable frame rate with hard cuts.

    Creates a video that simulates VFR behavior where frame durations
    are not constant. Tests PTS-based mapping accuracy.

    Pattern:
    - Frames with varying effective durations (simulated via metadata)
    - Scene cuts at specific frames

    Note: OpenCV doesn't directly support VFR encoding. This fixture
    uses constant frame rate but the test harness should simulate
    VFR PTS mapping.

    Expected: Cuts should be detected with PTS-aware mapping
    """
    output_path = spec.get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # For now, generate a CFR video with scene cuts
    # VFR simulation will be done in test harness
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        spec.fps,
        (spec.width, spec.height)
    )

    if not writer.isOpened():
        logger.error(f"Failed to open video writer for {output_path}")
        return False

    # Create 4 scenes with cuts
    scenes = [
        ((50, 50, 200), 80),   # Red, 80 frames (shorter)
        ((200, 50, 50), 120),  # Blue, 120 frames (longer)
        ((50, 200, 50), 60),   # Green, 60 frames (shortest)
        ((200, 200, 50), 100), # Yellow, 100 frames
    ]

    frame_idx = 0
    spec.expected_cuts = []

    for color, duration in scenes:
        if frame_idx > 0:
            spec.expected_cuts.append(frame_idx)

        for _ in range(duration):
            frame = create_solid_frame(spec.width, spec.height, color)
            writer.write(frame)
            frame_idx += 1

    writer.release()

    spec.total_frames = frame_idx
    spec.expected_cut_count = len(spec.expected_cuts)
    spec.metadata["vfr_simulation"] = "Test harness should simulate variable PTS"

    logger.info(
        f"Generated {output_path.name}: {spec.total_frames} frames, "
        f"{spec.expected_cut_count} cuts at {spec.expected_cuts}, "
        f"VFR simulation needed in test harness"
    )

    return True


def generate_duplicate_frames_fixture(spec: FixtureSpec) -> bool:
    """
    Generate duplicate_frames fixture: repeated frames without cuts.

    Creates a video with intentionally duplicated frames to test
    that the detector doesn't mistake repeated frames for cuts.

    Pattern:
    - Frames 0-99: Red scene, but every 10th frame is duplicated
    - Frames 100-199: Blue scene

    Expected: Single cut at frame 100, duplicated frames should not
    create false positives
    """
    output_path = spec.get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        spec.fps,
        (spec.width, spec.height)
    )

    if not writer.isOpened():
        logger.error(f"Failed to open video writer for {output_path}")
        return False

    color_a = (50, 50, 200)  # Red
    color_b = (200, 50, 50)  # Blue

    frame_a = create_solid_frame(spec.width, spec.height, color_a)
    frame_b = create_solid_frame(spec.width, spec.height, color_b)

    duplicate_interval = 10

    # Scene A with duplicates
    for i in range(100):
        writer.write(frame_a)
        # Duplicate every 10th frame
        if i % duplicate_interval == duplicate_interval - 1:
            writer.write(frame_a)  # Duplicate

    # Scene B (no duplicates)
    for i in range(100):
        writer.write(frame_b)

    writer.release()

    spec.total_frames = 100 + 10 + 100  # Original + duplicates + scene B
    spec.expected_cuts = [110]  # Cut at start of scene B (after duplicates)
    spec.expected_cut_count = 1
    spec.metadata["duplicate_frames"] = [9, 19, 29, 39, 49, 59, 69, 79, 89, 99]

    logger.info(
        f"Generated {output_path.name}: {spec.total_frames} frames, "
        f"cut at frame 110, 10 duplicated frames should not cause false positives"
    )

    return True


def generate_black_fade_fixture(spec: FixtureSpec) -> bool:
    """
    Generate black_fade fixture: fade to black and fade from black.

    Creates a video with gradual fade transitions to/from black.
    These should be marked as BLACK_FADE, not hard cuts.

    Pattern:
    - Frames 0-80: Red scene
    - Frames 80-99: Fade to black (20 frames)
    - Frames 100-119: Black
    - Frames 120-139: Fade from black to blue (20 frames)
    - Frames 140-219: Blue scene

    Expected: Fade regions should be marked as BLACK_FADE
    """
    output_path = spec.get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(
        str(output_path),
        fourcc,
        spec.fps,
        (spec.width, spec.height)
    )

    if not writer.isOpened():
        logger.error(f"Failed to open video writer for {output_path}")
        return False

    fade_frames = 20
    scene_before = 80
    black_frames = 20
    scene_after = 80

    color_a = (50, 50, 200)  # Red
    color_b = (200, 50, 50)  # Blue
    black = (0, 0, 0)

    frame_a = create_solid_frame(spec.width, spec.height, color_a)
    frame_b = create_solid_frame(spec.width, spec.height, color_b)
    frame_black = create_solid_frame(spec.width, spec.height, black)

    # Scene A
    for _ in range(scene_before):
        writer.write(frame_a)

    # Fade to black
    fade_out_start = scene_before
    for i in range(fade_frames):
        alpha = i / fade_frames
        blended = blend_frames(frame_a, frame_black, alpha)
        writer.write(blended)

    # Black
    for _ in range(black_frames):
        writer.write(frame_black)

    # Fade from black
    fade_in_start = scene_before + fade_frames + black_frames
    for i in range(fade_frames):
        alpha = i / fade_frames
        blended = blend_frames(frame_black, frame_b, alpha)
        writer.write(blended)

    # Scene B
    for _ in range(scene_after):
        writer.write(frame_b)

    writer.release()

    spec.total_frames = scene_before + fade_frames + black_frames + fade_frames + scene_after
    spec.expected_cuts = [fade_in_start + fade_frames]  # Cut after fade in
    spec.expected_cut_count = 1
    spec.metadata["fade_out"] = (fade_out_start, fade_out_start + fade_frames)
    spec.metadata["fade_in"] = (fade_in_start, fade_in_start + fade_frames)

    logger.info(
        f"Generated {output_path.name}: {spec.total_frames} frames, "
        f"fade out at {fade_out_start}-{fade_out_start + fade_frames - 1}, "
        f"fade in at {fade_in_start}-{fade_in_start + fade_frames - 1}"
    )

    return True


# =============================================================================
# Fixture Registry
# =============================================================================

def create_fixture_registry() -> Dict[FixtureType, FixtureSpec]:
    """Create the default fixture specifications."""
    return {
        FixtureType.HARD_CUT: FixtureSpec(
            name="hard_cut",
            type=FixtureType.HARD_CUT,
            fps=24.0,
            width=640,
            height=480,
            total_frames=300,
            description="Red/Blue/Green solid color scene transitions",
            generator_func=generate_hard_cut_fixture,
        ),
        FixtureType.FLASH: FixtureSpec(
            name="flash_no_cut",
            type=FixtureType.FLASH,
            fps=24.0,
            width=640,
            height=480,
            total_frames=300,
            description="Single-frame white flash without actual cut",
            generator_func=generate_flash_fixture,
        ),
        FixtureType.DISSOLVE: FixtureSpec(
            name="dissolve",
            type=FixtureType.DISSOLVE,
            fps=24.0,
            width=640,
            height=480,
            total_frames=212,
            description="12-frame dissolve transition between scenes",
            generator_func=generate_dissolve_fixture,
        ),
        FixtureType.BLACK_GAP: FixtureSpec(
            name="black_gap",
            type=FixtureType.BLACK_GAP,
            fps=24.0,
            width=640,
            height=480,
            total_frames=340,  # Will be calculated
            description="Black frames between scene transitions",
            generator_func=generate_black_gap_fixture,
        ),
        FixtureType.FAST_PAN: FixtureSpec(
            name="fast_pan",
            type=FixtureType.FAST_PAN,
            fps=24.0,
            width=640,
            height=480,
            total_frames=300,
            description="Continuous fast horizontal motion without cuts",
            generator_func=generate_fast_pan_fixture,
        ),
        FixtureType.VFR_CUT: FixtureSpec(
            name="vfr_cut",
            type=FixtureType.VFR_CUT,
            fps=24.0,
            width=640,
            height=480,
            total_frames=360,
            description="Variable frame rate with scene cuts (CFR base)",
            generator_func=generate_vfr_cut_fixture,
        ),
        FixtureType.DUPLICATE_FRAMES: FixtureSpec(
            name="duplicate_frames",
            type=FixtureType.DUPLICATE_FRAMES,
            fps=24.0,
            width=640,
            height=480,
            total_frames=210,
            description="Repeated frames to test false positive suppression",
            generator_func=generate_duplicate_frames_fixture,
        ),
        FixtureType.BLACK_FADE: FixtureSpec(
            name="black_fade",
            type=FixtureType.BLACK_FADE,
            fps=24.0,
            width=640,
            height=480,
            total_frames=220,
            description="Fade to black and fade from black transitions",
            generator_func=generate_black_fade_fixture,
        ),
    }


# =============================================================================
# Video Encoding Utilities
# =============================================================================

def reencode_with_h264(input_path: Path, output_path: Path, fps: float) -> bool:
    """
    Re-encode video with H.264 codec for better compatibility.

    OpenCV's mp4v codec may not be optimal for testing.
    This function uses ffmpeg to create a proper H.264 encode.
    """
    try:
        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output
            "-i", str(input_path),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            str(output_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            logger.warning(f"ffmpeg re-encode failed: {result.stderr}")
            return False

        return True

    except FileNotFoundError:
        logger.warning("ffmpeg not found, skipping H.264 re-encode")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg re-encode timeout")
        return False


# =============================================================================
# Main CLI
# =============================================================================

def list_fixtures(
    registry: Dict[FixtureType, FixtureSpec],
    output_dir: Path = FIXTURE_DIR
) -> None:
    """Print available fixture types."""
    print("\nAvailable Scene Cut Detection Fixtures:\n")
    print(f"{'Name':<20} {'FPS':<8} {'Frames':<8} {'Description'}")
    print("-" * 80)

    for spec in registry.values():
        print(f"{spec.name:<20} {int(spec.fps):<8} {spec.total_frames:<8} {spec.description}")

    print("\nOutput directory:", output_dir)


def generate_fixture(
    spec: FixtureSpec,
    output_dir: Path = FIXTURE_DIR,
    reencode: bool = False,
    verbose: bool = False
) -> bool:
    """Generate a single fixture video."""
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    if spec.generator_func is None:
        logger.error(f"No generator function for {spec.name}")
        return False

    # Override the spec's output directory
    original_get_output_path = spec.get_output_path
    spec.get_output_path = lambda: output_dir / spec.get_filename()

    logger.info(f"Generating {spec.name} fixture...")

    success = spec.generator_func(spec)

    # Restore original method
    spec.get_output_path = original_get_output_path

    if not success:
        logger.error(f"Failed to generate {spec.name}")
        return False

    output_path = output_dir / spec.get_filename()

    # Optional H.264 re-encode
    if reencode:
        temp_path = output_path.with_suffix(".temp.mp4")
        output_path.rename(temp_path)

        if reencode_with_h264(temp_path, output_path, spec.fps):
            temp_path.unlink()
            logger.info(f"Re-encoded with H.264: {output_path}")
        else:
            temp_path.rename(output_path)
            logger.info(f"Keeping original encode: {output_path}")

    return True


def generate_manifest(
    registry: Dict[FixtureType, FixtureSpec],
    output_dir: Path = FIXTURE_DIR
) -> None:
    """Generate a JSON manifest file with fixture metadata."""
    import json

    manifest_path = output_dir / "manifest.json"
    manifest_data = {
        "version": "1.0",
        "generated_at": str(Path(__file__).stat().st_mtime),
        "fixtures": {}
    }

    for spec in registry.values():
        fixture_path = output_dir / spec.get_filename()
        if fixture_path.exists():
            manifest_data["fixtures"][spec.name] = {
                "filename": spec.get_filename(),
                "fps": spec.fps,
                "width": spec.width,
                "height": spec.height,
                "total_frames": spec.total_frames,
                "expected_cuts": spec.expected_cuts,
                "expected_cut_count": spec.expected_cut_count,
                "description": spec.description,
                "metadata": spec.metadata,
            }

    manifest_path.write_text(json.dumps(manifest_data, indent=2))
    logger.info(f"Generated manifest: {manifest_path}")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate synthetic test video fixtures for Scene Cut Detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        "--fixture", "-f",
        type=str,
        help="Generate specific fixture (e.g., 'hard_cut', 'flash', 'dissolve')"
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Generate all fixtures"
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available fixture types"
    )
    parser.add_argument(
        "--fps",
        type=float,
        help="Override FPS for generated fixture"
    )
    parser.add_argument(
        "--width",
        type=int,
        help="Override width for generated fixture"
    )
    parser.add_argument(
        "--height",
        type=int,
        help="Override height for generated fixture"
    )
    parser.add_argument(
        "--reencode",
        action="store_true",
        help="Re-encode with H.264 using ffmpeg for better compatibility"
    )
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="Generate manifest.json with fixture metadata"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=FIXTURE_DIR,
        help="Output directory (default: tests/fixtures/scene_cut/)"
    )

    args = parser.parse_args()

    # Setup logging
    if args.verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s: %(message)s"
        )
    else:
        logging.basicConfig(
            level=logging.WARNING,
            format="%(message)s"
        )

    registry = create_fixture_registry()

    # Use specified output directory
    output_dir = args.output_dir

    # List fixtures
    if args.list:
        list_fixtures(registry, output_dir)
        return 0

    # Determine which fixtures to generate
    fixtures_to_generate: List[FixtureSpec] = []

    if args.all:
        fixtures_to_generate = list(registry.values())
    elif args.fixture:
        # Find matching fixture type
        fixture_name = args.fixture.lower().replace("-", "_")
        for spec in registry.values():
            if spec.name == fixture_name or spec.type.value == fixture_name:
                fixtures_to_generate.append(spec)
                break

        if not fixtures_to_generate:
            print(f"Error: Unknown fixture type '{args.fixture}'")
            print("Use --list to see available fixtures")
            return 1

    # Apply overrides
    for spec in fixtures_to_generate:
        if args.fps:
            spec.fps = args.fps
        if args.width:
            spec.width = args.width
        if args.height:
            spec.height = args.height

    # Generate fixtures
    if not fixtures_to_generate and not args.manifest:
        print("No fixtures specified. Use --fixture, --all, or --list")
        return 1

    success_count = 0
    for spec in fixtures_to_generate:
        if generate_fixture(spec, output_dir, args.reencode, args.verbose):
            success_count += 1

    # Generate manifest
    if args.manifest or args.all:
        generate_manifest(registry, output_dir)

    print(f"\nGenerated {success_count}/{len(fixtures_to_generate)} fixtures")
    print(f"Output directory: {output_dir}")

    return 0 if success_count == len(fixtures_to_generate) else 1


if __name__ == "__main__":
    sys.exit(main())
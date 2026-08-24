"""Ring geometry and motion smoothing.

Deliberately pure: no sockets, no clock, no subprocesses. Everything that
decides where a dot ends up lives here so it can be tested off-device, which
matters because the interesting failure modes (jitter, dots pegged to one side,
dots leaving the screen) are all arithmetic.

Sensor axis conventions, from the Steam Deck HID frame via the sensor service
(see sensor-service/inc/sdgyrodsu/sdhidframe.h and motionadapter.cpp):

  accel.x  +ve toward the Deck's RIGHT side      -> screen +X
  accel.z  +ve toward the Deck's BOTTOM edge     -> screen +Y
  accel.y  +ve out of the screen toward the user -> not in the screen plane

  So the in-screen component of gravity is (accel.x, accel.z), which is what
  tilt is read from. accel.y is ignored on purpose.

The gyro field NAMES from the sensor service are misleading and we keep them
only because they are the upstream wire format:

  gyro.pitch  rotation about the left-right axis  -> genuinely pitch (nodding),
                                                     shows up as vertical motion
  gyro.roll   rotation about the top-bottom axis  -> physically YAW (panning),
                                                     shows up as horizontal motion
  gyro.yaw    rotation about the screen normal    -> physically ROLL (twisting),
                                                     in-plane rotation, unused here
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from config import Settings
from protocol import Dot


def ring_anchors(width: int, height: int, count: int, margin: float) -> List[Tuple[float, float]]:
    """Evenly spaced points around an inset rectangle perimeter, clockwise from
    the top-left corner (PRD 6.1).

    Walking the perimeter by arc length rather than placing a fixed number per
    edge keeps the spacing even across the corners, which matters on a 16:10
    screen where the edges are quite different lengths.
    """
    if count <= 0:
        return []

    inner_w = max(0.0, width - 2.0 * margin)
    inner_h = max(0.0, height - 2.0 * margin)
    perimeter = 2.0 * (inner_w + inner_h)

    if perimeter <= 0.0:
        # Degenerate (margin swallowed the screen): stack them all at the centre
        # rather than dividing by zero.
        return [(width / 2.0, height / 2.0)] * count

    anchors: List[Tuple[float, float]] = []
    for i in range(count):
        d = (i / count) * perimeter
        if d < inner_w:                      # top edge, left -> right
            x, y = margin + d, margin
        elif d < inner_w + inner_h:          # right edge, top -> bottom
            x, y = margin + inner_w, margin + (d - inner_w)
        elif d < 2.0 * inner_w + inner_h:    # bottom edge, right -> left
            x, y = margin + inner_w - (d - inner_w - inner_h), margin + inner_h
        else:                                # left edge, bottom -> top
            x, y = margin, margin + inner_h - (d - 2.0 * inner_w - inner_h)
        anchors.append((x, y))
    return anchors


class MotionFilter:
    """Turns a stream of raw IMU samples into a single smoothed screen-space
    offset vector.

    PRD 7.3 makes smoothing a hard requirement, not polish: raw Deck IMU data is
    visibly noisy even sitting on a table, and unfiltered it would make the dots
    buzz.

    Two filters run on the tilt signal:

      * a fast EMA, tracking where gravity is right now;
      * a very slow EMA, tracking the posture the Deck is being *held* in.

    The offset is driven by the difference. Without the slow baseline, holding
    the Deck at its natural resting angle would put a large constant term into
    the tilt vector and park every dot against the clamp, so the ring would look
    permanently shoved to one side and would stop responding. With it, the ring
    re-centres on whatever posture you settle into and reacts to *changes*.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.tilt_x = 0.0
        self.tilt_y = 0.0
        self.baseline_x = 0.0
        self.baseline_y = 0.0
        self.gyro_x = 0.0
        self.gyro_y = 0.0
        self._primed = False

    def reset(self) -> None:
        self.__init__(self.settings)

    def update(self, sample: dict) -> None:
        """Feed one decoded sensor JSON object.

        Missing or malformed fields are treated as "no new information" rather
        than as zeros: a dropped field should not yank the dots to the centre.
        """
        accel = sample.get("accel") or {}
        gyro = sample.get("gyro") or {}

        raw_tilt_x = _as_float(accel.get("x"))
        raw_tilt_y = _as_float(accel.get("z"))
        # See the module docstring: the service's "roll" is physically yaw.
        raw_gyro_x = _as_float(gyro.get("roll"))
        raw_gyro_y = _as_float(gyro.get("pitch"))

        if raw_tilt_x is None or raw_tilt_y is None:
            return

        if not self._primed:
            # Seed both filters from the first real sample so the ring does not
            # sweep in from (0,0) when the overlay is switched on.
            self.tilt_x = self.baseline_x = raw_tilt_x
            self.tilt_y = self.baseline_y = raw_tilt_y
            self._primed = True

        s = self.settings
        self.tilt_x = _ema(self.tilt_x, raw_tilt_x, s.tilt_ema_alpha)
        self.tilt_y = _ema(self.tilt_y, raw_tilt_y, s.tilt_ema_alpha)
        self.baseline_x = _ema(self.baseline_x, raw_tilt_x, s.tilt_baseline_ema_alpha)
        self.baseline_y = _ema(self.baseline_y, raw_tilt_y, s.tilt_baseline_ema_alpha)

        if raw_gyro_x is not None:
            self.gyro_x = _ema(self.gyro_x, raw_gyro_x, s.gyro_ema_alpha)
        if raw_gyro_y is not None:
            self.gyro_y = _ema(self.gyro_y, raw_gyro_y, s.gyro_ema_alpha)

    def offset(self) -> Tuple[float, float]:
        """Current screen-space dot offset in pixels, clamped by magnitude."""
        s = self.settings
        if not self._primed:
            return 0.0, 0.0

        tilt_dx = (self.tilt_x - self.baseline_x) * s.tilt_gain_px * s.tilt_sign_x
        tilt_dy = (self.tilt_y - self.baseline_y) * s.tilt_gain_px * s.tilt_sign_y

        gyro_dx = self.gyro_x * s.gyro_gain_px * s.gyro_sign_x
        gyro_dy = self.gyro_y * s.gyro_gain_px * s.gyro_sign_y

        dx = (tilt_dx + gyro_dx) * s.sensitivity
        dy = (tilt_dy + gyro_dy) * s.sensitivity

        # Clamp by magnitude, not per-axis, so a diagonal tilt is not allowed a
        # longer excursion than a straight one (and so the clamp does not bend
        # the offset away from the true tilt direction).
        magnitude = math.hypot(dx, dy)
        if magnitude > s.max_offset_px > 0.0:
            scale = s.max_offset_px / magnitude
            dx *= scale
            dy *= scale
        return dx, dy

    def intensity(self) -> float:
        """0..1 measure of how much the ring is currently moving, for opacity."""
        s = self.settings
        if s.max_offset_px <= 0.0:
            return 0.0
        dx, dy = self.offset()
        return min(1.0, math.hypot(dx, dy) / s.max_offset_px)


def build_frame(
    anchors: Sequence[Tuple[float, float]],
    offset: Tuple[float, float],
    intensity: float,
    elapsed: float,
    settings: Settings,
    width: int,
    height: int,
) -> List[Dot]:
    """Place every dot for one output frame.

    The whole ring shifts by the same offset vector (PRD 6.1: "each dot's
    position shifts by an offset vector derived from current tilt/accel"), plus
    a per-dot sub-pixel idle drift so a resting Deck still looks alive rather
    than frozen.
    """
    s = settings
    off_x, off_y = offset
    alpha = _clamp01(s.base_alpha + s.motion_alpha_boost * _clamp01(intensity))

    # Keep dots fully on screen: a dot is drawn as a disc of radius r about its
    # centre, so the centre has to stay at least r inside every edge.
    r = s.dot_radius_px
    min_x, max_x = r, max(r, width - r)
    min_y, max_y = r, max(r, height - r)

    dots: List[Dot] = []
    count = max(1, len(anchors))
    for i, (ax, ay) in enumerate(anchors):
        # Per-dot phase so the drift does not move as one block.
        phase = 2.0 * math.pi * (i / count)
        wave = 2.0 * math.pi * s.idle_drift_hz * elapsed
        drift_x = s.idle_drift_px * math.sin(wave + phase)
        drift_y = s.idle_drift_px * math.cos(wave + phase * 1.7)

        x = min(max_x, max(min_x, ax + off_x + drift_x))
        y = min(max_y, max(min_y, ay + off_y + drift_y))
        dots.append(Dot(x, y, alpha))
    return dots


def _ema(previous: float, sample: float, alpha: float) -> float:
    return previous + alpha * (sample - previous)


def _as_float(value) -> "float | None":
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))

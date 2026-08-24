"""Every tunable Motion Dots has, in one place.

PRD 6.1 and 7.3 both call for this: the dot count must not be a magic number
scattered through the code, and the smoothing constant must be tunable because
the right value can only really be found on the device (PRD build phase 6).

Values here are the defaults. `load()` overlays anything the user has changed
via the QAM panel, read from CONFIG_PATH.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.config/motion-dots")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")

# UDP endpoints. Both loopback-only.
SENSOR_PORT = 27760   # sensor-service -> bridge (upstream SteamDeckMotion default)
OVERLAY_PORT = 27761  # bridge -> overlay renderer

# The sensor service drops clients it has not heard from in 30s, so the bridge
# has to re-register well inside that window.
SENSOR_KEEPALIVE_SECONDS = 5.0

# How often the bridge re-asks the overlay for its resolution. Cheap, and means a
# resolution change (docking, mode switch) is picked up within a couple of seconds.
OVERLAY_HELLO_SECONDS = 2.0


@dataclass
class Settings:
    """Runtime-tunable settings. Anything a person might want to change lives here."""

    # -- ring shape (PRD 6.1) ------------------------------------------------
    # PRD calls for 12-14 dots. 13 sits in the middle and, being odd, avoids
    # mirror-symmetric pairing on the top/bottom edges.
    dot_count: int = 13
    dot_radius_px: float = 4.0
    # PRD 6.1: inset 8-10px from the true screen edge.
    edge_margin_px: float = 9.0

    # -- opacity (PRD 6.1) ---------------------------------------------------
    base_alpha: float = 0.5
    # Opacity rise with motion intensity. PRD calls this a nice-to-have; set to
    # 0.0 for the flat, always-0.5 behaviour.
    motion_alpha_boost: float = 0.3

    # -- smoothing (PRD 7.3, hard requirement) -------------------------------
    # Exponential moving average coefficients, per received sensor sample
    # (~60Hz). Smaller = smoother and laggier.
    #
    # tilt_ema_alpha tracks where gravity currently is.
    tilt_ema_alpha: float = 0.12
    # tilt_baseline_ema_alpha tracks the posture the user is *holding* -- a much
    # slower filter. Dot offset is driven by the difference between the two, so
    # the ring self-centres in whatever way you happen to be holding the Deck
    # instead of being permanently shoved to one side by a 30-degree resting
    # tilt. Time constant here is on the order of several seconds.
    tilt_baseline_ema_alpha: float = 0.004
    # Gyro is already close to zero at rest, so it needs less smoothing; it
    # supplies the quick "kick" that gravity alone is too slow to show.
    gyro_ema_alpha: float = 0.25

    # -- response ------------------------------------------------------------
    # Pixels of dot shift per 1g of in-screen tilt change away from baseline.
    tilt_gain_px: float = 90.0
    # Pixels of dot shift per degree/second of rotation.
    gyro_gain_px: float = 0.35
    # Ceiling on total shift, so a hard shake cannot fling dots into the middle
    # of the screen or off it.
    max_offset_px: float = 26.0

    # User-facing sensitivity multiplier from the QAM slider. 1.0 = as tuned above.
    sensitivity: float = 1.0

    # -- idle behaviour (PRD 6.1) -------------------------------------------
    # "near-static ... allow only sub-pixel idle drift, not a hard freeze -- a
    # hard freeze reads as broken rather than idle."
    idle_drift_px: float = 0.6
    idle_drift_hz: float = 0.05

    # -- axis orientation ----------------------------------------------------
    # Which way a given tilt pushes the dots. These exist as settings rather than
    # constants because the correct signs are a question about how it *feels* in
    # the hand, which is exactly what PRD build phase 6 is for: flip a sign here
    # and restart the bridge, no rebuild.
    tilt_sign_x: float = 1.0
    tilt_sign_y: float = 1.0
    gyro_sign_x: float = 1.0
    gyro_sign_y: float = 1.0

    # -- output rate ---------------------------------------------------------
    # PRD 6.3: do not raise this without a reason. It already matches the sensor
    # service's output rate and is close to display refresh.
    output_hz: float = 60.0

    # Fallback screen size, used only until the overlay reports its real size.
    fallback_width: int = 1280
    fallback_height: int = 800

    def clamped(self) -> "Settings":
        """Return a copy with every value forced into a sane range.

        Settings arrive from a JSON file and from the QAM panel, so nothing here
        can be trusted to be in range -- and a bad value would show up as dots
        stuck off-screen rather than as an exception.
        """
        return Settings(
            dot_count=int(_clamp(self.dot_count, 1, 256)),
            dot_radius_px=_clamp(self.dot_radius_px, 0.5, 64.0),
            edge_margin_px=_clamp(self.edge_margin_px, 0.0, 400.0),
            base_alpha=_clamp(self.base_alpha, 0.0, 1.0),
            motion_alpha_boost=_clamp(self.motion_alpha_boost, 0.0, 1.0),
            tilt_ema_alpha=_clamp(self.tilt_ema_alpha, 0.001, 1.0),
            tilt_baseline_ema_alpha=_clamp(self.tilt_baseline_ema_alpha, 0.0001, 1.0),
            gyro_ema_alpha=_clamp(self.gyro_ema_alpha, 0.001, 1.0),
            tilt_gain_px=_clamp(self.tilt_gain_px, 0.0, 2000.0),
            gyro_gain_px=_clamp(self.gyro_gain_px, 0.0, 100.0),
            max_offset_px=_clamp(self.max_offset_px, 0.0, 500.0),
            sensitivity=_clamp(self.sensitivity, 0.0, 3.0),
            idle_drift_px=_clamp(self.idle_drift_px, 0.0, 20.0),
            idle_drift_hz=_clamp(self.idle_drift_hz, 0.0, 10.0),
            tilt_sign_x=_sign(self.tilt_sign_x),
            tilt_sign_y=_sign(self.tilt_sign_y),
            gyro_sign_x=_sign(self.gyro_sign_x),
            gyro_sign_y=_sign(self.gyro_sign_y),
            output_hz=_clamp(self.output_hz, 1.0, 240.0),
            fallback_width=int(_clamp(self.fallback_width, 64, 16384)),
            fallback_height=int(_clamp(self.fallback_height, 64, 16384)),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        """Build Settings from a dict, ignoring unknown keys and unusable values.

        Tolerant on purpose: a settings file written by an older or newer version
        of the plugin should degrade to defaults, not stop the overlay working.
        """
        # `from __future__ import annotations` means f.type is the *string*
        # "int", not the int type, so match on both spellings.
        int_fields = {f.name for f in fields(cls) if f.type in (int, "int")}
        known = {f.name for f in fields(cls)}
        kwargs = {}
        for key, value in (data or {}).items():
            if key not in known:
                logger.debug("ignoring unknown setting %r", key)
                continue
            try:
                kwargs[key] = int(float(value)) if key in int_fields else float(value)
            except (TypeError, ValueError):
                logger.warning("ignoring unusable value for %r: %r", key, value)
        return cls(**kwargs)


def _clamp(value: float, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    if value != value:  # NaN
        return low
    return max(low, min(high, value))


def _sign(value: float) -> float:
    try:
        return -1.0 if float(value) < 0 else 1.0
    except (TypeError, ValueError):
        return 1.0


def load(path: str = CONFIG_PATH) -> Settings:
    """Load settings from disk, falling back to defaults on any problem."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return Settings.from_dict(json.load(handle)).clamped()
    except FileNotFoundError:
        return Settings()
    except Exception:
        logger.exception("failed to read %s, using defaults", path)
        return Settings()


def save(settings: Settings, path: str = CONFIG_PATH) -> None:
    """Persist settings. Written atomically so a crash mid-write cannot leave
    a truncated file that would then fail to parse on every future load."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(settings.to_dict(), handle, indent=2)
    os.replace(tmp, path)

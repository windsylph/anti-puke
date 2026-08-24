"""The bridge: sensor service -> smoothing -> overlay renderer.

Runs as its own process (started by main.py). Keeping it out of the Decky plugin
process matters for two reasons: a 60Hz loop does not belong in Decky's shared
asyncio loop, and "stop the overlay" becomes a kill rather than a cooperative
shutdown that could hang (PRD 7.4).

It can also be run straight from a terminal, which is what makes the PRD's build
phases checkable one at a time:

    python3 sensor_bridge.py --dump-sensor      # phase 1: eyeball raw IMU output
    python3 sensor_bridge.py --dots 1           # phase 3: drive a single dot
    python3 sensor_bridge.py                    # phase 4: the full ring
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import select
import signal
import socket
import sys
import time
from typing import List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402
import protocol  # noqa: E402
import ring  # noqa: E402

logger = logging.getLogger("motion-dots.bridge")


class SensorBridge:
    def __init__(
        self,
        settings: config.Settings,
        sensor_port: int = config.SENSOR_PORT,
        overlay_port: int = config.OVERLAY_PORT,
    ):
        self.settings = settings.clamped()
        self.sensor_addr = ("127.0.0.1", sensor_port)
        self.overlay_addr = ("127.0.0.1", overlay_port)

        self.filter = ring.MotionFilter(self.settings)

        self.width = self.settings.fallback_width
        self.height = self.settings.fallback_height
        self._have_overlay_size = False
        self._anchors: List[Tuple[float, float]] = []
        self._anchor_key: Optional[tuple] = None

        self._sensor_sock: Optional[socket.socket] = None
        self._overlay_sock: Optional[socket.socket] = None

        self._running = False
        self._samples_seen = 0
        self._frames_sent = 0

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        self._sensor_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sensor_sock.setblocking(False)

        self._overlay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._overlay_sock.setblocking(False)

    def close(self) -> None:
        for sock in (self._sensor_sock, self._overlay_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._sensor_sock = None
        self._overlay_sock = None

    def stop(self) -> None:
        self._running = False

    # -- the two peers -------------------------------------------------------

    def _register_with_sensor(self) -> None:
        """The sensor service starts streaming to whoever sends it a datagram and
        forgets clients after 30s of silence, so this is both registration and
        keepalive. Payload content is irrelevant to it."""
        try:
            self._sensor_sock.sendto(b"motion-dots", self.sensor_addr)
        except OSError as exc:
            # ECONNREFUSED here just means the service is not up yet; it will be
            # retried on the next keepalive tick.
            if exc.errno not in (errno.ECONNREFUSED, errno.ENETUNREACH):
                logger.warning("sensor registration failed: %s", exc)

    def _hello_overlay(self) -> None:
        """Ask the overlay for its resolution. Re-sent periodically so a display
        mode change is picked up without restarting anything."""
        try:
            self._overlay_sock.sendto(protocol.encode_hello(), self.overlay_addr)
        except OSError as exc:
            if exc.errno not in (errno.ECONNREFUSED, errno.ENETUNREACH):
                logger.warning("overlay hello failed: %s", exc)

    def _drain_sensor(self, dump: bool) -> int:
        """Read every queued sensor sample, applying each to the filter.

        All of them are applied rather than only the newest: the EMA is defined
        per sample, so skipping samples would silently change the effective
        smoothing constant whenever the loop ran late.
        """
        applied = 0
        while True:
            try:
                payload, _ = self._sensor_sock.recvfrom(4096)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    # Someone sent to a closed port earlier; not fatal.
                    break
                logger.warning("sensor read failed: %s", exc)
                break

            try:
                sample = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.debug("dropping unparseable sensor packet (%d bytes)", len(payload))
                continue

            if dump:
                print(json.dumps(sample), flush=True)

            self.filter.update(sample)
            applied += 1
            self._samples_seen += 1
        return applied

    def _drain_overlay(self) -> None:
        """Pick up INFO replies telling us the overlay's real size."""
        while True:
            try:
                payload, _ = self._overlay_sock.recvfrom(1024)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    break
                logger.warning("overlay read failed: %s", exc)
                break

            size = protocol.decode_info(payload)
            if size is None:
                continue
            width, height = size
            if (width, height) != (self.width, self.height) or not self._have_overlay_size:
                logger.info("overlay reports %dx%d", width, height)
            self.width, self.height = width, height
            self._have_overlay_size = True

    # -- frame production ----------------------------------------------------

    def _anchors_for_current_screen(self) -> List[Tuple[float, float]]:
        key = (self.width, self.height, self.settings.dot_count, self.settings.edge_margin_px)
        if key != self._anchor_key:
            self._anchors = ring.ring_anchors(
                self.width, self.height, self.settings.dot_count, self.settings.edge_margin_px
            )
            self._anchor_key = key
        return self._anchors

    def build_frame(self, elapsed: float) -> List[protocol.Dot]:
        return ring.build_frame(
            anchors=self._anchors_for_current_screen(),
            offset=self.filter.offset(),
            intensity=self.filter.intensity(),
            elapsed=elapsed,
            settings=self.settings,
            width=self.width,
            height=self.height,
        )

    def _send_frame(self, dots: List[protocol.Dot]) -> None:
        try:
            self._overlay_sock.sendto(
                protocol.encode_dots(dots, self.settings.dot_radius_px), self.overlay_addr
            )
            self._frames_sent += 1
        except OSError as exc:
            # The overlay may not have bound its socket yet, or may have just
            # been killed. Neither is worth stopping for.
            if exc.errno not in (errno.ECONNREFUSED, errno.ENETUNREACH):
                logger.warning("overlay send failed: %s", exc)

    # -- main loop -----------------------------------------------------------

    def run(self, dump_sensor: bool = False, max_seconds: Optional[float] = None) -> None:
        if self._sensor_sock is None:
            self.open()

        self._running = True
        frame_interval = 1.0 / self.settings.output_hz

        start = time.monotonic()
        next_frame = start
        next_keepalive = start
        next_hello = start

        self._register_with_sensor()
        self._hello_overlay()

        logger.info(
            "bridge running: sensor=%s overlay=%s dots=%d rate=%.0fHz",
            self.sensor_addr, self.overlay_addr,
            self.settings.dot_count, self.settings.output_hz,
        )

        while self._running:
            now = time.monotonic()
            if max_seconds is not None and now - start >= max_seconds:
                break

            # Block until either socket has traffic or the next frame is due, so
            # the loop costs nothing while idle instead of spinning (PRD 7.1).
            timeout = max(0.0, next_frame - now)
            readable = [s for s in (self._sensor_sock, self._overlay_sock) if s is not None]
            try:
                ready, _, _ = select.select(readable, [], [], timeout)
            except (OSError, ValueError):
                break

            if self._sensor_sock in ready:
                self._drain_sensor(dump_sensor)
            if self._overlay_sock in ready:
                self._drain_overlay()

            now = time.monotonic()

            if now >= next_keepalive:
                self._register_with_sensor()
                next_keepalive = now + config.SENSOR_KEEPALIVE_SECONDS

            if now >= next_hello:
                self._hello_overlay()
                next_hello = now + config.OVERLAY_HELLO_SECONDS

            if now >= next_frame:
                # A frame goes out every tick even with no new sensor data, so
                # the idle drift keeps running and the ring never hard-freezes
                # (PRD 6.1).
                self._send_frame(self.build_frame(now - start))

                next_frame += frame_interval
                if next_frame < now:
                    # Fell behind (scheduler hiccup, suspend/resume). Re-base
                    # rather than trying to catch up with a burst of frames.
                    next_frame = now + frame_interval

        logger.info(
            "bridge stopped after %.1fs: %d samples in, %d frames out",
            time.monotonic() - start, self._samples_seen, self._frames_sent,
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Motion Dots sensor bridge")
    parser.add_argument("--sensor-port", type=int, default=config.SENSOR_PORT)
    parser.add_argument("--overlay-port", type=int, default=config.OVERLAY_PORT)
    parser.add_argument("--dots", type=int, default=None,
                        help="override dot count (use 1 for PRD build phase 3)")
    parser.add_argument("--sensitivity", type=float, default=None)
    parser.add_argument("--dump-sensor", action="store_true",
                        help="print each raw sensor sample as JSON (PRD build phase 1)")
    parser.add_argument("--seconds", type=float, default=None,
                        help="exit after this long; for testing")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--config", default=config.CONFIG_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[motion-dots.bridge] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    settings = config.load(args.config)
    if args.dots is not None:
        settings.dot_count = args.dots
    if args.sensitivity is not None:
        settings.sensitivity = args.sensitivity

    bridge = SensorBridge(settings, args.sensor_port, args.overlay_port)

    def handle_signal(_signum, _frame):
        bridge.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    bridge.open()
    try:
        bridge.run(dump_sensor=args.dump_sensor, max_seconds=args.seconds)
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

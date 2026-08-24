"""Motion Dots -- Decky Loader plugin entrypoint.

Owns the lifecycle of the three processes that make up the overlay and exposes
the QAM-facing callables. Deliberately thin: everything with real logic lives in
py_modules/ so it can be tested without Decky.

Note on layout: the PRD sketches this file as `backend/main.py`, but Decky
requires the plugin entrypoint at the plugin root, so it is here and the rest of
the backend is in py_modules/ (which Decky puts on sys.path).
"""

import asyncio
import logging
import os
import sys

try:
    import decky

    PLUGIN_DIR = decky.DECKY_PLUGIN_DIR
    logger = decky.logger
except ImportError:
    # Importable outside Decky (tests, running the pieces by hand).
    PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("motion-dots")

sys.path.insert(0, os.path.join(PLUGIN_DIR, "py_modules"))

import config  # noqa: E402
from process_manager import ManagedProcess, ProcessGroup  # noqa: E402

BIN_DIR = os.path.join(PLUGIN_DIR, "bin")
SENSOR_BIN = os.path.join(BIN_DIR, "motiondots-sensord")
OVERLAY_BIN = os.path.join(BIN_DIR, "motiondots-overlay")
BRIDGE_SCRIPT = os.path.join(PLUGIN_DIR, "py_modules", "sensor_bridge.py")

# How often we check whether a child died on its own.
WATCHDOG_INTERVAL_SECONDS = 1.0


class MotionDots:
    def __init__(self):
        self.settings = config.load()
        self.group = None
        # PRD 6.2: on/off persists across game launches within a session but is
        # not required to survive a reboot. Holding it in memory gives exactly
        # that -- the Decky plugin process outlives individual games, and a
        # reboot starts it fresh.
        self.enabled = False
        self.last_error = ""
        self._watchdog = None
        # Serialises start/stop. Without it a teardown started by the watchdog
        # and one started by the user (or by _unload) run concurrently: the
        # second sees self.group already cleared, returns immediately, and
        # _unload reports "done" while processes are still being killed on a
        # worker thread. Decky can then tear the plugin down mid-teardown and
        # leave those processes orphaned, which is exactly what PRD 7.4 forbids.
        self._lock = None

    # -- process wiring ------------------------------------------------------

    def _build_group(self) -> ProcessGroup:
        overlay_env = {
            # gamescope's Xwayland. Without this the overlay cannot open a
            # display and so cannot composite over the game.
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
        }
        return ProcessGroup([
            ManagedProcess("sensor-service", [SENSOR_BIN]),
            ManagedProcess("overlay", [OVERLAY_BIN, "--port", str(config.OVERLAY_PORT)],
                           env=overlay_env),
            ManagedProcess("bridge", [
                sys.executable, BRIDGE_SCRIPT,
                "--sensor-port", str(config.SENSOR_PORT),
                "--overlay-port", str(config.OVERLAY_PORT),
            ]),
        ])

    def _get_lock(self) -> asyncio.Lock:
        # Created lazily so the lock always binds to the loop actually running
        # the plugin, not to whatever (if anything) existed at import time.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _start(self) -> None:
        async with self._get_lock():
            if self.group is not None and self.group.any_running:
                return
            self.group = self._build_group()
            # Popen blocks briefly; keep it off the event loop so Decky's UI
            # does not hitch while three processes come up.
            await asyncio.get_event_loop().run_in_executor(None, self.group.start)
            self.last_error = ""
            logger.info("Motion Dots started")

    async def _stop(self) -> None:
        async with self._get_lock():
            if self.group is None:
                return
            group, self.group = self.group, None
            await asyncio.get_event_loop().run_in_executor(None, group.stop)
            logger.info("Motion Dots stopped")

    # -- callables exposed to the QAM panel ----------------------------------

    async def set_enabled(self, enabled: bool) -> dict:
        enabled = bool(enabled)
        try:
            if enabled:
                await self._start()
            else:
                await self._stop()
            self.enabled = enabled
        except Exception as exc:
            logger.exception("failed to %s Motion Dots", "start" if enabled else "stop")
            self.last_error = str(exc)
            # Do not leave a half-started pipeline behind a failed toggle.
            await self._stop()
            self.enabled = False
        return await self.get_state()

    async def get_state(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self.group and self.group.running),
            "sensitivity": self.settings.sensitivity,
            "dot_count": self.settings.dot_count,
            "last_error": self.last_error,
        }

    async def set_sensitivity(self, sensitivity: float) -> dict:
        self.settings.sensitivity = float(sensitivity)
        self.settings = self.settings.clamped()
        try:
            config.save(self.settings)
        except Exception:
            logger.exception("failed to persist settings")
        # The bridge reads settings once at startup, so a live change needs it
        # restarted. Only the bridge -- the sensor service and the overlay do not
        # care about sensitivity, and bouncing them would blink the overlay.
        if self.enabled and self.group is not None:
            await self._restart_bridge()
        return await self.get_state()

    async def _restart_bridge(self) -> None:
        loop = asyncio.get_event_loop()
        for proc in self.group.processes:
            if proc.name != "bridge":
                continue
            await loop.run_in_executor(None, proc.stop)
            await loop.run_in_executor(None, proc.start)
            return

    async def get_settings(self) -> dict:
        return self.settings.to_dict()

    # -- watchdog ------------------------------------------------------------

    async def _watch(self) -> None:
        """Notice a child that died on its own so the panel can stop claiming
        the overlay is up."""
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            if self.group is None:
                continue
            dead = await asyncio.get_event_loop().run_in_executor(None, self.group.dead)
            if dead:
                self.last_error = f"{', '.join(dead)} exited unexpectedly"
                logger.error(self.last_error)
                # One component down means the overlay is broken anyway; tear the
                # rest down rather than leaving processes burning battery for
                # nothing (PRD 7.4).
                await self._stop()
                self.enabled = False

    # -- Decky hooks ---------------------------------------------------------

    async def _main(self) -> None:
        logger.info("Motion Dots plugin loaded (bin dir: %s)", BIN_DIR)
        for path in (SENSOR_BIN, OVERLAY_BIN):
            if not os.path.exists(path):
                logger.warning("missing binary: %s -- run the build scripts", path)
        self._watchdog = asyncio.create_task(self._watch())

    async def _unload(self) -> None:
        # PRD 7.4: Decky unloading must not leave anything behind.
        #
        # Stop first, cancel the watchdog second. _stop() takes the lock, so if
        # the watchdog is already mid-teardown this waits for that to finish
        # rather than racing it. Cancelling first could abandon an in-flight
        # teardown and orphan whatever it had not reached yet.
        await self._stop()

        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog = None

        logger.info("Motion Dots plugin unloaded")

    async def _uninstall(self) -> None:
        await self._unload()


_plugin = MotionDots()


class Plugin:
    """The class Decky instantiates.

    Delegation is written out rather than generated reflectively. The obvious
    reflective version --

        setattr(Plugin, attr, lambda _self, _func=..., *a, **kw: _func(*a, **kw))

    -- looks fine but is broken: the captured function sits in a *positional*
    parameter, so the first real argument from the frontend overwrites it and the
    call fails with "'bool' object is not callable". Being explicit also keeps
    the plugin's public surface to exactly these methods.
    """

    async def set_enabled(self, enabled):
        return await _plugin.set_enabled(enabled)

    async def get_state(self):
        return await _plugin.get_state()

    async def set_sensitivity(self, sensitivity):
        return await _plugin.set_sensitivity(sensitivity)

    async def get_settings(self):
        return await _plugin.get_settings()

    # Decky lifecycle hooks.
    async def _main(self):
        return await _plugin._main()

    async def _unload(self):
        return await _plugin._unload()

    async def _uninstall(self):
        return await _plugin._uninstall()

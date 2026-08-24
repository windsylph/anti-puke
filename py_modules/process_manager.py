"""Subprocess lifecycle for the three Motion Dots processes.

Split out of main.py so it can be tested without Decky present. PRD 7.4 is the
whole reason this is its own module: "If the plugin is toggled off, or Decky
itself is restarted/unloaded, both the sensor-service and overlay subprocesses
must be terminated cleanly. No orphaned processes."

The escalation is SIGTERM -> wait -> SIGKILL, applied to a process *group* rather
than a bare pid, so nothing a child spawns can outlive us.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Dict, List, Optional

logger = logging.getLogger("motion-dots.procs")

# How long a child gets to exit on SIGTERM before it is killed outright.
TERM_GRACE_SECONDS = 2.0


class ManagedProcess:
    """One child process, started detached into its own process group."""

    def __init__(self, name: str, argv: List[str], env: Optional[Dict[str, str]] = None):
        self.name = name
        self.argv = argv
        self.env = env
        self.proc: Optional[subprocess.Popen] = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        if not os.path.exists(self.argv[0]):
            raise FileNotFoundError(f"{self.name}: {self.argv[0]} not found")

        environment = dict(os.environ)
        if self.env:
            environment.update(self.env)

        logger.info("starting %s: %s", self.name, " ".join(self.argv))
        self.proc = subprocess.Popen(
            self.argv,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Own process group, so stop() can signal the whole tree at once and
            # so a Ctrl-C aimed at Decky does not race us.
            start_new_session=True,
        )

    def stop(self, grace: float = TERM_GRACE_SECONDS) -> None:
        if self.proc is None:
            return

        if self.proc.poll() is None:
            logger.info("stopping %s (pid %d)", self.name, self.proc.pid)
            self._signal_group(signal.SIGTERM)

            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.05)

            if self.proc.poll() is None:
                logger.warning("%s ignored SIGTERM, sending SIGKILL", self.name)
                self._signal_group(signal.SIGKILL)
                try:
                    self.proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    logger.error("%s survived SIGKILL", self.name)

        # Always reap, so a finished child never lingers as a zombie.
        try:
            self.proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass

        self._drain_output()
        self.proc = None

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            # Already gone, or we cannot reach the group; fall back to the pid.
            try:
                self.proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def _drain_output(self) -> None:
        """Log whatever the child wrote. Also stops a full pipe buffer from
        wedging a child that logs more than we expected."""
        if self.proc is None or self.proc.stdout is None:
            return
        try:
            output = self.proc.stdout.read()
        except (ValueError, OSError):
            return
        finally:
            try:
                self.proc.stdout.close()
            except (ValueError, OSError):
                pass
        if output:
            text = output.decode("utf-8", errors="replace").strip()
            if text:
                logger.info("%s output:\n%s", self.name, text)

    def poll_exit(self) -> Optional[int]:
        """Return the exit code if the child has died on its own, else None."""
        if self.proc is None:
            return None
        code = self.proc.poll()
        if code is not None:
            self._drain_output()
            self.proc = None
        return code


class ProcessGroup:
    """Starts a set of processes in order and stops them in reverse.

    Start is all-or-nothing: if any member fails to come up, the ones already
    started are torn down before the error propagates, so a partial failure can
    never leave the sensor service running with nothing consuming it.
    """

    def __init__(self, processes: List[ManagedProcess]):
        self.processes = processes

    @property
    def running(self) -> bool:
        return bool(self.processes) and all(p.running for p in self.processes)

    @property
    def any_running(self) -> bool:
        return any(p.running for p in self.processes)

    def start(self) -> None:
        started: List[ManagedProcess] = []
        try:
            for proc in self.processes:
                proc.start()
                started.append(proc)
        except Exception:
            logger.exception("failed to start process group, rolling back")
            for proc in reversed(started):
                proc.stop()
            raise

    def stop(self) -> None:
        # Reverse order: consumers before producers, so nothing is left writing
        # into a socket whose reader has gone.
        for proc in reversed(self.processes):
            try:
                proc.stop()
            except Exception:
                logger.exception("error stopping %s", proc.name)

    def dead(self) -> List[str]:
        """Names of processes that exited on their own since the last check."""
        gone = []
        for proc in self.processes:
            code = proc.poll_exit()
            if code is not None:
                logger.error("%s exited unexpectedly with code %s", proc.name, code)
                gone.append(proc.name)
        return gone

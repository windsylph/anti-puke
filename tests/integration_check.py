"""Process-lifecycle integration check against the real binaries.

Starts the plugin for real, lets it fail (there is no Deck IMU on a dev machine,
so the sensor service exits with status 3), and asserts that _unload() leaves
nothing running.

This is the regression test for a bug where the watchdog's teardown and
_unload()'s teardown raced: the second caller saw the process group already
cleared, returned immediately, and _unload reported success while processes were
still being killed on a worker thread. Under Decky that is an orphan.

Needs a display for the overlay:
    Xvfb :99 -screen 0 1280x800x24 &
    DISPLAY=:99 LIBGL_ALWAYS_SOFTWARE=1 python3 tests/integration_check.py
"""
import asyncio, os, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import main

BIN = os.path.join(REPO, "bin") + os.sep
BRIDGE = os.path.join(REPO, "py_modules", "sensor_bridge.py")

def our_procs():
    """Live (non-zombie) processes actually EXECUTING one of our programs.

    Matches on argv[0]/argv[1] rather than substring-searching the whole command
    line, so a shell that merely mentions these paths is not counted.
    """
    found, me = [], os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
            with open(f"/proc/{entry}/stat") as f:
                st = f.read()
            if st[st.rindex(")") + 2] == "Z":
                continue
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
        argv = [a for a in argv if a]
        if not argv:
            continue
        if argv[0].startswith(BIN) or (len(argv) > 1 and argv[1] == BRIDGE):
            found.append(f"{entry}: {' '.join(argv)}")
    return found

async def go():
    p = main.Plugin()
    await p._main()
    print("baseline:", our_procs() or "none")

    await p.set_enabled(True)
    await asyncio.sleep(0.8)
    running = our_procs()
    print(f"while ON ({len(running)} of ours):")
    for x in running:
        print("   ", x)

    # The sensor service cannot open the IMU here, so it exits and the watchdog
    # tears the group down. Teardown is not instant (SIGTERM plus a grace
    # period), so processes may still be listed at this instant -- that is fine.
    # What must hold is the state after _unload() returns.
    await asyncio.sleep(2.2)
    print("mid-teardown (may still list processes):", our_procs() or "NONE")

    await p.set_enabled(False)
    await p._unload()
    leftover = our_procs()
    print("final leftover:", leftover or "NONE")
    assert not leftover, f"ORPHANS: {leftover}"
    print("\nRESULT: no orphaned processes")

asyncio.run(go())

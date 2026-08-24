# Motion Dots

A Decky Loader plugin for the Steam Deck that draws a ring of small white dots
around the edge of the screen, on top of whatever game is running. The dots
shift with the Deck's tilt and movement, driven by the built-in IMU.

This is a visual effect, not an accessibility or comfort tool. There is no
motion-sickness detection, no haptics, and no alerting.

---

## Status: not yet verified on hardware

**Everything below marked "unverified" needs a Steam Deck to confirm, and none
of it has had one.** This was developed and tested in a Linux container, so the
parts that depend on Steam Deck hardware or on gamescope are unproven.

| Area | Status |
|---|---|
| Sensor service builds and links | Verified (hidapi + pthread only) |
| Sensor service reads the Deck IMU | **Unverified** — no Deck hardware |
| Overlay builds, shaders compile, ring renders | Verified (Xvfb + Mesa llvmpipe, screenshot-checked) |
| Overlay composites over a running game via gamescope | **Unverified** — see the risk below |
| Bridge smoothing / geometry / clamping | Verified (32 unit tests) |
| Full pipeline, sensor JSON to dot packets | Verified (loopback harness, 60Hz sustained) |
| Subprocess start/stop, no orphans | Verified (including SIGTERM-ignoring and grandchild cases) |
| QAM panel builds and typechecks | Verified |
| Hotkey binding actually fires | **Unverified** — needs a controller |
| Battery/CPU cost over a multi-hour session | **Unverified** — see PRD §9 |

### The gamescope risk is still open

The whole overlay approach depends on gamescope honouring the
`GAMESCOPE_EXTERNAL_OVERLAY` window property. That is what OverLaid used, and it
is the only reason a plain Xwayland window can appear above a game. If a SteamOS
update changes or removes that behaviour, the overlay silently stops appearing
and no amount of work elsewhere in this repo helps.

**This must be checked first, before anything else is tuned**, per PRD build
phase 2. The check is one command on the Deck, with a game running:

```bash
DISPLAY=:0 ./bin/motiondots-overlay --self-test
```

`--self-test` draws a built-in animated ring with no sensor service and no
bridge involved, so it isolates exactly one question: does gamescope composite
this window over the game? If dots appear, the risky part works. If they do not,
stop and investigate gamescope before going further.

### Tested SteamOS version

PRD §7.2 asks for this to be recorded here. **No SteamOS version has been tested
yet.** When the check above is run, record the result:

```
SteamOS version:   <fill in: `cat /etc/os-release`>
gamescope version: <fill in: `gamescope --version`>
Overlay composites over a game:  yes / no
Date tested:
```

---

## Installing on the Steam Deck

There is no store listing — this installs as a local/developer plugin. Do the
build on the Deck itself: the sensor service links against hidapi and the
overlay against GLFW/GLEW/X11, and matching those to whatever the Deck's
current SteamOS image actually ships is safer than cross-building elsewhere
and hoping the versions line up.

**1. Turn on Developer Mode in Decky.** You need Decky Loader already
installed. Open the Quick Access Menu → the Decky (plug) icon → the gear/
Settings tab → toggle **Developer Mode**. This is what lets Decky load a
plugin that isn't from its public store.

**2. Switch to Desktop Mode** (Steam button → Power → Switch to Desktop) and
open a terminal (Konsole, on the taskbar).

**3. Set a password if you haven't**, so `sudo` works:
```bash
passwd
```

**4. Get the code onto the Deck.** Easiest is `git clone` if you have network
access configured for it; otherwise copy the repo over with `scp` from another
machine, or a USB drive.
```bash
git clone <this-repo-url> ~/motion-dots
cd ~/motion-dots
```

**5. Disable the read-only filesystem** so you can install build dependencies:
```bash
sudo steamos-readonly disable
```

**6. Install build dependencies.** SteamOS uses pacman. `base-devel`, `cmake`,
and `git` are usually already present (Decky's own dev environment needs
them); the rest almost certainly are not:
```bash
sudo pacman -Sy --needed cmake hidapi glfw-x11 glew libx11 libxfixes nodejs pnpm
```
If `pacman-key` complains about an uninitialized keyring (common on a fresh
SteamOS install), run `sudo pacman-key --init && sudo pacman-key --populate
archlinux` first.

**7. Re-enable the read-only filesystem** now that packages are installed —
there's no reason to leave the root filesystem writable:
```bash
sudo steamos-readonly enable
```

**8. Build both native binaries and the QAM panel:**
```bash
./build.sh
pnpm install
pnpm run build
```
`./build.sh` stages `motiondots-sensord` and `motiondots-overlay` into `bin/`;
`pnpm run build` produces `dist/index.js`. Both are gitignored, so this step
can't be skipped by just pulling the repo.

**9. Install the plugin into Decky's plugin directory.** Decky loads whatever
sits in `~/homebrew/plugins/<name>/`, so copy the built plugin there — not the
whole repo with its `node_modules` and build intermediates:
```bash
mkdir -p ~/homebrew/plugins/motion-dots
cp -r main.py plugin.json py_modules bin dist ~/homebrew/plugins/motion-dots/
chmod +x ~/homebrew/plugins/motion-dots/bin/*
```

**10. Restart Decky's plugin loader** so it picks up the new plugin: Quick
Access Menu → Decky icon → Settings → **Reload Plugins** (or restart the Deck
if that option isn't there in your Decky version).

**11. Switch back to Gaming Mode** and open the Quick Access Menu — Motion
Dots should now be listed alongside your other Decky plugins.

**12. Run the gamescope check before doing anything else.** This is the
project's one real unknown (see the status table above) and it's a single
command, with a game running:
```bash
DISPLAY=:0 ~/homebrew/plugins/motion-dots/bin/motiondots-overlay --self-test
```
If a ring of dots appears over the game, everything downstream of it — the
plugin toggle, the sensor pipeline, the tuning — is just software. If it
doesn't, don't chase the plugin further until gamescope compositing is sorted
out; see "The gamescope risk is still open" above.

**Updating later:** `git pull`, re-run steps 8–10. You don't need to redo the
readonly toggle or dependency install unless a new dependency gets added.

**Uninstalling:** turn the QAM toggle off first (so nothing is left running),
then `rm -rf ~/homebrew/plugins/motion-dots` and reload plugins again.

---

## How it works

Three processes, started and stopped together by the plugin:

```
  Deck IMU
     |  hidraw (hidapi)
     v
  sensor-service            C++, reads the IMU at 250Hz internally,
  (motiondots-sensord)      broadcasts JSON over UDP 27760 at 60Hz
     |
     |  JSON over loopback UDP
     v
  sensor_bridge.py          smooths, works out where each dot goes,
  (Python)                  sends dot positions over UDP 27761 at 60Hz
     |
     |  binary dot packets
     v
  overlay                   C++/OpenGL, an Xwayland window flagged
  (motiondots-overlay)      GAMESCOPE_EXTERNAL_OVERLAY, draws the dots
```

The split is deliberate: **all geometry and smoothing live in Python**, and the
overlay is a dumb renderer that draws whatever it is told. Tuning how the effect
looks never requires rebuilding a C++ binary.

### Why not just render in the plugin's React panel?

Because it would not work. A Decky plugin's `index.tsx` renders inside the Quick
Access Menu's CEF view, which does not appear over a running game. A real
in-game HUD has to be composited by gamescope, which is what the overlay process
is for. `src/index.tsx` here is settings only — it never draws a dot.

## Layout

```
motion-dots/
├── sensor-service/     trimmed fork of kmicki/itsOwen's Steam Deck IMU service
├── overlay/            trimmed fork of OverLaid's gamescope overlay
│   └── protocol.h      the wire format, mirrored in py_modules/protocol.py
├── py_modules/         the plugin's own backend logic
│   ├── config.py       every tunable, in one place
│   ├── ring.py         ring geometry + smoothing (pure, unit-tested)
│   ├── sensor_bridge.py  the 60Hz loop
│   ├── process_manager.py  subprocess lifecycle
│   └── protocol.py
├── src/index.tsx       QAM panel: toggle, sensitivity, hotkey
├── main.py             Decky entrypoint
└── tests/
```

`main.py` sits at the repo root rather than in `backend/` as the PRD sketched,
because Decky requires the plugin entrypoint there. The rest of the backend is
in `py_modules/`, which Decky puts on `sys.path`.

## Building

For the full on-Deck walkthrough — dependencies, `sudo steamos-readonly`,
where to copy the built plugin, enabling it in Decky — see "Installing on the
Steam Deck" above. The build itself, on any machine with the right
dependencies installed (`cmake`, a C++20 compiler, `hidapi`, `glfw3`, `GLEW`,
`X11`, optionally `XFixes`, plus `node` and `pnpm`):

```bash
./build.sh          # both native binaries -> bin/
pnpm install
pnpm run build      # the QAM panel -> dist/
```

`bin/` and `dist/` are gitignored, so this has to be run before the plugin is
copied anywhere.

## Testing

```bash
python3 tests/test_motion_dots.py     # 32 unit tests, no hardware needed
python3 tests/loopback_check.py       # full pipeline against fake peers
```

The loopback harness stands up a fake sensor service and a fake overlay, runs
the real bridge between them, and checks that a rightward tilt moves the dots
right, that no dot leaves the screen, that the resolution handshake works, and
that the ring never freezes.

## Working through the PRD's build phases

The build phases are checkable one at a time, and mostly from a terminal:

**Phase 1 — sensor service standalone.** On the Deck:
```bash
./bin/motiondots-sensord --log-level debug &
python3 py_modules/sensor_bridge.py --dump-sensor | tee /tmp/imu.log
```
Tilt the Deck and watch the values. `accel.x` should go positive tilting right,
`accel.z` positive tilting the bottom edge down.

**Phase 2 — overlay, static.** `DISPLAY=:0 ./bin/motiondots-overlay --self-test`
with a game running. **Do not go further until this works** — this is the
gamescope risk.

**Phase 3 — sensor to a single dot.**
```bash
./bin/motiondots-sensord &
DISPLAY=:0 ./bin/motiondots-overlay &
python3 py_modules/sensor_bridge.py --dots 1
```

**Phase 4 — full ring.** Drop the `--dots 1`.

**Phase 5 — toggle integration.** Install the plugin, use the QAM toggle, and
confirm with `pgrep -x motiondots-sensord` that nothing survives switching off.

**Phase 6 — tuning.** Everything worth changing is in `py_modules/config.py` or
`~/.config/motion-dots/settings.json`; the bridge reads it at startup, so a
restart of the bridge is enough. See below.

## Tuning

`~/.config/motion-dots/settings.json` overrides the defaults in
`py_modules/config.py`. The ones that matter on device:

| Setting | Default | What it does |
|---|---|---|
| `dot_count` | 13 | Dots in the ring (PRD asks for 12–14) |
| `edge_margin_px` | 9 | Inset from the screen edge |
| `tilt_ema_alpha` | 0.12 | Main smoothing. Lower = smoother and laggier |
| `tilt_baseline_ema_alpha` | 0.004 | How fast the ring re-centres on your posture |
| `tilt_gain_px` | 90 | Pixels of shift per 1g of tilt change |
| `gyro_gain_px` | 0.35 | Pixels of shift per degree/second |
| `max_offset_px` | 26 | Ceiling on total shift |
| `base_alpha` | 0.5 | Resting opacity |
| `motion_alpha_boost` | 0.3 | Extra opacity when moving; 0 disables |
| `tilt_sign_x` / `tilt_sign_y` | 1 | Flip if the dots move the wrong way |
| `gyro_sign_x` / `gyro_sign_y` | 1 | Same, for the gyro contribution |

The sign settings exist because which direction feels right is a question you
can only answer holding the thing. Flip a sign, restart the bridge, no rebuild.

### About the smoothing

Two filters run on the tilt signal: a fast one tracking where gravity is now,
and a much slower one tracking the posture you are holding the Deck in. The dots
are driven by the *difference*.

Without that second filter, holding the Deck at its natural resting angle would
feed a large constant into the tilt vector, park every dot against the clamp,
and make the ring look permanently shoved to one side and unresponsive. With it,
the ring settles wherever you are holding the Deck and reacts to changes.

The trade-off is that the baseline takes a few seconds to settle
(`1/tilt_baseline_ema_alpha` samples ≈ 4s at 60Hz), so the effect is at its most
correct shortly after you start playing rather than instantly.

## Permissions

The sensor service reads `/dev/hidraw*`, which is normally root-only, so
`plugin.json` declares `"flags": ["root"]`. If the device cannot be opened the
service exits with status 3 and says so:

```
FATAL: cannot open the Steam Deck HID device (vid 28de, pid 1205). Usually this
means insufficient permissions to read /dev/hidraw*: run the service as root, or
install a udev rule granting access.
```

PRD §9 asks whether this can work without elevated privileges. The alternative
is a udev rule granting the `deck` user access to the Steam Deck HID device;
that has not been tested here either.

## Measured cost

From the container, not from a Deck — treat as a floor, not a result:

- bridge: ~1.9% of one core, ~15 MiB RSS, sustaining 60Hz output
- sensor service and overlay: not measured under load

PRD §9 asks for real battery and CPU measurement over a multi-hour session
before calling v1 done. That is still outstanding.

## Not in v1

Auto show/hide based on detected motion, configurable dot colour/size/shape,
docked-mode or external-controller motion sources. See PRD §10.

## Credits

Built on [kmicki/SteamDeckGyroDSU](https://github.com/kmicki/SteamDeckGyroDSU)
(via [itsOwen/SteamDeckMotion](https://github.com/itsOwen/SteamDeckMotion)) and
[TheLogicMaster/OverLaid](https://github.com/TheLogicMaster/OverLaid). See
[NOTICE.md](NOTICE.md) for what was taken from each and what was changed.

MIT licensed.

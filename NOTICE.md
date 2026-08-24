# Third-party code

Motion Dots is built on two existing open-source projects rather than
reimplementing what they already solve. Both are vendored in modified form and
both retain their original licenses.

## sensor-service/

Derived from **[itsOwen/SteamDeckMotion](https://github.com/itsOwen/SteamDeckMotion)**
(vendored at commit `72ef53f`), which is itself derived from
**[kmicki/SteamDeckGyroDSU](https://github.com/kmicki/SteamDeckGyroDSU)**.

License: MIT, Copyright (c) 2022 kmicki — see
`third-party-licenses/SteamDeckMotion-kmicki-MIT.txt`.

The upstream Decky plugin that wraps this service is
[itsOwen/motioncues-decky](https://github.com/itsOwen/motioncues-decky); note
that the C++ service source lives in `SteamDeckMotion`, not in that repository
(which ships only a prebuilt binary).

Changes made:

- Removed the ncurses live-view presenter (`sdgyrodsu/presenter.*`), which drops
  the `ncurses` build dependency.
- Removed the hiddev device-discovery helper (`hiddev/hiddevfinder.*`) and the
  `shell/` command-execution helper it used, which drops the `libsystemd` build
  dependency. The Deck uses the hidapi/hidraw path.
- Replaced the ~400-line bespoke Makefile with a small CMake build.
- Replaced `main.cpp` with one that has no ncurses UI, accepts `--log-level`,
  and reports device-open failure as a clean non-zero exit rather than an
  uncaught exception (see `inc/fatal.h`).
- Dropped the systemd user unit and installer scripts: Motion Dots spawns the
  binary directly so that toggling the plugin off actually stops it.

The motion-sickness detection, haptics and alert thresholds mentioned in the
Motion Dots PRD were never in this binary — they lived in the Python plugin of
`motioncues-decky` — so there was nothing to strip out for that.

## overlay/

Derived from **[TheLogicMaster/OverLaid](https://github.com/TheLogicMaster/OverLaid)**
(vendored at commit `9fe79cd`).

License: BSD-3-Clause, Copyright (c) 2022 Justin Marentette; original
Copyright (c) 2022 Steam Deck Homebrew — see
`third-party-licenses/OverLaid-BSD-3-Clause.txt`.

What was reused is the technique that makes an in-game overlay possible at all:
create an ordinary Xwayland window and set the `GAMESCOPE_EXTERNAL_OVERLAY`
property on it, which makes gamescope composite it above the running game.

Changes made:

- Dropped Dear ImGui, `nlohmann/json` and `stb_image` entirely, along with
  OverLaid's text/image widget system and its JSON widget configuration. Motion
  Dots draws exactly one kind of thing, so circles are rendered directly with a
  small GLSL shader in a single draw call.
- Widget positions are no longer fixed at process start from a command-line JSON
  blob; dot positions stream in over UDP at ~60Hz.
- Added an empty X11 input region (XFixes) so the overlay cannot intercept input
  meant for the game.

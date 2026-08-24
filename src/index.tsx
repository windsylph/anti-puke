import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { FaCircleNotch } from "react-icons/fa6";

// -- backend calls -----------------------------------------------------------

interface PluginState {
  enabled: boolean;
  running: boolean;
  sensitivity: number;
  dot_count: number;
  last_error: string;
}

const setEnabled = callable<[enabled: boolean], PluginState>("set_enabled");
const getState = callable<[], PluginState>("get_state");
const setSensitivity = callable<[sensitivity: number], PluginState>("set_sensitivity");

// -- hotkey ------------------------------------------------------------------
//
// Decky has no hotkey API -- @decky/api exposes nothing of the sort -- so the
// PRD's "bindable hotkey" is implemented directly against Steam's controller
// input stream, which is what Decky plugins that need this actually use.
//
// Everything here is defensive: SteamClient is an undocumented surface that
// Valve changes between client builds, so a missing or renamed method must
// degrade to "no hotkey", never to a broken panel.

const GAMEPAD_BUTTON_LBACK = 32;
const GAMEPAD_BUTTON_RBACK = 33;
const GAMEPAD_BUTTON_GUIDE = 34;
const GAMEPAD_BUTTON_QUICK_ACCESS = 50;

interface HotkeyBinding {
  label: string;
  // Every button in this set must be held for the hotkey to fire.
  chord: number[];
}

const HOTKEYS: Record<string, HotkeyBinding> = {
  off: { label: "Off", chord: [] },
  l4: { label: "L4 (back, left)", chord: [GAMEPAD_BUTTON_LBACK] },
  r4: { label: "R4 (back, right)", chord: [GAMEPAD_BUTTON_RBACK] },
  steam_l4: { label: "STEAM + L4", chord: [GAMEPAD_BUTTON_GUIDE, GAMEPAD_BUTTON_LBACK] },
  qam_r4: { label: "QAM + R4", chord: [GAMEPAD_BUTTON_QUICK_ACCESS, GAMEPAD_BUTTON_RBACK] },
};

const HOTKEY_STORAGE_KEY = "motion-dots-hotkey";

function loadHotkeyChoice(): string {
  try {
    return localStorage.getItem(HOTKEY_STORAGE_KEY) ?? "off";
  } catch {
    return "off";
  }
}

function saveHotkeyChoice(choice: string): void {
  try {
    localStorage.setItem(HOTKEY_STORAGE_KEY, choice);
  } catch {
    // Non-fatal: the binding just will not survive a reload.
  }
}

/**
 * Watch the controller for a chord and call `onFire` when it completes.
 * Returns a cleanup function, or null if the input API is not available.
 */
function registerHotkey(chord: number[], onFire: () => void): (() => void) | null {
  if (chord.length === 0) return null;

  const input = (window as any)?.SteamClient?.Input;
  if (!input || typeof input.RegisterForControllerInputMessages !== "function") {
    console.warn("[Motion Dots] controller input API unavailable; hotkey disabled");
    return null;
  }

  const held = new Set<number>();
  // Latch so holding the chord toggles once rather than repeating.
  let fired = false;

  try {
    const registration = input.RegisterForControllerInputMessages(
      (_controllerIndex: number, button: number, isPressed: boolean) => {
        if (isPressed) held.add(button);
        else held.delete(button);

        const complete = chord.every((b) => held.has(b));
        if (complete && !fired) {
          fired = true;
          onFire();
        } else if (!complete) {
          fired = false;
        }
      },
    );

    return () => {
      try {
        registration?.unregister?.();
      } catch (err) {
        console.warn("[Motion Dots] failed to unregister hotkey", err);
      }
    };
  } catch (err) {
    console.warn("[Motion Dots] failed to register hotkey", err);
    return null;
  }
}

// -- panel -------------------------------------------------------------------

function Content() {
  const [state, setState] = useState<PluginState | null>(null);
  const [busy, setBusy] = useState(false);
  const [hotkey, setHotkey] = useState<string>(loadHotkeyChoice);

  // The hotkey callback must always see the current state, but re-registering
  // on every state change would drop chords mid-press. Keep the handler stable
  // and read the latest value through a ref.
  const stateRef = useRef<PluginState | null>(null);
  stateRef.current = state;

  const applyEnabled = async (next: boolean) => {
    setBusy(true);
    try {
      const result = await setEnabled(next);
      setState(result);
      if (result.last_error) {
        toaster.toast({ title: "Motion Dots", body: result.last_error });
      }
    } catch (err) {
      toaster.toast({ title: "Motion Dots", body: `Failed: ${err}` });
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    getState().then(setState).catch(() => undefined);
  }, []);

  // Poll while the panel is open so the toggle reflects reality if a
  // subprocess dies underneath us.
  useEffect(() => {
    const timer = setInterval(() => {
      getState().then(setState).catch(() => undefined);
    }, 2000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const binding = HOTKEYS[hotkey];
    if (!binding) return;
    const cleanup = registerHotkey(binding.chord, () => {
      const current = stateRef.current;
      void applyEnabled(!(current?.enabled ?? false));
    });
    return () => cleanup?.();
  }, [hotkey]);

  const enabled = state?.enabled ?? false;
  const sensitivity = state?.sensitivity ?? 1.0;

  return (
    <>
      <PanelSection title="Overlay">
        <PanelSectionRow>
          <ToggleField
            label="Motion Dots"
            description={
              state?.last_error
                ? state.last_error
                : enabled
                ? `${state?.dot_count ?? 0} dots following the Deck's motion`
                : "Show a ring of motion-reactive dots over your game"
            }
            checked={enabled}
            disabled={busy}
            onChange={(value: boolean) => void applyEnabled(value)}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Tuning">
        <PanelSectionRow>
          <SliderField
            label="Sensitivity"
            description="How far the dots move for a given tilt"
            value={sensitivity}
            min={0}
            max={2}
            step={0.05}
            showValue
            disabled={busy}
            onChange={(value: number) => {
              // Update locally first so the slider does not lag the thumb; the
              // backend call is what actually persists it.
              setState((prev) => (prev ? { ...prev, sensitivity: value } : prev));
              setSensitivity(value)
                .then(setState)
                .catch((err) =>
                  toaster.toast({ title: "Motion Dots", body: `Failed: ${err}` }),
                );
            }}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <DropdownItem
            label="Hotkey"
            description="Toggle the overlay without opening this menu"
            rgOptions={Object.entries(HOTKEYS).map(([key, binding]) => ({
              data: key,
              label: binding.label,
            }))}
            selectedOption={hotkey}
            onChange={(option: { data: string }) => {
              setHotkey(option.data);
              saveHotkeyChoice(option.data);
            }}
          />
        </PanelSectionRow>
      </PanelSection>

      {state && !state.running && state.enabled && (
        <PanelSection title="Status">
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => void applyEnabled(true)}>
              Restart overlay
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
}

export default definePlugin(() => ({
  name: "Motion Dots",
  titleView: <div>Motion Dots</div>,
  content: <Content />,
  icon: <FaCircleNotch />,
  onDismount() {
    // The backend's _unload hook is what actually stops the subprocesses; there
    // is nothing to tear down on the frontend beyond the panel itself.
  },
}));

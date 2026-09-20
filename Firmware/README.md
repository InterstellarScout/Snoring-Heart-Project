# Snoring Heart Project

CircuitPython firmware for an Adafruit QT Py ESP32 Pico (8 MB flash / 2 MB
PSRAM) controlling two independent DRV2605L haptic drivers.

## Current behavior

- Dedicated Wi-Fi AP named `Surrogate Heart`
- Fixed controller address: `http://192.168.69.1:5000/`
- Diagnostic endpoint: `http://192.168.69.1:5000/ping`
- Heart, purring, snoring, and breathing modes
- M5Stack Unit Dual Button U025 physical controls on TX/RX
- Optional SCD43 sensing on STEMMA QT with startup-silence behavior
- Directional purring with a continuous baseline and 240-1200 pulses/minute (4-20 Hz) rate
- Event-derived haptic output leases plus a hardware main-loop watchdog
- Staged dual-motor cross-chest snoring
- 60 BPM default heartbeat plus explicit and delayed persistent defaults
- One-hour configurable haptic auto-sleep
- Saved preferred mode automatically starts after successful initialization
  unless an SCD43 is detected, in which case Running starts OFF
- Optional upstream Wi-Fi while the direct AP remains enabled
- Fixed-IP and network-capability diagnostics

## Hardware wiring

| Device | SDA | SCL |
| --- | --- | --- |
| DRV2605L / Motor 1 | A0 | A1 |
| DRV2605L / Motor 2 | A2 | A3 |
| SCD43 | STEMMA QT SDA | STEMMA QT SCL |

Both drivers use address `0x5A` on separate I2C buses. Do not move them or put
the buttons on either motor bus.

| M5 U025 wire | QT Py pin | Function |
| --- | --- | --- |
| Black | GND | Common ground |
| Red | 5V | Module supply |
| White | TX (`board.TX`) | Blue / top button |
| Yellow | RX (`board.RX`) | Red / bottom button |

The passive GPIO module cannot be auto-detected reliably. Run
`diagnostics/dual_button_input_diagnostic.py` before enabling controls for a
new physical build, confirm polarity, and set
`BUTTON_POLARITY_PHYSICALLY_VERIFIED = True` only after that check.

## Installation

1. Install CircuitPython 10.x for `adafruit_qtpy_esp32_pico`.
2. Copy `circuitpython/code.py` to the root of `CIRCUITPY`.
3. Copy the contents of `circuitpython/lib/` to `CIRCUITPY/lib/`.
4. Copy `settings.example.toml` to `CIRCUITPY/settings.toml` and customize it.
5. Reboot the board and join the `Surrogate Heart` Wi-Fi network.

The AP is open when `HEART_AP_PASSWORD` is omitted. Use an 8-63 character password to enable WPA security.

## Configuration model

The QT Py owns `live_state` for the entire powered session. Browser refreshes,
reconnections, and state polling do not rebuild that state. A new device starts
in Heart mode at 60 BPM unless a saved default exists. **Save Current Settings
for Next Power-On** copies supported live values into `saved_defaults` in NVM,
including across a dead battery. After a mode change remains selected for five
minutes, the same settings are saved automatically. Upstream credentials are
stored in a separate NVM region and are never returned by the REST API.
CircuitPython NVM is device-local but not encrypted, so physical access to the
board must be treated as access to the saved Wi-Fi credential.

On the next power cycle, the firmware rebuilds live state from those defaults.
The haptic output auto-sleeps after one hour from boot or the most recent mode/
Run control change; set `HEART_AUTO_SLEEP_SECONDS` in `settings.toml` to alter
that duration, or set it to `0` to disable it.

## M5 dual-button controls

Input processing is cooperative: 40 ms debounce, a 120 ms chord window, a
350 ms double-click window, and a one-shot 3 second BOTH hold. A pre-held
button cannot generate an event until both inputs have first been released.

- Blue single: Master Power +5 percentage points
- Red single: Master Power -5 percentage points
- Blue/Red double: active mode faster/slower
- BOTH short: Heart -> Purring -> Snoring -> Breathing -> Heart
- BOTH held 3 seconds: toggle the shared `live_state.running`

Heart changes by 5 BPM, Breathing by 1 breath/minute, and Snoring changes its
complete cycle interval by 10% while preserving the spatial envelope. Purring
is retained as a separate web-selectable mode and uses its existing native
rate if a speed gesture is made while it is active. No gesture directly drives
a motor or generates a confirmation vibration.

## Upstream Wi-Fi and Internet scope

Connection Settings accepts a manual upstream SSID and password. Manual entry
is intentional because a blocking scan could stall the cooperative haptic
scheduler. Connect and upstream Internet test operations require Running OFF.
The direct `Surrogate Heart` AP is never intentionally disabled by station
configuration. Existing `CIRCUITPY_WIFI_SSID` / `CIRCUITPY_WIFI_PASSWORD`
values are recognized as bootstrap defaults; CircuitPython connects those
before `code.py` starts.

The bundled CircuitPython API exposes AP and station operation but no NAT,
NAPT, or IP-forwarding binding. The device can therefore test its own upstream
reachability, but AP-client Internet passthrough is not offered or claimed.

## Discovery

Join the `Surrogate Heart` network and use the captive portal. If the portal
does not open automatically, visit `http://192.168.69.1:5000/`. The firmware
does not advertise or use another discovery mechanism.

## Purring modulation

Purr Speed defaults to 600 pulses/minute (10 Hz) and ranges from 240-1200
pulses/minute (4-20 Hz). Soft, Normal, and Crisp are rate-relative envelope
profiles rather than fixed durations. Alternate Left holds Motor 1 at the
independent baseline while Motor 2 modulates; Alternate Right mirrors them.
Numeric rates and all other purr controls remain session-only until **Save
Defaults** is pressed. Old Slow/Normal/Fast defaults migrate to
Soft/Normal/Crisp with a 600 pulses/minute rate.

## Motor-output fail-safe

Each positive output has a scheduler-derived lease plus a 350 ms margin.
Continuously serviced Purring, Snoring, and Breathing renew a 150 ms lease. An expired
lease forces both motors off and stops Running. A 5-second hardware watchdog
resets a fully stalled main loop, while the top-level exception path attempts
to stop both motors before re-raising. The intentionally faulting development
route is absent unless `HEART_SAFETY_DEBUG` is explicitly enabled.

## Directories

- `circuitpython/`: active deployable firmware and dependencies
- `diagnostics/`: dual-motor and raw dual-button hardware diagnostics
- `legacy/`: previous firmware and deployment backups

## Privacy

`settings.toml` is intentionally ignored. Never commit real Wi-Fi credentials or private configuration.

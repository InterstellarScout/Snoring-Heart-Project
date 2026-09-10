### Surrogate Heart — V1 Project Summary
Surrogate Heart V1 is a $46.20, battery-powered, ESP32-S3 stuffed-animal haptic system that creates programmable heartbeat, purring and spatial snoring sensations using two independently controlled vibration motors, with a completely local phone-accessible web controller over its own Wi-Fi network.

Surrogate Heart V1 is a self-contained, battery-powered dual-haptic system designed to be embedded inside a stuffed animal. It creates programmable tactile simulations of a heartbeat, purring, and breathing/snoring-like vibrations, controlled wirelessly from a phone without requiring Internet access.

The finished V1 hardware cost is $46.20.

Hardware

| Component                                                                                                 | Quantity | Cost   |
| --------------------------------------------------------------------------------------------------------- | -------- | ------ |
| [Adafruit Micro-Lipo Charger for LiPoly Batt with USB Type C Jack](https://www.adafruit.com/product/4410) | 1        | $5.95  |
| [Lithium Ion Polymer Battery - 3.7v 500mAh](https://www.adafruit.com/product/1578)                        | 1        | $7.95  |
| [Adafruit QT Py S3 with 2MB PSRAM WiFi Dev Board with STEMMA QT](https://www.adafruit.com/product/5700)   | 1        | $12.50 |
| [Adafruit DRV2605L Haptic Motor Controller - STEMMA QT / Qwiic](https://www.adafruit.com/product/2305)    | 2        | $7.95  |
| [Vibrating Mini Motor Disc](https://www.adafruit.com/product/1201)                                        | 2        | $1.95  |
|                                                                                                           |          | $46.20 |

The QT Py ESP32-S3 acts as the brain, while the two independent DRV2605L controllers allow the two vibration motors to be manipulated separately. This is what enables effects to move across the stuffed animal rather than simply making the entire body buzz.

#### V1 architecture
```
                 500mAh LiPo
                      │
              Micro-Lipo Charger
                      │
                      ▼
                QT Py ESP32-S3
               Wi-Fi + Web Server
                 /           \
          A0 SDA / A1 SCL   A2 SDA / A3 SCL
               │                 │
          DRV2605L #1       DRV2605L #2
               │                 │
            Motor 1           Motor 2
```

The two DRV2605Ls occupy independent I²C buses, allowing both identical-address (0x5A) controllers to coexist without an I²C multiplexer. STEMMA QT consequently remains available for later expansion.

### Three tactile modes
#### ❤️ Heart

The stuffed animal produces a recognizable LUB-DUB rather than a simple periodic buzz.

The two motors can operate as:

Tandem — Motor 1 produces LUB followed by Motor 2 producing DUB.

Simultaneous — both motors reproduce the heartbeat.

Motor 1 only / Motor 2 only — isolates the heartbeat to one location.

LUB strength, DUB strength, LUB→DUB delay and overall Master Power can be adjusted independently.

Heart rate supports 30–240 BPM, including quick presets:

| State     |    BPM |
| --------- | -----: |
| Very Slow |     35 |
| Sleeping  |     45 |
| Relaxed   | **60** |
| Resting   |     72 |
| Alert     |     90 |
| Walking   |    110 |
| Active    |    130 |
| Running   |    160 |
| Sprinting |    190 |
| Extreme   |    220 |

Custom BPM values can be selected anywhere within the range.

At very slow rates, the heartbeat itself remains short and natural while the silence between beats increases. At very high rates, timing is constrained to prevent one LUB-DUB cycle from colliding with the next.

#### 🐈 Purring

Purring turns the motors into a continuous-feeling rhythmic tactile effect rather than discrete heartbeats.

It has adjustable:

intensity
continuous Purr Speed from 240-1200 pulses/minute (4-20 Hz)
Soft / Normal / Crisp play modes
Alternate Left / Alternate Right / Both / Motor 1 / Motor 2 routing
0-100% baseline power for the directional alternate modes (30% default)
Master Power

In Alternate Left, Motor 1 maintains the continuous baseline while Motor 2 pulses. Alternate Right mirrors that behavior. Baseline and pulse intensity are both scaled through Master Power. Changing a running baseline updates the already-active continuous output without injecting a new pulse.

The implementation is non-blocking, so the web interface and other device functions continue operating while it purrs. The default is 600 pulses/minute (10 Hz). Soft uses 80% high duty, 35% modulation depth, and gentle transitions; Normal uses 60% duty, 60% depth, and moderate transitions; Crisp uses 37.5% duty, 85% depth, and sharp transitions. These proportional envelopes scale with the chosen rate instead of using fixed pulse lengths. The low phase retains some motor output, allowing the coin ERM motor to modulate without repeatedly attempting a complete mechanical stop.

#### 💤 Snoring

Snoring produces a slower rise-and-fall vibration with configurable strength, duration/taper and motor routing.

It supports Motor 1, Motor 2, Both, and a special Alternating mode.

Alternating is particularly interesting because it doesn't simply switch motors between snores:

```
Motor 1
████████████████▓▓▒░
            ░▒▓████████████████
                         Motor 2
```

One motor rises and remains at full output while the second gradually joins it. Only after the following motor reaches full output does the lead motor taper to zero; the following motor then performs its own taper. This avoids a weak middle where both motors are only partially powered.

The following snore travels in the opposite direction.

Placed on opposite sides of the stuffed animal's chest, this creates the impression of a vibration moving across its body.

### 3D printing the enclosure

The printable enclosure files are in [`3D Print Files/`](3D%20Print%20Files/):

- [`Snoring Heart - Case.stl`](3D%20Print%20Files/Snoring%20Heart%20-%20Case.stl)
- [`Snoring Heart - Case-V2.stl`](3D%20Print%20Files/Snoring%20Heart%20-%20Case-V2.stl)

Choose the case revision you want to use, import its STL file into your slicer, and print it in flexible TPU. The project enclosure was printed with **Bambu TPU 95A HF Gray** so that the electronics can sit inside the animal in an enclosure that remains as soft and squishy as possible.

Material specifications and printing temperature:

- Filament: Bambu TPU 95A HF Gray
- Diameter tolerance: 0.03 mm
- Spool length: 341 m
- Printing temperature: 220-240 degrees C

In the slicer, select the printer's Bambu TPU 95A HF profile (or the closest TPU 95A profile available), set the nozzle temperature within the 220-240 degrees C range, and slice the selected STL. To preserve flexibility, use a low-density infill and avoid adding more walls or solid infill than the enclosure needs. Exact speed, bed temperature, supports, and adhesion settings depend on the printer and slicer; begin with the filament manufacturer's TPU profile. After printing, remove any supports and check that the enclosure has no sharp edges before placing it and the electronics inside the animal.

### Software installation

#### Adafruit QT Py ESP32 Pico installation (8 MB flash / 2 MB PSRAM)

Use this section for the **Adafruit QT Py ESP32 Pico - WiFi Dev Board with
STEMMA QT - 8 MB Flash / 2 MB PSRAM**. Despite the similar name and form
factor, it is not the QT Py ESP32-S3 used in the original build. The ESP32
Pico does not have native USB: it is programmed through its USB-to-serial
converter and does not mount `QTPYS3BOOT` or a USB `CIRCUITPY` drive. Do not
flash the repository's ESP32-S3 `.uf2` file onto this board.

##### Install CircuitPython firmware (already completed on the connected board)

**The currently connected QT Py ESP32 Pico has already completed this firmware
installation. Do not erase or reflash it for a normal project update; continue
with "Deploy the Surrogate Heart files" below.** These steps are retained for a
new board or firmware recovery.

1. Open the official
   [Adafruit QT Py ESP32 Pico CircuitPython page](https://circuitpython.org/board/adafruit_qtpy_esp32_pico/)
   and download the stable CircuitPython 10.x English **`.bin`** firmware for
   that exact board. Do not download an ESP32-S3 `.uf2`. The bundled `.mpy`
   libraries target CircuitPython major version 10; if a later major version
   becomes current, select the newest 10.x build under **Previous Versions** or
   replace the bundled libraries with ones from the matching major-version
   [Adafruit CircuitPython Library Bundle](https://circuitpython.org/libraries).
2. Connect the board with a data-capable USB-C cable. CircuitPython must be
   installed over its serial/COM connection because this ESP32 has no native
   USB mass-storage support.
3. Put the board in the ROM bootloader by holding **BOOT**, pressing and
   releasing **RESET**, and then releasing **BOOT**.
4. Flash the `.bin` at address `0x0` using either official method:

   - **Web Serial (easiest):** open Adafruit's
     [WebSerial ESPTool](https://adafruit.github.io/Adafruit_WebSerial_ESPTool/)
     in a supported browser, select **Connect**, choose the board's serial/COM
     port, and confirm that it connects. For a clean installation, select
     **Erase** first; this deletes all existing firmware and project files.
     Choose the downloaded `.bin`, leave its offset at `0x0`, select
     **Program**, wait for completion, and press **RESET**.
   - **Command line:** install `esptool`, replace `COMx` and the filename with
     the values on the installing computer, then run:

         python -m pip install --upgrade esptool
         python -m esptool --port COMx erase-flash
         python -m esptool --port COMx write-flash 0x0 adafruit-circuitpython-adafruit_qtpy_esp32_pico-en_US-VERSION.bin

     The erase command permanently removes the board's existing project files,
     so back them up first when recovering a used board.
5. Press **RESET** or power-cycle the board. No disk drive will appear. Open
   the board's serial/COM port at 115200 baud and confirm that the CircuitPython
   banner identifies the Adafruit QT Py ESP32 Pico.

The official
[CircuitPython on ESP32 installation guide](https://learn.adafruit.com/circuitpython-with-esp32-quick-start/installing-circuitpython)
contains the full Web Serial and command-line walkthroughs.

##### Deploy the Surrogate Heart files

Because this board cannot expose a USB `CIRCUITPY` drive, transfer the project
through its serial connection. The simplest documented route is Thonny 4.0 or
newer:

1. Open **Tools > Options > Interpreter** in Thonny, select
   **CircuitPython (generic)**, and select the QT Py's serial/COM port.
2. Stop the running program if necessary and open Thonny's device file browser.
3. Upload `Firmware/circuitpython/code.py` as `/code.py` on the CircuitPython
   device.
4. Upload the complete `Firmware/circuitpython/lib/` folder as `/lib/`, keeping
   all included files and subdirectories. Do not upload `Firmware/legacy/`,
   `Firmware/diagnostics/`, `Firmware/tests/`, or `__pycache__/`.
5. A `settings.toml` file is optional. Leave it absent to use the default open
   `Surrogate Heart` access point, or upload a customized copy of
   `Firmware/settings.example.toml` as `/settings.toml`.
6. Press **RESET**, monitor the 115200-baud serial output for a traceback, and
   then continue with **4. Reboot and connect** and **5. Verify the
   installation** below.

CircuitPython's optional
[Web Workflow](https://learn.adafruit.com/circuitpython-with-esp32-quick-start/setting-up-web-workflow)
can also upload files through a browser, but it first requires 2.4 GHz local
Wi-Fi credentials and `CIRCUITPY_WEB_API_PASSWORD` in `settings.toml`. Thonny
avoids requiring a home network and preserves this project's direct-access-point
setup.

#### Recorded QT Py ESP32-S3 new-board installation (August 27, 2026)

The QT Py began with only the UF2 bootloader mounted:

| Item | Observed value |
| --- | --- |
| Volume | **QTPYS3BOOT** |
| Board | QT Py ESP32-S3, 4 MB flash / 2 MB PSRAM |
| Board ID | **ESP32S3-QTPy-N4R2-A** |
| TinyUF2 | **0.15.0** (June 30, 2023) |
| Existing files | Standard bootloader files and **CURRENT.UF2** only |
| Existing CircuitPython/project | None |

CURRENT.UF2 was backed up before erasing flash. Its SHA-256 was
**54EE1150399A5FEDEF9477B99024555B7141056FC89F26F35EFAF4E1FEA5A69A**.
There were no user files to preserve. All 14 tests in **Firmware/tests/**
passed before deployment.

CircuitPython 10 requires TinyUF2 0.33.0 or newer on Espressif boards with
4 MB flash. The board's 0.15.0 bootloader therefore had to be upgraded first.
This installation used:

- TinyUF2 0.35.0 combined.bin for **adafruit_qtpy_esp32s3_n4r2**,
  SHA-256 **B7213A30C06C91846CBAE960D1E19B06F1F69CF3ECE86364C9298B579E72FCF3**
- CircuitPython 10.2.1 English UF2 for
  **adafruit_qtpy_esp32s3_4mbflash_2mbpsram**,
  SHA-256 **756C1455CF4883731215B3AD002BDA3E1109BE70EC5FB77347A6051C0B6EC593**
- The complete **Firmware/circuitpython/** deployment
- The stock zero-byte settings.toml, leaving the default open
  **Surrogate Heart** AP

##### Upgrade TinyUF2 from the recorded 0.15.0 state

Skip this subsection if INFO_UF2.TXT already reports 0.33.0 or newer. This
operation erases flash, so first back up any CIRCUITPY files.

1. Confirm INFO_UF2.TXT identifies the 4 MB flash / 2 MB PSRAM board. Do not
   use an 8 MB image.
2. Download the current **combined.bin**, not **combined-ota.bin**, for
   adafruit_qtpy_esp32s3_n4r2 from the
   [official board page](https://circuitpython.org/board/adafruit_qtpy_esp32s3_4mbflash_2mbpsram/).
3. Install Espressif's flasher:

       python -m pip install --upgrade esptool

4. Unplug USB. Hold **BOOT/B0**, reconnect USB while holding it, wait two
   seconds, and release BOOT. QTPYS3BOOT should disappear. The ROM serial
   device normally reports USB VID 303A; Adafruit VID 239A is not ROM mode.
5. Find its COM port and replace COMx below:

       python -m esptool --port COMx chip-id
       python -m esptool --port COMx erase-flash
       python -m esptool --port COMx write-flash 0x0 tinyuf2-adafruit_qtpy_esp32s3_n4r2-0.35.0-combined.bin

6. Reset if necessary, slowly double-press RESET to mount QTPYS3BOOT, and
   confirm INFO_UF2.TXT now reports TinyUF2 0.33.0 or newer.

Adafruit's
[QT Py ESP32-S3 factory-reset guide](https://learn.adafruit.com/adafruit-qt-py-esp32-s3/factory-reset)
documents the same recovery process and its browser-assisted alternative.

##### Complete deployment and verification

1. Copy the matching CircuitPython 10 UF2 to QTPYS3BOOT. Wait for it to unmount
   and for **CIRCUITPY** to mount.
2. Confirm CIRCUITPY/boot_out.txt reports CircuitPython 10.2.1 and the correct
   QT Py model.
3. Copy Firmware/circuitpython/code.py to CIRCUITPY/code.py and copy the full
   contents of Firmware/circuitpython/lib/ to CIRCUITPY/lib/. Preserve the
   subdirectories; do not deploy legacy, diagnostics, tests, or __pycache__.
4. Leave settings.toml absent for the default open AP, or create it from
   settings.example.toml to customize the documented network options.
5. Reset and inspect USB serial output. Confirm HTTP startup has no traceback
   and record whether each DRV2605L is detected at address 0x5A.
6. Join **Surrogate Heart**. A no-Internet warning is expected. Confirm
   http://192.168.69.1:5000/ping returns pong and then open the controller.
7. Before final assembly, test Motor 1, Motor 2, Both, every tactile mode,
   Stop, timer, saved defaults, and power-cycle restore.

The fixed numeric address is authoritative. This firmware does not currently
advertise **surrogate-heart.local** through mDNS.

##### Observed result of the recorded installation

| Check | Result |
| --- | --- |
| TinyUF2 after upgrade | 0.35.0, correct N4R2 board ID |
| CircuitPython | 10.2.1, correct 4 MB flash / 2 MB PSRAM build |
| Deployed files | 30 expected files; zero missing or hash mismatches |
| Application boot | Completed in 1.23 seconds with no traceback |
| Motor 1 | DRV2605L detected at 0x5A on hardware I2C |
| Motor 2 | Not detected at 0x5A; firmware continued in single-motor mode |
| Initialization safety | Zero motor activations during initialization |
| Network | Open Surrogate Heart AP ready at 192.168.69.1 |
| HTTP | Main server on port 5000 and captive helper on port 80 reported ready |
| Startup behavior | Saved Heart default started through the normal scheduler |

The installing computer was not joined to the device AP, so the HTTP routes
could not be exercised from that host during deployment. The serial log
confirmed that both listeners started. Complete the phone-side ping and tactile
checks above before final assembly. Motor 2 must be powered and its A2/A3
wiring corrected before dual-motor, tandem, or cross-body behavior can work.

The repository includes a ready-to-copy CircuitPython deployment under:

```text
Firmware/
|-- settings.example.toml
`-- circuitpython/
    |-- code.py
    `-- lib/
        |-- surrogate_heart_v03.py
        |-- adafruit_drv2605.mpy
        |-- adafruit_connection_manager.mpy
        |-- adafruit_bus_device/
        |-- adafruit_httpserver/
        `-- adafruit_register/
```

#### 1. Install CircuitPython on the QT Py ESP32-S3

These drag-and-drop steps apply only to the original QT Py ESP32-S3. For the
8 MB Flash / 2 MB PSRAM QT Py ESP32 Pico, use the dedicated serial installation
and deployment section above.

1. Download CircuitPython 10.x for the **Adafruit QT Py ESP32-S3 4 MB Flash / 2 MB PSRAM** from [circuitpython.org](https://circuitpython.org/board/adafruit_qtpy_esp32s3_4mbflash_2mbpsram/).
2. Connect the QT Py to the computer with a data-capable USB-C cable.
3. Enter the UF2 bootloader by double-pressing the board's reset button. A drive named `QTPYS3BOOT` should appear.
4. Copy the downloaded CircuitPython `.uf2` file to `QTPYS3BOOT`.
5. Wait for the board to restart and mount a drive named `CIRCUITPY`.

The exported libraries match CircuitPython major version 10. If another major version is installed, replace the bundled `.mpy` libraries with versions from the matching [Adafruit CircuitPython Library Bundle](https://circuitpython.org/libraries).

#### 2. Copy the firmware to the QT Py ESP32-S3

These USB-drive copy steps are for the ESP32-S3. The QT Py ESP32 Pico has no
USB `CIRCUITPY` drive; deploy to it with Thonny as described in its dedicated
section above.

1. Open `Firmware/circuitpython/` from this repository.
2. Copy `code.py` to the root of the `CIRCUITPY` drive.
3. Copy everything inside `Firmware/circuitpython/lib/` into `CIRCUITPY/lib/`.
4. Allow files and folders with matching names to be replaced when updating an existing installation.

The resulting device layout should resemble:

```text
CIRCUITPY/
|-- code.py
|-- settings.toml        optional
`-- lib/
    |-- surrogate_heart_v03.py
    |-- adafruit_drv2605.mpy
    |-- adafruit_connection_manager.mpy
    |-- adafruit_bus_device/
    |-- adafruit_httpserver/
    `-- adafruit_register/
```

Do not copy files from `Firmware/legacy/` onto the board. They are retained only for project history.

#### 3. Configure the access point

The firmware works without a `settings.toml` file and creates an open Wi-Fi network named `Surrogate Heart` by default.

To customize or password-protect the network:

1. Copy `Firmware/settings.example.toml` to the root of `CIRCUITPY`.
2. Rename it to `settings.toml`.
3. Edit the values as needed:

```toml
HEART_AP_SSID="Surrogate Heart"
HEART_AP_PASSWORD="replace-with-a-private-password"
```

The password must contain 8-63 characters. Remove or comment out `HEART_AP_PASSWORD` to run an open development network. Never commit the real `settings.toml`; the repository's `.gitignore` excludes it.

Home-network credentials are not required. The controller does not depend on a router or Internet connection.

#### 4. Reboot and connect

1. On the ESP32-S3, safely eject `CIRCUITPY`; on either supported board, press
   the QT Py reset button.
2. Wait for the Wi-Fi network `Surrogate Heart` to appear.
3. Join it from the phone. A `No Internet Connection` warning is expected; remain connected.
4. If a captive landing page appears, press **Open Controller**.
5. Otherwise open this address in a browser:

```text
http://192.168.69.1:5000/
```

This fixed address is the authoritative fallback. Test basic connectivity with:

```text
http://192.168.69.1:5000/ping
```

A working diagnostic displays `pong`.

After successful hardware and network initialization, the firmware loads the explicitly saved defaults and starts their preferred mode through its normal scheduler. Factory defaults select Heart mode; use **Save Current Settings as Defaults** to change future power-on behavior.

#### 5. Verify the installation

Confirm the following before placing the electronics inside the stuffed animal:

- Motor 1 test activates only Motor 1.
- Motor 2 test activates only Motor 2.
- Both-motors test activates both motors.
- Tandem Heart produces Motor 1 LUB followed by Motor 2 DUB.
- Purring and Snoring remain responsive while the web page is open.
- Alternating Snoring brings the following motor fully up before fading the lead motor, then reverses direction on the next snore.
- Refreshing or reconnecting the browser does not inject extra haptic events.

The optional `Firmware/diagnostics/dual_motor_diagnostic.py` can be temporarily copied to `CIRCUITPY/code.py` for isolated wiring tests. Restore the normal `Firmware/circuitpython/code.py` afterward.

#### Troubleshooting

- **No `CIRCUITPY` drive on the QT Py ESP32 Pico:** this is expected because
  the board has no native USB. Use its serial/COM port and Thonny; do not try to
  install an ESP32-S3 UF2.
- **No `CIRCUITPY` drive on the QT Py ESP32-S3:** use a known data-capable USB
  cable and reinstall the correct UF2.
- **Import error:** verify every bundled library was copied into `CIRCUITPY/lib/` without adding an extra nested `lib` directory.
- **AP does not appear:** open the CircuitPython serial console and inspect the `[WIFI]` startup messages.
- **`/ping` fails:** verify the phone is still connected to `Surrogate Heart` and use the numeric IP address.
- **One motor is missing:** check 3V, common ground, and the correct SDA/SCL pair, then review the `[HAPTIC]` serial diagnostics.
- **Need the serial console:** use Mu Editor in CircuitPython mode, or another 115200-baud serial terminal, and press `Ctrl+D` to reload.

### Phone control
No app installation is required for V1.

The QT Py creates its own dedicated Wi-Fi network:

**SSID**: Surrogate Heart

The planned/final V1 network configuration is:

```
Surrogate Heart
      │
      ├── 192.168.69.1
      │
      └── Web controller :5000
```

The phone connects directly to the stuffed animal. No home router, Internet connection, account, cloud service, or external server is required.

The controller provides the Heart, Purring and Snoring panels and exposes only the settings relevant to the currently selected mode.

There is also a lightweight /ping health check for distinguishing basic network connectivity from problems with the larger application.

#### Discovery

V1 intentionally uses two access methods:

```
Captive Portal
Automatically presents controls after joining Wi-Fi
        ↓ fallback
http://192.168.69.1:5000/
Fixed direct address
```

The captive portal uses the usual captive-network concept: connectivity probes and, where practical, a lightweight port-80/DNS helper direct the phone toward the controller.

The fixed IP remains available regardless of whether a particular phone cooperates with captive-portal discovery. No additional discovery mechanism is advertised.

### Power-on behavior
V1 is intended to behave like an object rather than a computer that needs configuring every time it starts.

After power is applied:
```
Power ON
   │
   ├── Initialize QT Py
   ├── Detect Motor 1
   ├── Detect Motor 2
   ├── Load configuration
   ├── Start Surrogate Heart Wi-Fi
   ├── Start web controller
   ├── Start captive portal
   │
   ▼
Startup complete
   │
   ▼
SAVED PREFERRED MODE
Saved controls restored
Running
```

There are no motor activations during initialization.

Once initialization completes, the saved preferred mode enters Running ON through its normal scheduler. The factory configuration starts Heart mode, but an explicit **Save Defaults** operation can make Heart, Purring, or Snoring the preferred startup mode and preserve all supported controls.

Ordinary UI changes update only the powered-session `live_state`. Page reloads, polling, reconnects, and mode switches do not replace it. **Save Defaults** is the only UI action that copies the current configuration to persistent `saved_defaults`; unsaved changes disappear after complete power loss.

Selecting another tactile mode stops the previous scheduler and both motors, preserves every mode's session settings, and starts the newly selected mode normally without injecting a test pulse.

### Remote controls
The web controller provides control over things such as:

Global
- Running / Stop
- Master Power
- operating mode
- shutdown/stop timer
- hardware status

Heart
- 30–240 BPM
- named activity presets
- LUB strength
- DUB strength
- LUB→DUB timing
- dual-motor behavior
- test heartbeat

Purring
- intensity
- baseline power
- continuous 240-1200 pulses/minute (4-20 Hz) speed, default 600 pulses/minute / 10 Hz
- rate-relative Soft / Normal / Crisp duty, depth, and transition character
- Alternate Left / Alternate Right / Both / Motor 1 / Motor 2 routing
- test purr

Snoring
- intensity
- timing/taper
- Motor 1 / Motor 2 / Both / Alternating routing
- moving cross-body crossfade
- test snore

Connection Settings
- always-on direct `Surrogate Heart` AP at `192.168.69.1`
- optional upstream/home Wi-Fi station connection
- Disabled / Connecting / Connected / Failed status
- station IP and signal strength when available
- explicit upstream reachability test
- explicit Save Defaults action

Individual Motor 1, Motor 2 and dual-motor tests are also available for installation and troubleshooting.

The upstream connection is AP+STA: the Heart may join home Wi-Fi without intentionally disabling its direct AP. A saved password is kept in a separate device-local NVM region, is never returned by the REST API, and is never displayed after submission. CircuitPython NVM is not encrypted, so physical access to the board must be treated as access to that credential. CircuitPython exposes station and access-point operation on `wifi.Radio`, but its Python API does not expose NAT/NAPT or IP forwarding. Consequently, the UI does **not** claim or offer Internet passthrough for phones attached to the Heart AP. The Heart's own upstream reachability and AP-client Internet forwarding are separate capabilities.

### REST API
The web interface sits on top of a local REST API rather than being tightly coupled to the HTML interface.

V1 includes endpoints along the lines of:

```
GET  /api/v1/state

POST /api/v1/run
POST /api/v1/master
POST /api/v1/mode

POST /api/v1/heart
POST /api/v1/purring
POST /api/v1/snoring
POST /api/v1/defaults
POST /api/v1/network

POST /api/v1/haptic/motor1/test
POST /api/v1/haptic/motor2/test
POST /api/v1/haptic/both/test

POST /api/v1/heartbeat/test
POST /api/v1/purring/test
POST /api/v1/snoring/test

GET/POST/DELETE
/api/v1/heartbeat/timer

GET /ping
```

That architecture leaves open the possibility of a native phone app, BLE controller, Home Assistant integration, physical controls, or another local device controlling the animal later without rewriting the actual haptic engine.

### An important V1 design feature

The software separates configuration from physical output.

Changing a slider doesn't itself vibrate a motor.

Opening the webpage doesn't cause a heartbeat.

Polling device state doesn't trigger a haptic event.

Connecting or disconnecting Wi-Fi doesn't restart the pattern.

Instead:

```
Phone / REST
      │
      ▼
Configuration + Commands
      │
      ▼
Central Haptic Arbiter
      │
      ▼
Heart ─ Purr ─ Snore
      │
      ▼
Motor 1 + Motor 2
```

Heart, Purring, Snoring and explicit tests therefore don't fight each other for the motors.

Settings changed during an active tactile event can also be applied to the next event, rather than suddenly altering a vibration halfway through.

That is particularly important for making the device feel organic instead of like two motors being commanded by a webpage.

### Haptic safety

Every positive motor command carries an intentional output lease derived from its scheduler deadline. Heart pulses use their exact LUB or DUB end time; continuously serviced Purring and Snoring outputs renew a short 150 ms lease. A named 350 ms safety margin allows normal scheduler and network jitter. If the scheduler does not deliberately renew or end the output before that combined deadline, the independent fail-safe forces both motors off and stops Running.

A 5-second ESP32-S3 hardware watchdog is also fed by the healthy main loop. It resets the controller if request processing or another runtime fault stalls the whole interpreter long enough that the software lease checker cannot run. On any catchable top-level exception, the firmware first attempts to command both motors off, logs the failure, and then re-raises it. The development-only fault route is registered only when `HEART_SAFETY_DEBUG` is explicitly enabled; it is absent during normal operation.

### AI-assisted disclaimer

The original idea, emotional purpose, and creative drive behind this project came entirely from its human creator. The project was subsequently developed with assistance from generative AI, which served as a tool to help realize that human vision. AI helped with parts of the research, planning, firmware, troubleshooting, and documentation, while the concept, direction, final design decisions, assembly, testing, and responsibility for safe use remain with the project's human creator. AI-generated material can contain mistakes, so anyone reproducing or modifying the project should independently review the code, wiring, battery handling, temperatures, materials, and mechanical construction before use.

That assistance helped make this project possible for our little wombat buddy. By giving her a heartbeat, purring, breathing, and snoring-like sensations, she can now offer much more realistic sensory input and a stronger physical and emotional connection to the life from which she was derived. The technology is only a means to that end: making something deeply personal feel warmer, more present, and more alive.


---


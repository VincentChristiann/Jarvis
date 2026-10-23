# Jarvis: Design Plan

## Progress
| Phase | Status |
|---|---|
| 1. Core, hub, pairing, mutual TLS, policy, audit | Done |
| 2. Boot services, certificate renewal, hub dashboard | Done |
| 2b. Everyday commands (media, volume, apps, files, remote input) + control windows | Done |
| 2c. Phone dashboard (device-bound keys, touchpad, keyboard) | Done |
| 3. Gestures + hologram (hand tracking, head-tracked 3D, cursor mode) | Done; needs tuning with real users |
| 4. Voice (offline speech recognition, strict command phrases, wake word, phone mic, spoken replies) | Done for fixed commands; Whisper path untested on real audio |
| 5. Natural language (local LLM -> catalog commands, builds on the voice parser) | Not started |
| 6. Native phone app, Windows hotspot automation, guided WireGuard remote access, hub failover | Not started |


## Goals
- One agent (`jarvisd`) installed on every device, starting at boot.
- Works with no internet. Devices link over a local network that Jarvis can create itself.
- Control any device from any device.
- Laptops and desktops: camera hand-gesture control with a floating "hologram" interface.
- All devices: typed and voice input.

## Decisions

| Topic | Decision |
|---|---|
| Core language | Python 3.11+ (FastAPI + asyncio) for the daemon |
| UI | Web app (TypeScript, Three.js), served by the hub and opened in a browser or PWA. Phones need no native app |
| Gestures | MediaPipe Hands, run in the browser (WASM, bundled, offline) on laptops and desktops only |
| Voice | Local STT (faster-whisper) and TTS (Piper) on the hub. Phones stream mic audio to the hub |
| Typed and NL input | Command box and CLI. Optional local LLM (Ollama) for natural language. Cloud models off by default |
| Topology | **Hub and spoke.** A laptop or desktop is always the hub and the first device set up. Other devices are spokes |
| Discovery | mDNS (`jarvis.local`) on any shared network, plus the hub's own hotspot |
| Transport | WebSocket over TLS. Per-device keys issued at pairing |
| Boot | systemd (Linux), launchd (macOS), Windows Service or Task Scheduler |

## Hub hotspot and QR pairing
1. Hub setup creates a Wi-Fi hotspot with a random SSID and password (`nmcli` on Linux, Mobile Hotspot API on Windows, Internet Sharing on macOS).
2. The hub shows a QR code in the standard `WIFI:T:WPA;S:<ssid>;P:<pass>;;` format. Scanning it joins the phone or device to the hotspot.
3. The hotspot runs a **captive portal**. DNS sends every request to the hub, so the device automatically opens the pairing page with no URL typing.
4. The pairing page shows a short code that is confirmed on the hub. The device then receives its signed key and is trusted from then on.
5. On later boots the hub brings the hotspot up automatically. Known devices reconnect on their own.

Limits to plan around:
- A single Wi-Fi radio often cannot be a hotspot and a client at the same time. Hub internet then comes over Ethernet, or is offline. This is fine for Jarvis, but you can't browse on the hub's Wi-Fi while hosting.
- macOS cannot start Internet Sharing from code reliably. On Mac, the hub joins an existing network or uses a USB or external Wi-Fi adapter.
- Browsers only allow mic and camera on HTTPS or localhost. The hub generates its own certificate authority, and pairing installs it on the device.
- iOS may limit background behaviour, so a phone connects while the app is open.

## The "hologram" interface
A real hologram needs special hardware. The practical version is an **AR-style 3D overlay**:
- A transparent, always-on-top window, or a full-screen browser view, with floating 3D panels (Three.js).
- Your hand skeleton from the webcam is drawn live. A pinch grabs a panel, and an open palm opens the menu.
- Panels show devices, status, media, files and commands.
- It later runs on real holographic or Pepper's-ghost displays unchanged, since it's rendered as a mirrored scene.

## Modules
```
jarvis/
  core/        daemon, config, plugin loader, event bus
  net/         mDNS, hotspot, captive portal, pairing, TLS/CA
  commands/    plugins: system, media, files, apps, device-to-device
  voice/       wake word, STT, TTS
  nlu/         optional local LLM -> commands
  ui/          web app: dashboard + 3D hologram view
  vision/      gesture classifier + gesture-to-command mapping (shared with ui)
  installers/  systemd / launchd / Windows service, one-line install scripts
  cli/         `jarvis` command
```

## Security (because this can run commands on your machines)
- Nothing is trusted until paired on the hub.
- Every command is signed per device. Spokes can be revoked from the hub.
- Commands are allow-listed per device (for example the phone can send media control but not `rm`).
- The hotspot password is random and rotatable. The pairing code expires in 2 minutes.
- No inbound traffic from the internet. Remote access, if ever wanted, goes through an opt-in VPN such as WireGuard or Tailscale.

## Phases
1. **Core, hub and pairing.** Daemon, boot service, mDNS, hotspot, QR and captive-portal pairing, and a `ping` command between devices. **Done when** a phone scans the QR and appears in the hub's device list.
2. **Command plugins and dashboard.** Run commands on any paired device from the web UI and CLI.
3. **Gestures and hologram.** Browser webcam, MediaPipe hands, 3D panels, a gesture map, and a calibration screen.
4. **Voice.** Wake word, local STT and TTS, and phone mic streaming to the hub.
5. **Natural language.** Local LLM turns typed or spoken requests into plugin commands.
6. **Installers and polish.** One-line installers, auto-update over the LAN, and a hub-failover option.

## Open items (not blocking phase 1)
- The exact list of gestures, and what each one triggers.
- Which actions each device type may perform.
- Whether another laptop should take over as hub if the hub is off.

# Jarvis

Offline-first, secure multi-device control. A laptop or desktop is the **hub**; every other device pairs to it by QR/link and is then controllable from any paired device. See `docs/PLAN.md` (roadmap) and `docs/SECURITY.md` (threat model).

## Quick start (Python 3.11+)
```
pip install -e .
jarvis hub init
jarvis hub serve                      # on the hub
jarvis hub pair --role phone          # shows a QR + link
jarvis device pair "<link>" --name my-phone --class phone   # on the new device
jarvis hub pending                    # compare the 6-digit code on both screens
jarvis hub approve <id>
jarvis device run                     # keep this running on every device
jarvis device send <device-id> ping
```
`jarvis hub dashboard` opens the web dashboard (approve devices, show a pairing QR, run commands, open control windows, camera status and kill switch, gesture actions, activity) on the hub machine.

## Everyday use
```
jarvis device allow input.* files.* app.*          # on a device: opt in to what it will accept (owner decides)
jarvis device app add browser firefox              # apps the hub may launch (only names you add)
jarvis device send <id> media.play_pause           # media.*  volume.set/step/mute  app.open  files.list/get/put
jarvis device send <id> input.move --arg dx=40 --arg dy=0   # remote mouse/keyboard (hub operator approves a 10-minute window)
```
Remote mouse/keyboard on Linux needs `xdotool` (X11); media needs `playerctl`; volume uses `wpctl`, `pactl` or `amixer`.

## Phone
`jarvis hub pair --web` (or choose "Phone browser" on the dashboard) shows a QR. First install the hub's certificate on the phone from the link shown and check its fingerprint, then scan. The phone gets a touchpad, keyboard and media/volume buttons. See `docs/SECURITY.md`.

## Gestures and hologram
```
jarvis vision install          # once, needs internet: downloads hand/face tracking + 3D library, verifies hashes
jarvis hub serve --hub-class laptop
jarvis hub dashboard           # then click "Open hologram" and "Start camera"
```
The camera only runs while you press Start (and the hub turns it off after 5 minutes of inactivity, or instantly with the dashboard button or `jarvis hub camera-off`). Video never leaves the browser. Gestures: aim with your index finger and pinch to press; swipe right/left/up/down; hold an open palm or a fist; "Cursor mode" turns your fingertip into the other computer's mouse.

## Voice
```
pip install -e ".[voice]"        # bundled offline speech model (PocketSphinx)
jarvis voice check               # which engine will be used
jarvis hub serve                 # voice shows up on the dashboard, the hologram page and the phone page
```
Hold the 🎤 button and speak, or turn on Hands-free and say "Jarvis, next track". Examples: "play", "pause", "next track", "volume up", "set volume to forty", "mute", "lock the computer" (asks you to confirm on the hub), "open firefox" (an app you added), "list devices". Add "on the laptop" to pick a device. Replies are spoken by the device's own voice. Nothing is recorded, and the log never contains what you said. For free-form speech and any device/app name, install Whisper: `pip install faster-whisper && jarvis voice install`.

Start automatically at boot/login: `jarvis service install hub` or `jarvis service install device` (systemd on Linux, Task Scheduler on Windows; `--dry-run` shows what it would do).

Other hub commands: `devices`, `revoke <id>`, `confirms` / `allow <id>` / `deny <id>` (high-risk commands), `camera-off`, `audit`.

Run tests: `pip install -e .[dev] && pytest` (the gesture engine tests also need Node).

## Status
Done: secure pairing, mutual-TLS control, role permissions with control windows, audit log, certificate auto-renewal, boot services, hub dashboard, phone dashboard, media/volume/apps/files/remote input, gesture engine, hologram view with head-tracked 3D, mDNS, Linux hotspot helper.
Voice (fixed command phrases, on the dashboard, hologram and phone) is done. Not yet: free-form natural-language commands, native phone app, Windows hotspot automation, guided remote-access (VPN) setup, tuning gestures with real users.

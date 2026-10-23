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
`jarvis hub dashboard` opens the web dashboard (approve devices, see the QR, run commands, view activity) on the hub machine.

Start automatically at boot/login: `jarvis service install hub` or `jarvis service install device` (systemd on Linux, Task Scheduler on Windows; `--dry-run` shows what it would do).

Other hub commands: `devices`, `revoke <id>`, `confirms` / `allow <id>` / `deny <id>` (high-risk commands), `audit`.

Run tests: `pip install -e .[dev] && pytest`.

## Status
Done: secure pairing, mutual-TLS control channel, role-based permissions, audit log, certificate auto-renewal, boot services (Linux/Windows), hub web dashboard, mDNS, Linux hotspot helper.
Not yet: dashboard access from phones (needs client-cert design), gestures/hologram, voice, natural language, Windows hotspot automation, more control commands (media, apps, files).

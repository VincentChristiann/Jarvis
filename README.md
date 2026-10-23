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
Other hub commands: `devices`, `revoke <id>`, `confirms` / `allow <id>` / `deny <id>` (high-risk commands), `audit`.

Run tests: `pip install -e .[dev] && pytest`.

## Status
Phase 1 done: secure pairing, mutual-TLS control channel, role-based permissions, audit log, mDNS, Linux hotspot helper. Not yet built: web UI, gestures/hologram, voice, natural language, installers/boot services, Windows hotspot, certificate auto-renewal.

# Jarvis Security Model

Jarvis can run commands on your machines, so every design choice defaults to denying access.

## Trust model
- The **hub** (a laptop or desktop) owns a private Certificate Authority (ECDSA P-256). The CA key never leaves the hub.
- Every device gets its own key pair, generated **on that device**. Private keys never travel.
- Every connection is **mutual TLS 1.3**. A device with no hub-issued certificate cannot complete a handshake.
- Device certificates carry only the `clientAuth` purpose and the hub certificate only `serverAuth`, so a stolen device cert cannot impersonate the hub.
- Nothing listens on the internet. Control and pairing ports are for the local network or hotspot only.

## Pairing (the only time a device is not yet trusted)
1. The operator runs `jarvis hub pair`. The hub creates a **single-use, 256-bit token** that expires in 120 s. Only its hash is kept.
2. The QR contains the token, the hub address, and the SHA-256 **fingerprint of the hub's pairing certificate**. The device pins it, so a rogue hotspot or Wi-Fi attacker cannot read the token or impersonate the hub.
3. The device sends its CSR and the token. The hub burns the token on first use, whether or not pairing succeeds.
4. Both screens show a **6-digit confirmation code** derived from the token and the CSR. The operator checks they match and runs `jarvis hub approve`. Without approval nothing is issued.
5. The hub issues a 90-day client certificate and a role (permission set).
6. Failures are rate limited: 5 bad tokens from one address in 10 minutes blocks that address for 10 minutes. At most 5 requests may be pending.

## Authorization (checked on the hub for every command)
- Each device has a **role** with `can_send` and `accepts` allow-lists. Anything not listed is denied.
- Every command has a **risk level**. Unknown commands are denied.
  - `low` / `medium` (media, volume, apps, files): allowed when the role and the target's own allow-list permit it.
  - `high` (`system.lock`): the operator confirms **every single use**.
  - `session` (remote mouse and keyboard, `input.*`): the operator approves **once**, which opens a **control window** of 10 minutes for that one sender, target and command family. Windows are listed on the dashboard with a "Close now" button, and are closed automatically when either device is revoked. A burst of cursor moves produces one prompt, not hundreds.
- The target device keeps its own **local allow-list** that the hub cannot override, so a compromised hub or controller still cannot make a device run something its owner never enabled.
- Commands run with no shell. Arguments are validated (types, ranges, key names from an allow-list, text without control characters) and passed as separate arguments, never built into a command line.
- **Apps** can only be launched by a name the device owner configured on that device (`jarvis device app add`).
- **Files** are confined to one share folder (`~/Jarvis-Share`): absolute paths, `..` and symlinks that point outside are refused; 4 MB limit; no overwrite unless asked.
- On a freshly paired device only `ping`, `system.info`, `notify`, `media.*` and `volume.*` are enabled. Apps, files, remote input and lock stay off until the owner runs `jarvis device allow ...` (a bare `*` is refused).
- Commands carry a timestamp and nonce. Stale or repeated ones are rejected.
- Only the connected device that was sent a command may answer it.

## Revocation
- `jarvis hub revoke <id>` marks the device revoked, closes its live connections, and rejects its certificate on reconnect. The hub checks the registry on every connection, so there is no waiting for certificate expiry.

## Auditing
- Pairing, connections, allowed and denied commands, confirmations and revocations are written to a **hash-chained** log. Editing or deleting an entry breaks the chain, and `jarvis hub audit` reports it.

## Secrets at rest
- Keys and tokens are written with owner-only permissions (`0600` files, `0700` directories). On Windows, permissions are inherited from the user profile folder, so keep Jarvis data inside it.
- The admin API listens on `127.0.0.1` only and requires a random bearer token read from a private file.

## Sensors (camera and microphone)
- Sensors are **off by default**. The hub keeps the current **mode** (`idle`, `hologram`, `gesture`, `voice`, ...) and refuses a mode whose sensor the hub's device class may not use (`policy.sensor_allowed`; phones never get the camera, a hub started as `--hub-class other` gets none).
- A mode **expires by itself after 5 minutes** unless the page keeps renewing it. Every start, stop, refusal and expiry is in the audit log.
- The hologram page shows a permanent **CAMERA ON** lamp. The camera stops on **Esc**, the Stop button, when the tab is hidden, after a minute with no hand in view, or when the hub turns the mode off (dashboard button or `jarvis hub camera-off`). Stopping releases the camera device, so the hardware light goes out.
- Only the hologram page is allowed to ask for the camera: every other page is served with `Permissions-Policy: camera=()`.
- **Video never leaves the browser.** Hand tracking runs locally in WebAssembly; only the resulting gesture (for example "swipe right") becomes a command.
- Gestures can only trigger a short allow-list of harmless commands (media, volume, ping, notify). They can never trigger lock, files, apps or remote input, and a tampered `gestures.json` is ignored.
- Hand-tracking code and models are downloaded once with `jarvis vision install` and checked against pinned hashes (npm packages by the registry's sha512 integrity value; the two model files by SHA-256 recorded when they were first fetched), then served from the hub, so nothing is loaded from the internet at runtime.

## Dashboard
- Served by the admin listener on `127.0.0.1` only, so it is reachable from the hub machine and nowhere else.
- Login is a **one-time link** from `jarvis hub dashboard` (valid 60 s, burned on any attempt). It sets an `HttpOnly`, `SameSite=Strict` session cookie valid 8 h.
- State-changing requests need a custom header (blocks cross-site forms), the `Host` header must be `127.0.0.1` or `localhost` (blocks DNS rebinding), and a strict CSP forbids inline scripts, framing and outside connections. Page content is built with `textContent`, never `innerHTML`.
- The dashboard operator is still bound by the command catalog and each device's own allow-list.
- The hub dashboard itself is never exposed to the network. Phones use the separate, much smaller phone dashboard below.

## Phone dashboard (HTTPS, port 8446)
- No passwords and nothing to phish. The phone's browser makes an **ECDSA P-256 key that it can never export** (WebCrypto, kept in IndexedDB); pairing registers only the public half, after you compare the 6-digit code and approve it on the hub. The same one-time token and rate limits as app pairing apply.
- Login is a **signed challenge**: the hub sends a random single-use nonce (60 s), the phone signs it, the hub verifies it against the registered key and starts a 1-hour `HttpOnly`, `Secure`, `SameSite=Strict` session. A captured login cannot be replayed, and five failures from one address block that address.
- A phone browser has **no certificate and cannot use the control channel**. It can only send commands the role allows, through the same checks as every other device. Revoking it ends its session immediately.
- Same hardening as the dashboard: custom header on every POST, `Origin` check, strict CSP, no inline scripts, HSTS.
- **Trust requirement (important):** a browser can only check the hub if the phone trusts the hub's certificate authority. Install it once from `https://<hub>:8446/ca.pem` and **compare its SHA-256 fingerprint with the one shown on the hub dashboard**. Without that, a malicious device on the same Wi-Fi could pose as the hub during pairing, and no code in the page could detect it. On the hub's own hotspot (random WPA2 password) this risk is small; on a shared network, do not skip the check.

## Remote access (outside the local network)
- **No port forwarding, ever.** Remote access goes only through a private overlay network (WireGuard, or Tailscale if you prefer a managed one) that joins your own devices. The hub binds to that interface in addition to the LAN.
- The overlay only provides reachability. The same mutual TLS, pairing and permission checks apply, so it adds no extra trust. Someone who got onto the overlay still needs a hub-issued certificate.
- This is configured per hub with `--advertise <overlay address>`. A guided WireGuard setup is planned for phase 6.

## Known limits
- **Not tested on real Windows or phones/tablets.** The Windows input and media code (`SendInput`) follows the Win32 API but has only been reviewed, not run. On Linux, remote input needs `xdotool` and so works on X11 only, not Wayland. Windows has no absolute volume key, so `volume.set` approximates it.
- **Gesture accuracy is unproven on real users.** The classifier is unit-tested on synthetic hands and was run through the real model on two sample photos, but its thresholds (pinch distance, swipe speed, hold time) are standard heuristics that will need tuning with your hand and lighting.
- Remote input is applied by the device the moment it arrives; there is no per-move prompt once a window is open. Keep windows short and close them from the dashboard when you are done.
- Device certificates last 90 days and renew automatically in the last 30 days (same key). The hub keeps the previous certificate valid until the new one first connects, so a crash mid-renewal cannot lock a device out. A device that stays offline past expiry must re-pair.
- Hub restarts drop outstanding pairing invitations (intentional).
- Anyone with root or administrator access to the hub can read the CA key. Protect the hub with disk encryption and a lock-screen password.
- The CA key is not yet passphrase-encrypted. Hardware-backed storage (TPM or OS keyring) is planned.
- Windows hotspot creation is not automated. See `docs/PLAN.md`.

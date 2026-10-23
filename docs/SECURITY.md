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
- Every command has a **risk level**. Unknown commands are denied. `high`-risk commands (such as `system.lock`) also need a **hub-side confirmation** from the operator.
- The target device keeps its own **local allow-list** that the hub cannot override, so a compromised hub or controller still cannot make a device run something its owner never enabled.
- Commands run with no shell. Arguments are validated and never interpolated into command lines.
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
- Sensors are **off by default** and only allowed in a matching mode (for example `gesture` for the camera). Phones can never use the camera, and each device class has its own allowed sensors. The rule is `policy.sensor_allowed`. It is enforced when the vision and voice phases arrive.

## Remote access (outside the local network)
- **No port forwarding, ever.** Remote access goes only through a private overlay network (WireGuard, or Tailscale if you prefer a managed one) that joins your own devices. The hub binds to that interface in addition to the LAN.
- The overlay only provides reachability. The same mutual TLS, pairing and permission checks apply, so it adds no extra trust. Someone who got onto the overlay still needs a hub-issued certificate.
- This is configured per hub with `--advertise <overlay address>`. A guided WireGuard setup is planned for phase 6.

## Known limits
- Certificates last 90 days and **automatic renewal is not built yet** (planned for phase 2). Until then, re-pair a device when its certificate expires.
- Hub restarts drop outstanding pairing invitations (intentional).
- Anyone with root or administrator access to the hub can read the CA key. Protect the hub with disk encryption and a lock-screen password.
- The CA key is not yet passphrase-encrypted. Hardware-backed storage (TPM or OS keyring) is planned.
- Windows hotspot creation is not automated. See `docs/PLAN.md`.

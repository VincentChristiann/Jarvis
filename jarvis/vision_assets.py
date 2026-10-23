"""Download the hand/face tracking assets once, so the hologram works fully offline afterwards.

Every file is verified against a pinned hash before it is kept. Usage: jarvis vision install
(or `jarvis vision install`).
"""
import base64
import hashlib
import io
import tarfile
import urllib.request
from pathlib import Path

DEST = Path(__file__).resolve().parent / "hub" / "web" / "vendor"

# npm tarballs: pinned by the registry's published sha512 integrity value.
NPM = {
    "mediapipe": (
        "https://registry.npmjs.org/@mediapipe/tasks-vision/-/tasks-vision-0.10.21.tgz",
        "sha512-TuhKH+credq4zLksGbYrnvJ1aLIWMc5r0UHwzxzql4BHECJwIAoBR61ZrqwGOW6ZmSBIzU1t4VtKj8hbxFaKeA==",
        {
            "package/vision_bundle.mjs": "mediapipe/vision_bundle.mjs",
            "package/wasm/vision_wasm_internal.js": "mediapipe/wasm/vision_wasm_internal.js",
            "package/wasm/vision_wasm_internal.wasm": "mediapipe/wasm/vision_wasm_internal.wasm",
            "package/wasm/vision_wasm_nosimd_internal.js": "mediapipe/wasm/vision_wasm_nosimd_internal.js",
            "package/wasm/vision_wasm_nosimd_internal.wasm": "mediapipe/wasm/vision_wasm_nosimd_internal.wasm",
        },
    ),
    "three": (
        "https://registry.npmjs.org/three/-/three-0.160.0.tgz",
        "sha512-DLU8lc0zNIPkM7rH5/e1Ks1Z8tWCGRq6g8mPowdDJpw1CFBJMU7UoJjC6PefXW7z//SSl0b2+GCw14LB+uDhng==",
        {"package/build/three.module.min.js": "three/three.module.min.js"},
    ),
}

# Model files: pinned by SHA-256.
MODELS = {
    "models/hand_landmarker.task": (
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
        "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"),
    "models/face_landmarker.task": (
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"),
}


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def check_integrity(data: bytes, integrity: str):
    algo, _, want = integrity.partition("-")
    got = base64.b64encode(hashlib.new(algo, data).digest()).decode()
    if got != want:
        raise SystemExit(f"integrity check FAILED (expected {integrity[:20]}…); not installing")


def write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main(dest: Path = DEST):
    for name, (url, integrity, wanted) in NPM.items():
        print(f"fetching {name} …")
        blob = get(url)
        check_integrity(blob, integrity)
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            for member, out in wanted.items():
                write(dest / out, tar.extractfile(member).read())
    for out, (url, sha256) in MODELS.items():
        print(f"fetching {out} …")
        data = get(url)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise SystemExit(f"hash mismatch for {out}; not installing")
        write(dest / out, data)
    print(f"done: {dest}")


def installed(dest: Path = DEST) -> bool:
    needed = [o for *_, w in NPM.values() for o in w.values()] + list(MODELS)
    return all((dest / n).exists() for n in needed)


if __name__ == "__main__":
    main()

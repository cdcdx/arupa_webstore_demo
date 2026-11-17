#!/usr/bin/env python3
"""Generate two tiny signed CRX3 packages with one temporary author key.

The private key is destroyed with the temporary directory; these fixtures are
for local functional tests, not for publishing an extension.
"""

import hashlib
import io
import json
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def archive(version):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps({
            "manifest_version": 3, "name": "逐风扩展商店 Demo", "version": version,
            "description": "本地 CRX 安装与升级功能测试"}, ensure_ascii=False))
    return output.getvalue()


def main():
    with tempfile.TemporaryDirectory(prefix="nomad-crx-demo-") as temporary:
        folder = Path(temporary)
        key = folder / "author.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        public = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
                                check=True, capture_output=True).stdout
        raw_id = hashlib.sha256(public).digest()[:16]
        extension_id = "".join(chr(ord("a") + nibble) for byte in raw_id for nibble in (byte >> 4, byte & 15))
        signed = field(1, raw_id)
        for version, directory in (("1.0", ROOT / "packages"), ("1.1", ROOT / "samples")):
            payload = archive(version)
            message = b"CRX3 SignedData\0" + struct.pack("<I", len(signed)) + signed + payload
            input_path, signature_path = folder / "input.bin", folder / "signature.bin"
            input_path.write_bytes(message)
            subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key), "-out", str(signature_path),
                            str(input_path)], check=True, capture_output=True)
            proof = field(1, public) + field(2, signature_path.read_bytes())
            header = field(2, proof) + field(10000, signed)
            directory.mkdir(exist_ok=True)
            (directory / f"nomad-demo-{version}.crx").write_bytes(b"Cr24" + struct.pack("<II", 3, len(header)) + header + payload)
        print(f"示例插件 ID: {extension_id}")
        print("初始包在 packages/，升级包在 samples/；升级时用后者替换前者。")


if __name__ == "__main__":
    main()

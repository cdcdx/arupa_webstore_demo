"""Read and validate the CRX3 files exposed by the local demo catalog.

This module deliberately performs only read-only catalog inspection. The browser's
native extension installer remains responsible for signature, permission and DNR
validation when an extension is installed.
"""

from __future__ import annotations

import hashlib
import base64
import binascii
import io
import json
import re
import struct
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_RULE_BYTES = 32 * 1024 * 1024
MAX_DIAGNOSTIC_TOTAL_BYTES = 64 * 1024 * 1024
ID_RE = re.compile(r"[a-p]{32}\Z")
MIVPN_ID = "objojdkhlcekcnhaeoebojmffolmafll"
PLATFORMS = ("desktop", "android", "ios")


@dataclass(frozen=True)
class Package:
    path: Path
    extension_id: str
    name: str
    version: str
    manifest: dict
    sha256: str
    size: int
    requirements: dict
    signed_product: str | None = None
    platforms: tuple[str, ...] = PLATFORMS

    def public(self) -> dict:
        return {
            "id": self.extension_id,
            "name": self.name,
            "version": self.version,
            "manifest": self.manifest,
            "sha256": self.sha256,
            "size": self.size,
            "file": self.path.name,
            "requirements": self.requirements,
            "signedProduct": self.signed_product,
            "platforms": list(self.platforms),
        }


def valid_extension_id(value: str) -> bool:
    return bool(ID_RE.fullmatch(value))


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            break
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
    raise ValueError("crx_header_invalid")


def _proto_bytes(data: bytes, field: int) -> bytes:
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        wire = tag & 7
        if wire == 2:
            length, pos = _varint(data, pos)
            if length > len(data) - pos:
                raise ValueError("crx_header_invalid")
            value = data[pos:pos + length]
            pos += length
            if tag >> 3 == field:
                return value
        elif wire in (0, 1, 5):
            if wire == 0:
                _, pos = _varint(data, pos)
            else:
                pos += 8 if wire == 1 else 4
            if pos > len(data):
                raise ValueError("crx_header_invalid")
        else:
            raise ValueError("crx_header_invalid")
    raise ValueError("crx_identity_missing")


def crx_id(data: bytes) -> str:
    if len(data) < 13 or data[:4] != b"Cr24" or struct.unpack_from("<I", data, 4)[0] != 3:
        raise ValueError("crx3_required")
    header_size = struct.unpack_from("<I", data, 8)[0]
    if not 0 < header_size <= MAX_HEADER_BYTES or 12 + header_size >= len(data):
        raise ValueError("crx_header_invalid")
    signed = _proto_bytes(data[12:12 + header_size], 10000)
    raw_id = _proto_bytes(signed, 1)
    if len(raw_id) != 16:
        raise ValueError("crx_identity_invalid")
    return "".join(chr(ord("a") + nibble) for byte in raw_id for nibble in (byte >> 4, byte & 15))


def version_parts(value: str) -> tuple[int, ...] | None:
    parts = value.split(".")
    if not 1 <= len(parts) <= 4 or any(not re.fullmatch(r"0|[1-9][0-9]*", p) for p in parts):
        return None
    numbers = tuple(int(p) for p in parts)
    return numbers if any(numbers) and all(number <= 65535 for number in numbers) else None


def install_id(data: bytes, manifest: dict) -> str:
    """Select the install identity, without treating metadata as signature proof.

    Signed MiVPN resources retain their fixed manifest identity inside a CRX
    signed by an independent distribution key. The browser needs the inner ID
    to select that path and still verifies both signatures before installation.
    Ordinary extensions continue using the outer CRX identity.
    """
    outer_id = crx_id(data)
    encoded = manifest.get("key")
    if not isinstance(encoded, str) or len(encoded) > MAX_MANIFEST_BYTES:
        return outer_id
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return outer_id
    identity = "".join(chr(ord("a") + int(n, 16)) for n in hashlib.sha256(key).hexdigest()[:32])
    if identity != MIVPN_ID or outer_id == MIVPN_ID:
        return outer_id
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            inventory = archive.getinfo("META-INF/mivpn-package.json")
            signature = archive.getinfo("META-INF/mivpn-package.sig")
            if inventory.file_size > MAX_MANIFEST_BYTES or signature.file_size != 64:
                raise ValueError("mivpn_signed_payload_required")
            metadata = json.loads(archive.read(inventory))
        if (not isinstance(metadata, dict) or metadata.get("schema") != 1
                or metadata.get("product") != "mivpn" or metadata.get("extensionId") != MIVPN_ID
                or metadata.get("version") != manifest.get("version")):
            raise ValueError("mivpn_signed_payload_required")
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise ValueError("mivpn_signed_payload_required") from error
    return MIVPN_ID


def extract_manifest(data: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            item = archive.getinfo("manifest.json")
            if item.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("manifest_too_large")
            manifest = json.loads(archive.read(item))
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise ValueError("package_manifest_invalid") from error
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 3:
        raise ValueError("manifest_v3_required")
    if not isinstance(manifest.get("version"), str) or not version_parts(manifest["version"]):
        raise ValueError("manifest_version_invalid")
    return manifest


def display_name(data: bytes, manifest: dict) -> str:
    name = manifest.get("name", "")
    if not isinstance(name, str):
        return "Extension"
    match = re.fullmatch(r"__MSG_([A-Za-z0-9_]+)__", name)
    locale = manifest.get("default_locale", "")
    if not match or not isinstance(locale, str) or not re.fullmatch(r"[A-Za-z_]{2,20}", locale):
        return name[:256]
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            item = archive.getinfo(f"_locales/{locale}/messages.json")
            if item.file_size > MAX_MANIFEST_BYTES:
                return name
            messages = json.loads(archive.read(item))
        for key, value in messages.items():
            if key.lower() == match.group(1).lower() and isinstance(value, dict):
                localized = value.get("message")
                if isinstance(localized, str) and localized:
                    return localized[:256]
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError, AttributeError):
        pass
    return name[:256]


def active_rule_requirements(data: bytes, manifest: dict) -> dict:
    """Report only positively observed conditions; this is not an installer verdict."""
    dnr = manifest.get("declarative_net_request")
    resources = dnr.get("rule_resources") if isinstance(dnr, dict) else None
    if not isinstance(resources, list):
        return {}
    count = 0
    scanned = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for resource in resources:
                if not isinstance(resource, dict) or resource.get("enabled") is not True:
                    continue
                path = resource.get("path")
                if (
                    not isinstance(path, str)
                    or not path
                    or path.startswith("/")
                    or "\\" in path
                    or any(part in ("", ".", "..") for part in path.split("/"))
                ):
                    return {}
                item = archive.getinfo(path)
                if (
                    item.file_size > MAX_DIAGNOSTIC_RULE_BYTES
                    or scanned + item.file_size > MAX_DIAGNOSTIC_TOTAL_BYTES
                ):
                    return {}
                with archive.open(item) as stream:
                    raw = stream.read(MAX_DIAGNOSTIC_RULE_BYTES + 1)
                if len(raw) != item.file_size:
                    return {}
                scanned += len(raw)
                rules = json.loads(raw)
                if not isinstance(rules, list):
                    return {}
                for rule in rules:
                    condition = rule.get("condition") if isinstance(rule, dict) else None
                    if isinstance(condition, dict) and any(
                        isinstance(condition.get(key), list) and condition[key]
                        for key in ("responseHeaders", "excludedResponseHeaders")
                    ):
                        count += 1
    except (KeyError, OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        return {}
    return {"responseStageRules": count} if count else {}


def read_package(path: Path) -> Package:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("package_invalid_or_too_large")
    data = path.read_bytes()
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("package_too_large")
    manifest = extract_manifest(data)
    signed_product, platforms = signed_package_metadata(data, manifest)
    return Package(
        path=path,
        extension_id=install_id(data, manifest),
        name=display_name(data, manifest),
        version=manifest["version"],
        manifest=manifest,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        requirements=active_rule_requirements(data, manifest),
        signed_product=signed_product,
        platforms=platforms,
    )


def signed_package_metadata(data: bytes, manifest: dict) -> tuple[str | None, tuple[str, ...]]:
    """Read routing hints only; the browser authenticates the publishing signature."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        stem = next((name for name in ("META-INF/nomad-package", "META-INF/mivpn-package")
                     if name + ".json" in names), None)
        if stem is None:
            return None, PLATFORMS
        try:
            inventory = archive.getinfo(stem + ".json")
            signature = archive.getinfo(stem + ".sig")
            if inventory.file_size > MAX_MANIFEST_BYTES or signature.file_size != 64:
                return None, PLATFORMS
            metadata = json.loads(archive.read(inventory))
        except (KeyError, ValueError):
            return None, PLATFORMS
        product = metadata.get("product") if isinstance(metadata, dict) else None
        if (not isinstance(product, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", product)
                or metadata.get("schema") != 1 or metadata.get("version") != manifest["version"]
                or metadata.get("extensionId") != install_id(data, manifest)):
            return None, PLATFORMS
        platforms = PLATFORMS
        files = {item.filename for item in archive.infolist() if not item.is_dir() and item.file_size > 0}
        desktop = bool(files.intersection({"engine/win-x64/mivpn-browser.dll",
            "engine/mac-arm64/mivpn-browser.dylib", "engine/mac-x64/mivpn-browser.dylib",
            "engine/linux-x64/libmivpn-browser.so"}))
        android = "engine/classes.dex" in files or any(
            name.startswith("engine/lib/") and name.endswith(".so") for name in files)
        ios = product == "mivpn-ios" or any(name.startswith("engine/ios-") for name in files)
        # Product names are metadata, not an authentication or platform boundary.
        # Recognize the legacy format and renamed products, including iOS-only
        # packages whose engine is provided by the iOS host. Engine-bearing
        # packages also work with an independently named publishing product.
        if stem == "META-INF/mivpn-package" or product == "mivpn" or product.startswith("mivpn-") or desktop or android or ios:
            platforms = tuple(platform for platform, supported in
                              (("desktop", desktop), ("android", android), ("ios", ios)) if supported)
        return product, platforms


def latest_by_id(packages: list[Package]) -> dict[str, Package]:
    """Keep legacy ID-only downloads deterministic, including equal versions."""
    chosen: dict[str, Package] = {}
    for package in packages:
        previous = chosen.get(package.extension_id)
        version = (version_parts(package.version) + (0,) * 4)[:4]
        old_version = (version_parts(previous.version) + (0,) * 4)[:4] if previous else ()
        if previous is None or version > old_version or version == old_version and package.path.name < previous.path.name:
            chosen[package.extension_id] = package
    return chosen


class CatalogIndex:
    """Cache parsed metadata, never package bytes; stat every file on each lookup."""

    def __init__(self, packages_dir: Path):
        self.packages_dir = packages_dir
        self._entries: dict[Path, tuple[tuple, Package | dict]] = {}
        self._lock = threading.Lock()

    def get(self, platform: str | None = None, *, all_packages: bool = False) -> tuple[list[Package], list[dict]]:
        with self._lock:
            packages = []
            errors = []
            entries = {}
            for path in sorted(self.packages_dir.glob("*.crx")):
                try:
                    info = path.lstat()
                    stamp = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    cached = self._entries.get(path)
                    if cached and cached[0] == stamp:
                        value = cached[1]
                    else:
                        try:
                            value = read_package(path)
                            after = path.lstat()
                            if stamp != (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                                         after.st_mtime_ns, after.st_ctime_ns):
                                raise ValueError("package_changed")
                        except (OSError, ValueError, zipfile.BadZipFile) as error:
                            value = {"file": path.name, "error": str(error)}
                    entries[path] = (stamp, value)
                    if isinstance(value, dict):
                        errors.append(value)
                        continue
                    # Keep the platform argument for existing callers, but it
                    # is a client hint only. All platforms share one catalog.
                    packages.append(value)
                except OSError as error:
                    errors.append({"file": path.name, "error": str(error)})
            self._entries = entries
            selected = packages if all_packages else latest_by_id(packages).values()
            return sorted(selected, key=lambda item: (item.name.casefold(), item.extension_id,
                          tuple(-n for n in (version_parts(item.version) + (0,) * 4)[:4]),
                          item.path.name)), errors


def catalog(packages_dir: Path, platform: str | None = None) -> tuple[list[Package], list[dict]]:
    return CatalogIndex(packages_dir).get(platform)

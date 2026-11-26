#!/usr/bin/env python3
"""HTTP server for the local CRX catalog demo (loopback, LAN, optional tunnel)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from crx_catalog import CatalogIndex, Package, PLATFORMS, latest_by_id, valid_extension_id
from local_tunnel import LocalTunnel

HOST = "0.0.0.0"
LOOPBACK = "127.0.0.1"
PORT = 18731
ROOT = Path(__file__).resolve().parent
PACKAGES = ROOT / "packages"
PACKAGE_CHUNK_BYTES = 64 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/docs": ("api.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Return one inclusive byte range; ignore unsupported/malformed fields.

    Decimal bounds are clamped as strings before conversion so oversized
    numerals cannot overflow or hit Python's integer conversion limit.
    """
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", (value or "").strip(), re.IGNORECASE)
    if not match or not any(match.groups()):
        return None
    first, last = ((part.lstrip("0") or "0") if part else "" for part in match.groups())
    if first and last and (len(last), last) < (len(first), first):
        return None

    def bounded(number: str) -> int:
        limit = str(size)
        return size if (len(number), number) > (len(limit), limit) else int(number)

    if first:
        start = bounded(first)
        if start >= size:
            raise ValueError("range_not_satisfiable")
        return start, min(bounded(last), size - 1) if last else size - 1
    suffix = bounded(last)
    if suffix == 0 or size == 0:
        raise ValueError("range_not_satisfiable")
    return size - suffix, size - 1


def lan_ip() -> str | None:
    """Best-effort lookup of this machine's LAN IPv4 address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Connect to an external address so the kernel picks the right NIC.
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


def local_hostnames() -> set[str]:
    """Hostnames this process is reachable through; tunnels add theirs at runtime."""
    hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    if ip := lan_ip():
        hosts.add(ip)
    hostname = socket.gethostname().lower().rstrip(".")
    if hostname:
        hosts.update({hostname, f"{hostname}.local"})
    return hosts


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], packages_dir: Path,
                 package_delay: float = 0, package_without_length: bool = False,
                 control_token: str | None = None, instance_id: str | None = None):
        self.packages_dir = packages_dir
        self.catalog = CatalogIndex(packages_dir)
        self.package_delay = package_delay
        self.package_without_length = package_without_length
        self.control_token = control_token
        self.instance_id = instance_id or secrets.token_hex(16)
        self.stopping = threading.Event()
        self.allowed_hosts = local_hostnames()
        self.tunnel: LocalTunnel | None = None
        super().__init__(server_address, Handler)

    def runtime(self) -> dict:
        port = self.server_address[1]
        lan = lan_ip()
        tunnel = self.tunnel.snapshot() if self.tunnel else {"state": "disabled", "publicUrl": None, "error": None}
        return {
            "service": "arupa-webstore-demo",
            "instanceId": self.instance_id,
            "localUrl": f"http://{LOOPBACK}:{port}/",
            "lanUrl": f"http://{lan}:{port}/" if lan else None,
            "tunnel": tunnel,
        }


class Handler(BaseHTTPRequestHandler):
    server: DemoServer

    def send_headers(self, code: int, content_type: str, length: int,
                     extra_headers: dict[str, str] | None = None) -> None:
        package = content_type in ("application/x-chrome-extension", "application/octet-stream")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if not package or code == 206 or not self.server.package_without_length:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, no-transform" if package else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; connect-src 'self'; "
            "style-src 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def send_bytes(self, code: int, body: bytes, content_type: str,
                   extra_headers: dict[str, str] | None = None) -> None:
        self.send_headers(code, content_type, len(body), extra_headers)
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_package(self, package: Package, *, attachment: bool = False) -> None:
        source = None
        try:
            if package.path.is_symlink():
                raise ValueError("package_changed")
            source = package.path.open("rb")
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != package.size:
                raise ValueError("package_changed")
            digest = hashlib.sha256()
            remaining = package.size
            while remaining:
                chunk = source.read(min(PACKAGE_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("package_changed")
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(source.fileno())
            if (source.read(1) or digest.hexdigest() != package.sha256
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("package_changed")
            source.seek(0)
        except (OSError, ValueError):
            if source is not None:
                source.close()
            self.send_bytes(409, b"package_changed", "text/plain; charset=utf-8")
            return

        with source:
            etag = '"' + package.sha256 + '"'
            headers = {"Accept-Ranges": "bytes", "ETag": etag}
            selected = None
            ranges = self.headers.get_all("Range", [])
            validators = self.headers.get_all("If-Range", [])
            # Only GET has range semantics. A single matching strong ETag is
            # accepted; weak, date or ambiguous validators fall back to 200.
            if (self.command == "GET" and len(ranges) == 1
                    and (not validators or len(validators) == 1 and validators[0].strip() == etag)):
                try:
                    selected = byte_range(ranges[0], package.size)
                except ValueError:
                    self.send_bytes(416, b"range_not_satisfiable", "text/plain; charset=utf-8",
                                    {**headers, "Content-Range": f"bytes */{package.size}"})
                    return
            start, end = selected if selected is not None else (0, package.size - 1)
            length = end - start + 1
            if selected is not None:
                headers["Content-Range"] = f"bytes {start}-{end}/{package.size}"
            if attachment:
                filename = package.path.name
                fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
                headers["Content-Disposition"] = (
                    f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}')
            source.seek(start)
            self.send_headers(206 if selected is not None else 200,
                              "application/octet-stream" if attachment else "application/x-chrome-extension", length, headers)
            if self.command == "HEAD":
                return
            interval = max(1, (length + 7) // 8)
            chunk_size = min(PACKAGE_CHUNK_BYTES, interval) if self.server.package_delay else PACKAGE_CHUNK_BYTES
            sent, next_pause = 0, 0
            try:
                while sent < length:
                    if self.server.package_delay and sent >= next_pause:
                        if self.server.stopping.wait(self.server.package_delay):
                            self.close_connection = True
                            return
                        next_pause += interval
                    # A replaced pathname does not change this open file. In-place
                    # writes are unsupported; stop a changed/truncated response.
                    current = os.fstat(source.fileno())
                    if (current.st_size, current.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        self.close_connection = True
                        return
                    chunk = source.read(min(chunk_size, length - sent))
                    if not chunk:
                        self.close_connection = True
                        return
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent += len(chunk)
            except OSError:
                self.close_connection = True  # A closed/cancelled download owns no lasting server resources.

    def host_allowed(self) -> bool:
        host_header = self.headers.get("Host", "")
        hostname, _, port_text = host_header.partition(":")
        if port_text:
            if not port_text.isdigit() or int(port_text) != self.server.server_address[1]:
                return False
        # Accept loopback, this machine's LAN identity and an active tunnel host;
        # this keeps the DNS-rebinding guard while allowing remote access.
        return hostname.lower().rstrip(".") in self.server.allowed_hosts

    def loopback_request(self) -> bool:
        host = self.headers.get("Host", "").partition(":")[0].lower()
        return (host in LOOPBACK_HOSTS
                and self.headers.get("Origin") is None
                and self.headers.get("Sec-Fetch-Site") is None)

    def forbidden(self) -> None:
        self.send_bytes(403, b"forbidden", "text/plain; charset=utf-8")

    def do_GET(self) -> None:
        if not self.host_allowed():
            self.forbidden()
            return
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "same-site", "none"):
            # Allow opening the page from an external link (chat, IM, docs). That is
            # a top-level document navigation; cross-site subresource requests stay blocked.
            if not (self.headers.get("Sec-Fetch-Site") == "cross-site"
                    and self.headers.get("Sec-Fetch-Dest") == "document"):
                self.forbidden()
                return

        url = urlsplit(self.path)
        if url.path in ASSETS and not url.query:
            filename, content_type = ASSETS[url.path]
            try:
                body = (ROOT / filename).read_bytes()
            except OSError:
                self.send_bytes(500, b"asset_unavailable", "text/plain; charset=utf-8")
                return
            self.send_bytes(200, body, content_type)
            return

        if url.path == "/api/health" and not url.query:
            body = json.dumps({"status": "ok", "service": "arupa-webstore-demo"}).encode()
            self.send_bytes(200, body, "application/json; charset=utf-8")
            return

        if url.path == "/api/runtime" and not url.query:
            body = json.dumps(self.server.runtime(), ensure_ascii=False).encode()
            self.send_bytes(200, body, "application/json; charset=utf-8")
            return

        if url.path == "/api/catalog":
            query = parse_qs(url.query, keep_blank_values=True)
            platforms = query.get("platform", ["desktop"])
            if len(platforms) != 1 or platforms[0] not in PLATFORMS:
                self.send_bytes(400, b"invalid_platform", "text/plain; charset=utf-8")
                return
            all_values = query.get("all", ["0"])
            if len(all_values) != 1 or all_values[0] not in ("0", "1"):
                self.send_bytes(400, b"invalid_catalog_request", "text/plain; charset=utf-8")
                return
            all_packages = all_values[0] == "1"
            packages, errors = self.server.catalog.get(platforms[0], all_packages=all_packages)
            defaults = latest_by_id(packages)
            body = json.dumps(
                {"packages": [package.public() | ({"isLatest": defaults[package.extension_id] is package}
                              if all_packages else {}) for package in packages], "errors": errors},
                ensure_ascii=False,
            ).encode()
            self.send_bytes(200, body, "application/json; charset=utf-8")
            return

        if url.path == "/api/package":
            query = parse_qs(url.query, keep_blank_values=True)
            ids = query.get("id", [])
            if len(ids) != 1 or not valid_extension_id(ids[0]):
                self.send_bytes(400, b"invalid_extension_id", "text/plain; charset=utf-8")
                return
            platforms = query.get("platform", ["desktop"])
            hashes = query.get("sha256", [])
            files = query.get("file", [])
            downloads = query.get("download", ["0"])
            if (len(platforms) != 1 or platforms[0] not in PLATFORMS
                    or hashes and (len(hashes) != 1 or not re.fullmatch(r"[a-f0-9]{64}", hashes[0]))
                    or files and (len(files) != 1 or not files[0] or any(c in files[0] for c in ("/", "\\", "\x00")))
                    or len(downloads) != 1 or downloads[0] not in ("0", "1")):
                self.send_bytes(400, b"invalid_package_request", "text/plain; charset=utf-8")
                return
            packages, _ = self.server.catalog.get(platforms[0], all_packages=bool(files))
            package = next((item for item in packages if item.extension_id == ids[0]
                            and (not files or item.path.name == files[0])), None)
            if package is None:
                self.send_bytes(404, b"package_not_found", "text/plain; charset=utf-8")
                return
            if hashes and hashes[0] != package.sha256:
                self.send_bytes(409, b"package_changed", "text/plain; charset=utf-8")
                return
            if downloads[0] == "1":
                self.send_package(package, attachment=True)
            else:
                self.send_package(package)
            return

        self.send_bytes(404, b"not_found", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/_control/stop":
            self.send_bytes(404, b"not_found", "text/plain; charset=utf-8")
            return
        token = self.server.control_token
        supplied = self.headers.get("X-Store-Control-Token", "")
        if not token or not self.loopback_request() or not secrets.compare_digest(supplied, token):
            self.forbidden()
            return
        self.server.stopping.set()
        body = json.dumps({"instanceId": self.server.instance_id}).encode()
        self.send_bytes(200, body, "application/json; charset=utf-8")
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def create_server(packages_dir: Path = PACKAGES, package_delay: float = 0,
                  package_without_length: bool = False, port: int = PORT,
                  control_token: str | None = None, instance_id: str | None = None) -> DemoServer:
    packages_dir.mkdir(exist_ok=True)
    return DemoServer((HOST, port), packages_dir, package_delay, package_without_length,
                      control_token=control_token, instance_id=instance_id)


def parse_args(argv: list[str] | None = None, defaults: dict | None = None) -> argparse.Namespace:
    defaults = defaults or {}
    parser = argparse.ArgumentParser(description="启动逐风扩展商店本地 Demo")
    parser.add_argument("--open", action="store_true", help="启动后在默认浏览器中打开 Demo 页面")
    parser.add_argument("--port", type=int, default=defaults.get("port", PORT), help="监听端口，默认 18731")
    parser.add_argument("--packages-dir", type=Path, default=Path(defaults.get("packages_dir", PACKAGES)),
                        help="指定隔离的 CRX 测试目录")
    parser.add_argument("--package-delay-ms", type=int, default=defaults.get("package_delay_ms", 0),
                        help="取包响应及每块包体的延迟，0–5000 毫秒")
    parser.add_argument("--package-without-length", action="store_true",
                        default=defaults.get("package_without_length", False),
                        help="CRX 整包响应省略 Content-Length；206 分段响应仍返回分段长度")
    parser.add_argument(
        "--no-tunnel",
        dest="tunnel",
        action="store_false",
        help="只在本机和局域网提供访问，不申请随机公网域名",
    )
    parser.add_argument(
        "--tunnel",
        dest="tunnel",
        action="store_true",
        help="申请随机 trycloudflare.com 域名（默认开启，保留该参数以兼容旧命令）",
    )
    parser.set_defaults(tunnel=defaults.get("tunnel", True))
    parser.add_argument(
        "--browser",
        default=defaults.get("browser", os.environ.get("ARUPA_BROWSER")),
        help="逐风浏览器可执行文件；也可通过 ARUPA_BROWSER 环境变量设置",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.package_delay_ms <= 5000:
        parser.error("--package-delay-ms 必须为 0–5000")
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须为 1–65535")
    return args


def demo_is_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}api/health", timeout=1) as response:
            payload = json.load(response)
        if response.status == 200 and payload == {
            "status": "ok",
            "service": "arupa-webstore-demo",
        }:
            return True
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    # Older revisions did not expose /api/health. Recognize their read-only
    # catalog shape so rerunning start.* still opens a service already in use.
    try:
        with urllib.request.urlopen(f"{url}api/catalog", timeout=1) as response:
            payload = json.load(response)
        return (
            response.status == 200
            and isinstance(payload, dict)
            and isinstance(payload.get("packages"), list)
            and isinstance(payload.get("errors"), list)
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def open_demo(url: str, browser: str | None) -> None:
    if browser:
        subprocess.Popen([browser, f"--apps-gallery-url={url}", url])
    else:
        webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.browser:
        browser_path = Path(args.browser).expanduser().resolve()
        if not browser_path.is_file():
            raise SystemExit(f"逐风浏览器不存在：{browser_path}")
        args.browser = str(browser_path)
    url = f"http://{LOOPBACK}:{args.port}/"
    allowed_hosts = local_hostnames()
    tunnel = LocalTunnel(f"http://{LOOPBACK}:{args.port}", args.tunnel, allowed_hosts)
    try:
        server = create_server(args.packages_dir, args.package_delay_ms / 1000, args.package_without_length,
                               port=args.port,
                               control_token=os.environ.get("ARUPA_STORE_CONTROL_TOKEN") or None,
                               instance_id=os.environ.get("ARUPA_STORE_INSTANCE_ID") or None)
    except OSError as error:
        if args.open and demo_is_running(url):
            print(f"Demo 服务已在运行，正在打开：{url}")
            open_demo(url, args.browser)
            return 0
        raise SystemExit(f"无法启动 {HOST}:{args.port}：{error}") from error
    server.allowed_hosts = allowed_hosts
    server.tunnel = tunnel

    print(f"逐风扩展商店 Demo：{url}", flush=True)
    if ip := lan_ip():
        print(f"局域网访问：  http://{ip}:{args.port}/", flush=True)
    print(f"插件目录：{args.packages_dir}")
    if args.tunnel:
        print("正在申请随机域名…（首次运行会下载 cloudflared）", flush=True)
    else:
        print("已关闭随机域名。", flush=True)
    tunnel.start()
    print("按 Ctrl+C 停止服务。", flush=True)

    def handle_stop(_signum: int | None = None, _frame: object | None = None) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_stop)
    if args.open:
        threading.Timer(0.2, open_demo, args=(url, args.browser)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.stop()
        server.server_close()
    print("\nDemo 服务已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

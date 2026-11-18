#!/usr/bin/env python3
"""HTTP server for the local CRX catalog demo (loopback + LAN)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crx_catalog import catalog, valid_extension_id

HOST = "0.0.0.0"
LOOPBACK = "127.0.0.1"
PORT = 18731
ROOT = Path(__file__).resolve().parent
PACKAGES = ROOT / "packages"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/docs": ("api.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], packages_dir: Path,
                 package_delay: float = 0, package_without_length: bool = False):
        self.packages_dir = packages_dir
        self.package_delay = package_delay
        self.package_without_length = package_without_length
        super().__init__(server_address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: DemoServer

    def send_bytes(self, code: int, body: bytes, content_type: str) -> None:
        package = content_type == "application/x-chrome-extension"
        delay = self.server.package_delay if package else 0
        if delay:
            time.sleep(delay)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if not package or not self.server.package_without_length:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; connect-src 'self'; "
            "style-src 'self'; object-src 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if not delay:
            self.wfile.write(body)
            return
        chunk_size = max(1, (len(body) + 7) // 8)
        try:
            for offset in range(0, len(body), chunk_size):
                time.sleep(delay)
                self.wfile.write(body[offset:offset + chunk_size])
        except (BrokenPipeError, ConnectionResetError):
            pass  # The browser can cancel the test transfer by closing the page.

    def do_GET(self) -> None:
        expected_port = self.server.server_address[1]
        host_header = self.headers.get("Host", "")
        # Accept any Host that carries our port (covers localhost, 127.0.0.1,
        # LAN IPs, and hostnames). Reject if the port is explicitly wrong.
        if ":" in host_header:
            try:
                port = int(host_header.rsplit(":", 1)[1])
                if port != expected_port:
                    self.send_bytes(403, b"forbidden", "text/plain; charset=utf-8")
                    return
            except ValueError:
                self.send_bytes(403, b"forbidden", "text/plain; charset=utf-8")
                return
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            self.send_bytes(403, b"forbidden", "text/plain; charset=utf-8")
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

        if url.path == "/api/catalog" and not url.query:
            packages, errors = catalog(self.server.packages_dir)
            body = json.dumps(
                {"packages": [package.public() for package in packages], "errors": errors},
                ensure_ascii=False,
            ).encode()
            self.send_bytes(200, body, "application/json; charset=utf-8")
            return

        if url.path == "/api/package":
            ids = parse_qs(url.query, keep_blank_values=True).get("id", [])
            if len(ids) != 1 or not valid_extension_id(ids[0]):
                self.send_bytes(400, b"invalid_extension_id", "text/plain; charset=utf-8")
                return
            packages, _ = catalog(self.server.packages_dir)
            package = next((item for item in packages if item.extension_id == ids[0]), None)
            if package is None:
                self.send_bytes(404, b"package_not_found", "text/plain; charset=utf-8")
                return
            try:
                data = package.path.read_bytes()
                if len(data) != package.size or hashlib.sha256(data).hexdigest() != package.sha256:
                    raise ValueError("package_changed")
            except (OSError, ValueError):
                self.send_bytes(409, b"package_changed", "text/plain; charset=utf-8")
                return
            self.send_bytes(200, data, "application/x-chrome-extension")
            return

        self.send_bytes(404, b"not_found", "text/plain; charset=utf-8")


def create_server(packages_dir: Path = PACKAGES, package_delay: float = 0,
                  package_without_length: bool = False) -> DemoServer:
    packages_dir.mkdir(exist_ok=True)
    return DemoServer((HOST, PORT), packages_dir, package_delay, package_without_length)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动逐风扩展商店本地 Demo")
    parser.add_argument("--open", action="store_true", help="启动后在默认浏览器中打开 Demo 页面")
    parser.add_argument("--packages-dir", type=Path, default=PACKAGES, help="指定隔离的 CRX 测试目录")
    parser.add_argument("--package-delay-ms", type=int, default=0, help="取包响应及每块包体的延迟，0–5000 毫秒")
    parser.add_argument("--package-without-length", action="store_true", help="CRX 响应省略 Content-Length")
    parser.add_argument(
        "--browser",
        default=os.environ.get("ARUPA_BROWSER"),
        help="逐风浏览器可执行文件；也可通过 ARUPA_BROWSER 环境变量设置",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.package_delay_ms <= 5000:
        parser.error("--package-delay-ms 必须为 0–5000")
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


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.browser:
        browser_path = Path(args.browser).expanduser().resolve()
        if not browser_path.is_file():
            raise SystemExit(f"逐风浏览器不存在：{browser_path}")
        args.browser = str(browser_path)
    url = f"http://{LOOPBACK}:{PORT}/"
    try:
        server = create_server(args.packages_dir, args.package_delay_ms / 1000, args.package_without_length)
    except OSError as error:
        if args.open and demo_is_running(url):
            print(f"Demo 服务已在运行，正在打开：{url}")
            open_demo(url, args.browser)
            return
        raise SystemExit(f"无法启动 {HOST}:{PORT}：{error}") from error

    print(f"逐风扩展商店 Demo：{url}")
    if ip := lan_ip():
        print(f"局域网访问：  http://{ip}:{PORT}/")
    print(f"插件目录：{args.packages_dir}")
    print("按 Ctrl+C 停止服务。", flush=True)
    if args.open:
        threading.Timer(0.2, open_demo, args=(url, args.browser)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDemo 服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

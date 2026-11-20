#!/usr/bin/env python3
"""HTTP server for the local CRX catalog demo (loopback, LAN, optional tunnel)."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import queue
import re
import socket
import subprocess
import sys
import tarfile
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
CLOUDFLARED = ROOT / ".bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
CLOUDFLARED_RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
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
        host_header = self.headers.get("Host", "")
        hostname, _, port_text = host_header.partition(":")
        if port_text:
            if not port_text.isdigit() or int(port_text) != self.server.server_address[1]:
                self.send_bytes(403, b"forbidden", "text/plain; charset=utf-8")
                return
        # Accept loopback, this machine's LAN identity and an active tunnel host;
        # this keeps the DNS-rebinding guard while allowing remote access.
        if hostname.lower().rstrip(".") not in ALLOWED_HOSTS:
            self.send_bytes(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "same-site", "none"):
            # Allow opening the page from an external link (chat, IM, docs). That is
            # a top-level document navigation; cross-site subresource requests stay blocked.
            if not (self.headers.get("Sec-Fetch-Site") == "cross-site"
                    and self.headers.get("Sec-Fetch-Dest") == "document"):
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

        if url.path == "/api/catalog":
            platforms = parse_qs(url.query, keep_blank_values=True).get("platform", ["desktop"])
            if len(platforms) != 1 or platforms[0] not in ("desktop", "android"):
                self.send_bytes(400, b"invalid_platform", "text/plain; charset=utf-8")
                return
            packages, errors = catalog(self.server.packages_dir, platforms[0])
            body = json.dumps(
                {"packages": [package.public() for package in packages], "errors": errors},
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
            if (len(platforms) != 1 or platforms[0] not in ("desktop", "android")
                    or hashes and (len(hashes) != 1 or not re.fullmatch(r"[a-f0-9]{64}", hashes[0]))):
                self.send_bytes(400, b"invalid_package_request", "text/plain; charset=utf-8")
                return
            packages, _ = catalog(self.server.packages_dir, platforms[0])
            package = next((item for item in packages if item.extension_id == ids[0]), None)
            if package is None:
                self.send_bytes(404, b"package_not_found", "text/plain; charset=utf-8")
                return
            if hashes and hashes[0] != package.sha256:
                self.send_bytes(409, b"package_changed", "text/plain; charset=utf-8")
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
    parser.set_defaults(tunnel=True)
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


def local_hostnames() -> set[str]:
    """Hostnames this process is reachable through; tunnels add theirs at runtime."""
    hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    if ip := lan_ip():
        hosts.add(ip)
    hostname = socket.gethostname().lower().rstrip(".")
    if hostname:
        hosts.update({hostname, f"{hostname}.local"})
    return hosts


ALLOWED_HOSTS = local_hostnames()


def cloudflared_asset() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "cloudflared-darwin-arm64.tgz" if machine in ("arm64", "aarch64") else "cloudflared-darwin-amd64.tgz"
    if sys.platform.startswith("linux"):
        return "cloudflared-linux-arm64" if machine in ("arm64", "aarch64") else "cloudflared-linux-amd64"
    if sys.platform == "win32":
        return "cloudflared-windows-amd64.exe" if machine in ("amd64", "x86_64") else "cloudflared-windows-386.exe"
    raise SystemExit(f"暂不支持在 {sys.platform} 上自动下载 cloudflared，请手动安装后重试。")


def ensure_cloudflared() -> Path:
    """Return the local cloudflared binary, downloading it on first use."""
    if CLOUDFLARED.is_file():
        return CLOUDFLARED
    asset = cloudflared_asset()
    url = CLOUDFLARED_RELEASE + asset
    print(f"首次使用随机域名需要下载 cloudflared：{url}", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            payload = response.read()
    except OSError as error:
        raise SystemExit(
            f"下载 cloudflared 失败：{error}\n可手动下载 {url} 后放到 {CLOUDFLARED}"
        ) from error

    if asset.endswith(".tgz"):
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                member = archive.extractfile("cloudflared")
                if member is None:
                    raise KeyError("cloudflared")
                payload = member.read()
        except (KeyError, tarfile.TarError) as error:
            raise SystemExit("cloudflared 压缩包内容异常，请重试或手动安装。") from error

    CLOUDFLARED.parent.mkdir(exist_ok=True)
    temporary = CLOUDFLARED.with_name(CLOUDFLARED.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.chmod(0o755)
    temporary.replace(CLOUDFLARED)
    return CLOUDFLARED


TUNNEL_PROCESS: subprocess.Popen[str] | None = None


def start_tunnel() -> tuple[subprocess.Popen[str], str]:
    """Open a Cloudflare quick tunnel and return its process and public URL."""
    global TUNNEL_PROCESS
    process = subprocess.Popen(
        [str(ensure_cloudflared()), "tunnel", "--url", f"http://{LOOPBACK}:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    TUNNEL_PROCESS = process
    found: queue.Queue[str | None] = queue.Queue()
    log: list[str] = []

    def read_output() -> None:
        for line in process.stdout or []:
            log.append(line)
            match = TUNNEL_URL_RE.search(line)
            if match and found.empty():
                found.put(match.group(0))
        found.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    try:
        url = found.get(timeout=60)
    except queue.Empty:
        process.terminate()
        raise SystemExit("申请随机域名超时，请检查网络后重试。") from None
    if url is None:
        process.terminate()
        detail = "".join(log[-8:]).strip()
        raise SystemExit(f"随机域名启动失败。\n{detail}")
    return process, url


def start_tunnel_async() -> None:
    """Apply for a public URL in the background so local access starts first."""

    def run() -> None:
        try:
            _, public_url = start_tunnel()
        except SystemExit as error:
            print(f"随机域名不可用，当前仅能本机/局域网访问：{error}", flush=True)
            return
        ALLOWED_HOSTS.add(urlsplit(public_url).hostname or "")
        print(f"随机域名访问：{public_url}/", flush=True)

    threading.Thread(target=run, daemon=True).start()


def stop_tunnel() -> None:
    process = TUNNEL_PROCESS
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


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
    if args.tunnel:
        print("正在申请随机域名…（首次运行会下载 cloudflared）", flush=True)
        start_tunnel_async()
    print("按 Ctrl+C 停止服务。", flush=True)

    if args.open:
        threading.Timer(0.2, open_demo, args=(url, args.browser)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDemo 服务已停止。")
    finally:
        stop_tunnel()
        server.server_close()


if __name__ == "__main__":
    main()

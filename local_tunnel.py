"""Own the quick tunnel for exactly one store server's lifetime."""

from __future__ import annotations

from collections import deque
import io
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request

ROOT = Path(__file__).resolve().parent
CLOUDFLARED = ROOT / ".bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def cloudflared_asset() -> str:
    arm = platform.machine().lower() in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return f"cloudflared-darwin-{'arm64' if arm else 'amd64'}.tgz"
    if sys.platform.startswith("linux"):
        return f"cloudflared-linux-{'arm64' if arm else 'amd64'}"
    if sys.platform == "win32":
        arch = "arm64" if arm else "amd64" if platform.machine().lower() in ("amd64", "x86_64") else "386"
        return f"cloudflared-windows-{arch}.exe"
    raise OSError(f"不支持自动下载 cloudflared：{sys.platform}")


def ensure_cloudflared() -> Path:
    if CLOUDFLARED.is_file():
        return CLOUDFLARED
    asset = cloudflared_asset()
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/" + asset
    print(f"正在下载 cloudflared：{url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read(128 * 1024 * 1024 + 1)
    if len(payload) > 128 * 1024 * 1024:
        raise OSError("cloudflared 下载超过大小限制")
    if asset.endswith(".tgz"):
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = archive.extractfile("cloudflared")
            if member is None:
                raise OSError("cloudflared 压缩包内容异常")
            payload = member.read()
    CLOUDFLARED.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=CLOUDFLARED.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
        temporary.chmod(0o755)
        temporary.replace(CLOUDFLARED)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return CLOUDFLARED


class LocalTunnel:
    def __init__(self, origin: str, enabled: bool, allowed_hosts: set[str]):
        self.origin = origin
        self.allowed_hosts = allowed_hosts
        self.status = "starting" if enabled else "disabled"
        self.public_url: str | None = None
        self.error: str | None = None
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.status, "publicUrl": self.public_url, "error": self.error}

    def start(self) -> None:
        if self.status == "disabled":
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        tail: deque[str] = deque(maxlen=8)
        try:
            executable = ensure_cloudflared()
            # Stop can arrive while the executable is downloading. Never spawn after it.
            with self._lock:
                if self._stopped.is_set():
                    return
                self.process = subprocess.Popen(
                    [str(executable), "tunnel", "--url", self.origin, "--no-autoupdate"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                process = self.process
            timer = threading.Timer(60, self._startup_timeout)
            timer.daemon = True
            timer.start()
            public_url = None
            try:
                for line in process.stdout:
                    tail.append(line.rstrip())
                    print(f"[cloudflared] {line.rstrip()}", flush=True)
                    if match := URL_RE.search(line):
                        public_url = match.group(0)
                    if public_url and "Registered tunnel connection" in line:
                        with self._lock:
                            if not self._stopped.is_set() and self.status == "starting":
                                self.public_url = public_url
                                self.allowed_hosts.add(public_url.split("//", 1)[1])
                                self.status = "ready"
                                timer.cancel()
                                print(f"随机域名访问：{public_url}/", flush=True)
                process.wait()
            finally:
                timer.cancel()
                process.stdout.close()
            if not self._stopped.is_set():
                raise OSError(self.error or f"隧道进程退出（{process.returncode}）：" + " | ".join(tail))
        except (OSError, ValueError, KeyError, tarfile.TarError) as error:
            with self._lock:
                if self.public_url:
                    self.allowed_hosts.discard(self.public_url.split("//", 1)[1])
                self.public_url = None
                if not self._stopped.is_set():
                    self.status = "failed"
                    self.error = str(error)
            print(f"随机域名不可用，本机/局域网仍可访问：{error}", flush=True)
        finally:
            with self._lock:
                if self.public_url:
                    self.allowed_hosts.discard(self.public_url.split("//", 1)[1])
                self.public_url = None

    def _startup_timeout(self) -> None:
        with self._lock:
            if self.status != "starting" or self._stopped.is_set():
                return
            self.error = "隧道连接超时，请检查网络后重启"
            self.status = "failed"
            process = self.process
        self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except ProcessLookupError:
            pass

    def stop(self) -> None:
        with self._lock:
            self._stopped.set()
            self.status = "stopped"
            process = self.process
            if self.public_url:
                self.allowed_hosts.discard(self.public_url.split("//", 1)[1])
            self.public_url = None
        self._terminate(process)
        if self._thread:
            self._thread.join(timeout=1)

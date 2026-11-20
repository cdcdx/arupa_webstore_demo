#!/usr/bin/env python3
"""Cross-platform lifecycle commands. Never kill a listener based on a PID file."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

if sys.version_info < (3, 9):
    raise SystemExit("需要 Python 3.9 或更高版本。")

import server

ROOT = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@contextmanager
def lifecycle_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "control.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 20
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个启停操作仍在进行，请稍后重试。") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(directory: Path) -> dict:
    try:
        value = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(directory: Path, state: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as output:
            temporary = Path(output.name)
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write("\n")
        temporary.replace(directory / "state.json")
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def probe(port: int) -> dict | None:
    try:
        with OPENER.open(f"http://127.0.0.1:{port}/api/runtime", timeout=1) as response:
            value = json.load(response)
        if value.get("service") == "arupa-webstore-demo" and isinstance(value.get("instanceId"), str):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def owned(runtime: dict | None, state: dict) -> bool:
    return bool(runtime and state.get("controlToken") and runtime["instanceId"] == state.get("instanceId"))


def show_status(runtime: dict | None, state: dict, port: int, directory: Path) -> None:
    if runtime:
        print("商店运行中。" if owned(runtime, state) else "商店运行中（未由当前启停脚本管理）。")
        print(f"本机：{runtime['localUrl']}")
        if runtime.get("lanUrl"):
            print(f"局域网：{runtime['lanUrl']}")
        tunnel = runtime.get("tunnel", {})
        print(f"公网：{tunnel.get('publicUrl') or {'starting': '正在连接，请稍后运行 start.* status', 'disabled': '未开启', 'failed': '不可用', 'stopped': '已停止'}.get(tunnel.get('state'), '不可用')}")
        if tunnel.get("error"):
            print(f"隧道提示：{tunnel['error']}")
    elif listening(port):
        print(f"端口 {port} 已被占用，未识别为当前受管商店。")
    else:
        print("商店已停止。")
    print(f"日志：{directory / 'server.log'}")


def config_args(config: dict) -> list[str]:
    arguments = ["--port", str(config["port"]), "--packages-dir", config["packages_dir"],
                 "--package-delay-ms", str(config["package_delay_ms"]),
                 "--tunnel" if config["tunnel"] else "--no-tunnel"]
    if config["package_without_length"]:
        arguments.append("--package-without-length")
    # The browser is opened by this controller, never by the detached child.
    return arguments


def start(directory: Path, config: dict, open_page: bool, state: dict) -> None:
    port = config["port"]
    runtime = probe(port)
    if runtime or listening(port):
        if not owned(runtime, state):
            raise RuntimeError(f"端口 {port} 已有其他服务或旧版前台商店；请在原终端停止后再启动。本脚本不会终止未知进程。")
        if state.get("config") != config:
            raise RuntimeError("商店已按其他参数运行。请使用 start.sh restart（Windows：start.bat restart）应用新参数。")
        print("复用现有服务，公网域名保持不变。")
    else:
        instance_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        environment = {**os.environ, "ARUPA_STORE_INSTANCE_ID": instance_id, "ARUPA_STORE_CONTROL_TOKEN": token}
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
        with (directory / "server.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, "-u", str(ROOT / "server.py"), *config_args(config)],
                                       cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, **options)
        state = {"instanceId": instance_id, "controlToken": token, "pid": process.pid,
                 "port": port, "config": config}
        try:
            save_state(directory, state)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"商店启动失败（退出码 {process.returncode}），请查看 {directory / 'server.log'}")
                runtime = probe(port)
                if owned(runtime, state):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("商店启动超时，请查看 server.log。")
        except BaseException:
            # This is the child we just created, not an unverified PID from disk.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            raise
        print("商店已在后台启动，关闭终端后仍继续运行。")
    show_status(runtime, state, port, directory)
    if open_page:
        try:
            server.open_demo(runtime["localUrl"], config.get("browser"))
        except OSError as error:
            print(f"服务运行正常，但打开浏览器失败：{error}。请手动打开上面的地址。")


def stop(directory: Path, state: dict, port: int) -> None:
    runtime = probe(port)
    if not runtime and not listening(port):
        print("商店已停止，无需重复停止。")
        return
    if not owned(runtime, state):
        raise RuntimeError(f"端口 {port} 不是当前受管实例，无法安全停止。旧版前台服务请在原终端按 Ctrl+C。")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/_control/stop", data=b"", method="POST",
                                     headers={"X-Store-Control-Token": state["controlToken"]})
    with OPENER.open(request, timeout=3) as response:
        result = json.load(response)
    if result.get("instanceId") != state["instanceId"]:
        raise RuntimeError("停止回执与当前实例不一致，未执行后续操作。")
    deadline = time.monotonic() + 15
    while listening(port):
        current = probe(port)
        if current and current["instanceId"] != state["instanceId"]:
            raise RuntimeError("端口已由另一个实例接管，停止检查已中止。")
        if time.monotonic() >= deadline:
            raise RuntimeError("停止超时，请查看日志；未强行终止任何进程。")
        time.sleep(0.1)
    save_state(directory, {**state, "pid": None, "controlToken": None})
    print("商店及其隧道已停止。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="逐风本地商店：不带子命令时启动并打开页面",
        epilog="统一入口：start.sh / start.command / start.bat [start|stop|restart|status]。start/restart 还支持 --no-tunnel、--packages-dir、--browser 等参数。")
    parser.add_argument("command", nargs="?", default="start", choices=("start", "stop", "restart", "status"))
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / ".runtime", help="状态和日志目录")
    parser.add_argument("--port", type=int, help="默认 18731；重用已保存的端口")
    parser.add_argument("--open", dest="open_page", action="store_true", help="打开商店页面")
    parser.add_argument("--no-open", dest="open_page", action="store_false", help="不打开浏览器")
    parser.set_defaults(open_page=None)
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in ("start", "stop", "restart", "status"):
        arguments.insert(0, "start")
    args, forwarded = parser.parse_known_args(arguments)
    if args.open_page is None:
        args.open_page = args.command in ("start", "restart")
    if args.command in ("status", "stop") and (forwarded or args.open_page):
        parser.error("stop/status 不接受浏览器或服务配置参数")
    directory = args.runtime_dir.expanduser().resolve()

    def run() -> None:
        state = load_state(directory)
        port = args.port if args.port is not None else state.get("port", server.PORT)
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise RuntimeError("端口必须为 1–65535")
        if state.get("port", port) != port:
            raise RuntimeError("状态目录对应另一个端口；请使用不同的 --runtime-dir。")
        if args.command == "status":
            show_status(probe(port), state, port, directory)
            return
        if args.command == "stop":
            stop(directory, state, port)
            return
        defaults = {**state.get("config", {}), "port": port}
        options = server.parse_args(forwarded, defaults)
        config = {name: getattr(options, name) for name in
                  ("port", "packages_dir", "package_delay_ms", "package_without_length", "tunnel", "browser")}
        config["packages_dir"] = str(config["packages_dir"])
        if args.command == "restart":
            stop(directory, state, port)
        start(directory, config, args.open_page, load_state(directory))

    try:
        if args.command == "status":
            run()
        else:
            with lifecycle_lock(directory):
                run()
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import crx_catalog
import local_tunnel
import server
import storectl


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("condition did not become true")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class StoreLifecycleTests(unittest.TestCase):
    def test_posix_entrypoints_from_another_working_directory(self):
        if os.name == "nt":
            self.skipTest("POSIX shell entrypoints")
        with tempfile.TemporaryDirectory(prefix="store scripts ") as temporary:
            root = Path(temporary)
            options = ["--runtime-dir", str(root / "runtime"), "--port", str(free_port())]

            def script(name, command=None, *extra):
                command_args = [command] if command else []
                return subprocess.run([str(server.ROOT / name), *command_args, *options, *extra], cwd=root,
                                      capture_output=True, text=True, timeout=20)

            try:
                for name, command in (("start.command", None), ("start.sh", "restart")):
                    result = script(name, command, "--no-open", "--no-tunnel", "--packages-dir", str(root / "packages"))
                    self.assertEqual(result.returncode, 0, result.stderr)
                result = script("start.command", "status")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("运行中", result.stdout)
                result = script("start.command", "stop")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("已停止", result.stdout)
            finally:
                script("start.sh", "stop")

    def test_parallel_start_reuses_instance_restart_preserves_options_and_stop_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="store lifecycle ") as temporary:
            directory = Path(temporary)
            packages = directory / "test packages"
            packages.mkdir()
            (packages / "demo.crx").write_bytes((server.PACKAGES / "nomad-demo-1.0.crx").read_bytes())
            runtime_dir = directory / "runtime"
            port = free_port()

            def control(command, *options):
                return subprocess.run([sys.executable, str(server.ROOT / "storectl.py"), command,
                                       "--runtime-dir", str(runtime_dir), "--port", str(port), *options],
                                      cwd=directory, text=True, capture_output=True, timeout=25)

            try:
                options = ("--no-tunnel", "--packages-dir", str(packages), "--package-delay-ms", "17",
                           "--package-without-length", "--no-open")
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    starts = list(pool.map(lambda _: control("start", *options), range(2)))
                for result in starts:
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertTrue(any("复用现有服务" in result.stdout for result in starts))
                state = storectl.load_state(runtime_dir)
                old_runtime = storectl.probe(port)
                self.assertEqual(old_runtime["instanceId"], state["instanceId"])
                self.assertEqual(old_runtime["tunnel"]["state"], "disabled")
                self.assertNotIn(state["controlToken"], json.dumps(old_runtime))
                self.assertNotIn(state["controlToken"], control("status").stdout)
                rejected = control("start", "--package-delay-ms", "18")
                self.assertEqual(rejected.returncode, 1)
                self.assertIn("restart", rejected.stderr)
                self.assertEqual(storectl.probe(port)["instanceId"], state["instanceId"])
                restarted = control("restart", "--no-open")
                self.assertEqual(restarted.returncode, 0, restarted.stderr)
                new_state = storectl.load_state(runtime_dir)
                self.assertNotEqual(new_state["instanceId"], state["instanceId"])
                self.assertEqual(new_state["config"], state["config"])
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", "/api/package?id=" + crx_catalog.read_package(packages / "demo.crx").extension_id)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIsNone(response.getheader("Content-Length"))
                self.assertEqual(response.read(), (packages / "demo.crx").read_bytes())
                connection.close()
                self.assertEqual(control("stop").returncode, 0)
                self.assertFalse(storectl.listening(port))
                self.assertEqual(control("stop").returncode, 0)
                self.assertIn("已停止", control("status").stdout)
                if os.name != "nt":
                    self.assertEqual((runtime_dir / "state.json").stat().st_mode & 0o777, 0o600)
            finally:
                control("stop")

    def test_stale_pid_does_not_stop_or_replace_an_unowned_listener(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            httpd = server.DemoServer(("127.0.0.1", 0), directory)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_address[1]
            storectl.save_state(directory, {"instanceId": "stale", "controlToken": "wrong",
                                           "pid": os.getpid(), "port": port})
            try:
                for command in ("start", "stop", "restart"):
                    result = subprocess.run([sys.executable, str(server.ROOT / "storectl.py"), command,
                                             "--runtime-dir", str(directory), "--no-open"],
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(storectl.probe(port)["instanceId"], httpd.instance_id)
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()

    def test_foreground_sigterm_closes_listener(self):
        if os.name == "nt":
            self.skipTest("Windows stop uses the authenticated control endpoint; POSIX SIGTERM case")
        with tempfile.TemporaryDirectory() as temporary:
            port = free_port()
            process = subprocess.Popen([sys.executable, str(server.ROOT / "server.py"), "--no-tunnel",
                                        "--port", str(port), "--packages-dir", temporary],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                wait_until(lambda: storectl.probe(port))
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertFalse(storectl.listening(port))
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)


class HttpAndCatalogTests(unittest.TestCase):
    def test_metadata_cache_invalidates_on_replace_and_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "demo.crx"
            path.write_bytes((server.PACKAGES / "nomad-demo-1.0.crx").read_bytes())
            index = crx_catalog.CatalogIndex(root)
            with mock.patch.object(crx_catalog, "read_package", wraps=crx_catalog.read_package) as reader:
                self.assertEqual(index.get("desktop")[0][0].version, "1.0")
                index.get("android")
                self.assertEqual(reader.call_count, 1)
                replacement = root / "new.tmp"
                replacement.write_bytes((server.ROOT / "samples/nomad-demo-1.1.crx").read_bytes())
                replacement.replace(path)
                self.assertEqual(index.get("desktop")[0][0].version, "1.1")
                self.assertEqual(reader.call_count, 2)
                path.unlink()
                self.assertEqual(index.get(), ([], []))

    def test_control_requires_token_loopback_host_and_no_web_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            httpd = server.DemoServer(("127.0.0.1", 0), Path(temporary), control_token="private-token")
            httpd.allowed_hosts.add("public.trycloudflare.com")
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=2)
            try:
                for extra in ({"X-Store-Control-Token": "wrong"},
                              {"X-Store-Control-Token": "private-token", "Origin": "http://localhost"},
                              {"X-Store-Control-Token": "private-token", "Sec-Fetch-Site": "same-origin"},
                              {"X-Store-Control-Token": "private-token", "Host": "public.trycloudflare.com"}):
                    connection.request("POST", "/_control/stop", body=b"", headers=extra)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                    self.assertFalse(httpd.stopping.is_set())
                connection.request("GET", "/api/runtime")
                payload = connection.getresponse().read()
                self.assertNotIn(b"private-token", payload)
                connection.request("POST", "/_control/stop", body=b"",
                                   headers={"X-Store-Control-Token": "private-token"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["instanceId"], httpd.instance_id)
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
            finally:
                connection.close()
                httpd.shutdown(); httpd.server_close(); thread.join()

    def test_package_transfer_uses_verified_open_file_across_atomic_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = (server.PACKAGES / "nomad-demo-1.0.crx").read_bytes()
            package = root / "demo.crx"
            package.write_bytes(original)
            httpd = server.DemoServer(("127.0.0.1", 0), root)
            send_headers = server.Handler.send_headers

            def replace_after_verification(handler, code, content_type, length):
                if content_type == "application/x-chrome-extension":
                    replacement = root / "replacement.tmp"
                    replacement.write_bytes(b"different file")
                    replacement.replace(package)
                return send_headers(handler, code, content_type, length)

            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
            try:
                with mock.patch.object(server.Handler, "send_headers", replace_after_verification):
                    connection.request("GET", "/api/package?id=" + crx_catalog.crx_id(original))
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), original)
            finally:
                connection.close()
                httpd.shutdown(); httpd.server_close(); thread.join()


class TunnelLifecycleTests(unittest.TestCase):
    def fake_tunnel(self, directory, *, lifetime=20):
        script = directory / "fake-cloudflared.py"
        script.write_text("import time\nprint('https://test-store.trycloudflare.com',flush=True)\n"
                          "print('Registered tunnel connection',flush=True)\n"
                          f"time.sleep({lifetime})\n")
        original_popen = subprocess.Popen
        return mock.patch.object(local_tunnel.subprocess, "Popen",
                                 side_effect=lambda *args, **kwargs: original_popen([sys.executable, str(script)], **kwargs))

    def test_stop_reaps_child_and_removes_public_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            hosts = set()
            tunnel = local_tunnel.LocalTunnel("http://127.0.0.1:1234", True, hosts)
            with mock.patch.object(local_tunnel, "ensure_cloudflared", return_value=Path("unused")), self.fake_tunnel(Path(temporary)):
                try:
                    tunnel.start()
                    wait_until(lambda: tunnel.snapshot()["state"] == "ready")
                    self.assertIn("test-store.trycloudflare.com", hosts)
                finally:
                    tunnel.stop()
                self.assertIsNotNone(tunnel.process.poll())
                self.assertNotIn("test-store.trycloudflare.com", hosts)
                self.assertIsNone(tunnel.snapshot()["publicUrl"])

    def test_child_exit_invalidates_public_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            tunnel = local_tunnel.LocalTunnel("http://127.0.0.1:1234", True, set())
            with mock.patch.object(local_tunnel, "ensure_cloudflared", return_value=Path("unused")), self.fake_tunnel(Path(temporary), lifetime=0.2):
                try:
                    tunnel.start()
                    wait_until(lambda: tunnel.snapshot()["state"] == "ready")
                    wait_until(lambda: tunnel.snapshot()["state"] == "failed")
                    self.assertIsNone(tunnel.snapshot()["publicUrl"])
                    self.assertNotIn("test-store.trycloudflare.com", tunnel.allowed_hosts)
                finally:
                    tunnel.stop()

    def test_stop_during_binary_download_does_not_spawn_later(self):
        entered, release = threading.Event(), threading.Event()

        def slow_download():
            entered.set()
            release.wait(timeout=5)
            return Path("unused")

        tunnel = local_tunnel.LocalTunnel("http://127.0.0.1:1234", True, set())
        with mock.patch.object(local_tunnel, "ensure_cloudflared", side_effect=slow_download), mock.patch.object(local_tunnel.subprocess, "Popen") as popen:
            try:
                tunnel.start()
                self.assertTrue(entered.wait(timeout=2))
                tunnel.stop()
            finally:
                release.set()
                tunnel._thread.join(timeout=2)
            popen.assert_not_called()
            self.assertEqual(tunnel.snapshot()["state"], "stopped")


if __name__ == "__main__":
    unittest.main()

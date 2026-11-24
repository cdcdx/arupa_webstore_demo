import concurrent.futures
import http.client
import io
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
import zipfile
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
    def test_http_requests_share_cache_and_invalidate_errors_replacements_and_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); package = root / "demo.crx"
            original = (server.PACKAGES / "nomad-demo-1.0.crx").read_bytes()
            package.write_bytes(original)
            httpd = server.DemoServer(("127.0.0.1", 0), root)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()

            def get(route):
                connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
                try:
                    connection.request("GET", route); response = connection.getresponse()
                    return response.status, response.read()
                finally: connection.close()

            try:
                with mock.patch.object(crx_catalog, "read_package", wraps=crx_catalog.read_package) as reader:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                        responses = list(pool.map(get, ["/api/catalog?platform=desktop", "/api/catalog?platform=android"] * 3))
                    self.assertTrue(all(code == 200 for code, _ in responses))
                    self.assertEqual(reader.call_count, 1, "one shared parse across concurrent HTTP requests and platforms")
                    self.assertEqual(get("/api/package?id=" + crx_catalog.crx_id(original)), (200, original))
                    self.assertEqual(reader.call_count, 1, "download reuses metadata but still verifies file bytes")
                    replacement = root / "new.tmp"
                    replacement.write_bytes((server.ROOT / "samples/nomad-demo-1.1.crx").read_bytes()); replacement.replace(package)
                    self.assertEqual(json.loads(get("/api/catalog")[1])["packages"][0]["version"], "1.1")
                    self.assertEqual(reader.call_count, 2)
                    package.write_bytes(b"invalid package")
                    for _ in range(2):
                        payload = json.loads(get("/api/catalog")[1])
                        self.assertEqual(payload["packages"], []); self.assertEqual(len(payload["errors"]), 1)
                    self.assertEqual(reader.call_count, 3, "invalid packages are cached until their file changes")
                    package.write_bytes(original)
                    self.assertEqual(json.loads(get("/api/catalog")[1])["packages"][0]["version"], "1.0")
                    self.assertEqual(reader.call_count, 4)
                    package.unlink()
                    self.assertEqual(json.loads(get("/api/catalog")[1]), {"packages": [], "errors": []})
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()

    def test_cached_regular_file_cannot_be_replaced_with_a_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / "demo.crx"
            path.write_bytes((server.PACKAGES / "nomad-demo-1.0.crx").read_bytes())
            index = crx_catalog.CatalogIndex(root)
            self.assertEqual(len(index.get()[0]), 1)
            target = root / "moved.tmp"; path.replace(target)
            try: path.symlink_to(target)
            except OSError: self.skipTest("symlink creation unavailable")
            packages, errors = index.get()
            self.assertEqual(packages, []); self.assertEqual(len(errors), 1)

    def test_download_uses_bounded_reads_known_and_unknown_length_and_closes_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / "demo.crx"
            # Structural fixture for HTTP transfer; native signing is tested separately.
            memory = io.BytesIO((server.PACKAGES / "nomad-demo-1.0.crx").read_bytes())
            with zipfile.ZipFile(memory, "a") as archive:
                archive.writestr("transfer-fixture.bin", b"x" * (server.PACKAGE_CHUNK_BYTES * 4))
            raw = memory.getvalue(); path.write_bytes(raw)
            httpd = server.DemoServer(("127.0.0.1", 0), root, package_delay=0.01)
            item, = httpd.catalog.get()[0]
            streams, reads = [], []
            original_open = Path.open
            case = self

            class BoundedFile:
                def __init__(self, stream): self.stream = stream
                def __getattr__(self, name): return getattr(self.stream, name)
                def __enter__(self): return self
                def __exit__(self, *args): self.stream.close()
                def read(self, size=-1):
                    case.assertGreater(size, 0, "unbounded file reads are forbidden")
                    case.assertLessEqual(size, server.PACKAGE_CHUNK_BYTES)
                    reads.append((self.stream.tell(), size))
                    return self.stream.read(size)

            def open_file(candidate, *args, **kwargs):
                stream = original_open(candidate, *args, **kwargs)
                if candidate == path and args == ("rb",):
                    streams.append(stream); return BoundedFile(stream)
                return stream

            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            try:
                with mock.patch.object(Path, "open", open_file), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file download")):
                    for without_length in (False, True):
                        httpd.package_without_length = without_length
                        connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
                        try:
                            connection.request("GET", f"/api/package?id={item.extension_id}&sha256={item.sha256}")
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.getheader("Content-Length"), None if without_length else str(len(raw)))
                            received = bytearray()
                            while chunk := response.read(8192): received.extend(chunk)
                            self.assertEqual(bytes(received), raw)
                        finally: connection.close()
                    connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
                    connection.request("GET", "/api/package?id=" + item.extension_id)
                    response = connection.getresponse(); response.read(1); connection.close()
                    wait_until(lambda: all(stream.closed for stream in streams))
                self.assertEqual(len(streams), 3)
                self.assertGreaterEqual(sum(position == 0 for position, _ in reads), 6, "same open file is verified then rewound for each transfer")
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()

    def test_file_changed_after_catalog_lookup_is_rejected_before_success_headers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / "demo.crx"
            original = (server.PACKAGES / "nomad-demo-1.0.crx").read_bytes(); path.write_bytes(original)
            httpd = server.DemoServer(("127.0.0.1", 0), root)
            original_send = server.Handler.send_package
            def change_before_verification(handler, package):
                path.write_bytes(b"x" * len(original))
                return original_send(handler, package)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
            try:
                with mock.patch.object(server.Handler, "send_package", change_before_verification):
                    connection.request("GET", "/api/package?id=" + crx_catalog.crx_id(original))
                    response = connection.getresponse()
                    self.assertEqual(response.status, 409); self.assertEqual(response.read(), b"package_changed")
            finally:
                connection.close(); httpd.shutdown(); httpd.server_close(); thread.join()

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

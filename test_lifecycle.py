import concurrent.futures
import hashlib
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
from urllib.parse import unquote
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


class RangeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "demo.crx"
        # Structural fixture to exercise transfer boundaries, not native signature verification.
        memory = io.BytesIO((server.PACKAGES / "nomad-demo-1.0.crx").read_bytes())
        with zipfile.ZipFile(memory, "a") as archive:
            archive.writestr("range-fixture.bin", bytes(range(256)) * 1024)
        self.raw = memory.getvalue()
        self.path.write_bytes(self.raw)
        self.httpd = server.DemoServer(("127.0.0.1", 0), self.root)
        self.item, = self.httpd.catalog.get()[0]
        self.route = "/api/package?id=" + self.item.extension_id
        self.etag = '"' + self.item.sha256 + '"'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        patch = mock.patch.object(server.Handler, "log_message")
        patch.start()
        self.addCleanup(patch.stop)

    def stop_server(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.thread.join()

    def request(self, headers=None, method="GET", route=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            if isinstance(headers, list):
                connection.putrequest(method, route or self.route)
                for name, value in headers:
                    connection.putheader(name, value)
                connection.endheaders()
            else:
                connection.request(method, route or self.route, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_full_download_and_head_advertise_same_strong_validator(self):
        status, headers, body = self.request()
        self.assertEqual((status, body), (200, self.raw))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["ETag"], self.etag)
        self.assertEqual(headers["Content-Length"], str(len(self.raw)))
        self.assertNotIn("Content-Range", headers)
        self.assertIn("no-transform", headers["Cache-Control"])
        for extra in ({}, {"Range": "bytes=3-9"}, {"Range": "bytes=999999999-"}):
            with self.subTest(headers=extra):
                status, head, body = self.request(extra, method="HEAD")
                self.assertEqual((status, body), (200, b""))
                for key in ("Accept-Ranges", "ETag", "Content-Length"):
                    self.assertEqual(head[key], headers[key])
                self.assertNotIn("Content-Range", head)

    def test_manual_download_is_an_attachment_and_preserves_range_and_raw_install(self):
        route = self.route + "&download=1"
        for without_length in (False, True):
            with self.subTest(without_length=without_length):
                self.httpd.package_without_length = without_length
                status, headers, body = self.request(route=route)
                self.assertEqual((status, body), (200, self.raw))
                self.assertEqual(headers["Content-Type"], "application/octet-stream")
                self.assertEqual(headers["Content-Disposition"], 'attachment; filename="demo.crx"; filename*=UTF-8\'\'demo.crx')
                self.assertEqual(headers["ETag"], self.etag)
                self.assertEqual(headers.get("Content-Length"), None if without_length else str(len(self.raw)))
                status, headers, body = self.request({"Range": "bytes=3-19", "If-Range": self.etag}, route=route)
                self.assertEqual((status, body), (206, self.raw[3:20]))
                self.assertEqual(headers["Content-Length"], "17")
                self.assertEqual(headers["Content-Range"], f"bytes 3-19/{len(self.raw)}")
                self.assertIn("attachment", headers["Content-Disposition"])
                status, headers, body = self.request(route=route, method="HEAD")
                self.assertEqual((status, body), (200, b""))
                self.assertIn("attachment", headers["Content-Disposition"])
                status, headers, body = self.request({"Range": "bytes=999999999-"}, route=route)
                self.assertEqual(status, 416)
                self.assertNotIn("Content-Disposition", headers, "never save an error response as a CRX")
        for suffix in ("", "&download=0"):
            status, headers, body = self.request(route=self.route + suffix)
            self.assertEqual((status, body), (200, self.raw))
            self.assertEqual(headers["Content-Type"], "application/x-chrome-extension")
            self.assertNotIn("Content-Disposition", headers)
        for suffix in ("&download=2", "&download=", "&download=1&download=0"):
            status, headers, body = self.request(route=self.route + suffix)
            self.assertEqual(status, 400)
            self.assertNotIn("Content-Disposition", headers)
        status, headers, body = self.request(route=route + "&sha256=" + "0" * 64)
        self.assertEqual(status, 409)
        self.assertNotIn("Content-Disposition", headers)

    def test_attachment_filename_is_utf8_and_cannot_inject_headers(self):
        for filename in ('扩展 "测试".crx', '扩展\r\nX-Injected: yes.crx'):
            with self.subTest(filename=filename):
                new_path = self.root / filename
                self.path.replace(new_path); self.path = new_path
                status, headers, body = self.request(route=self.route + "&download=1")
                self.assertEqual((status, body), (200, self.raw))
                disposition = headers["Content-Disposition"]
                self.assertEqual(unquote(disposition.split("filename*=UTF-8''")[1]), filename)
                self.assertTrue(disposition.isascii())
                self.assertNotIn("\r", disposition); self.assertNotIn("\n", disposition)
                self.assertNotIn("X-Injected", headers)

    def test_single_ranges_return_exact_inclusive_slices_and_lengths(self):
        size = len(self.raw)
        cases = [("bytes=0-15", 0, 15), ("bytes=65535-65537", 65535, 65537),
                 (f"bytes={size - 7}-999999999", size - 7, size - 1),
                 ("bytes=100-", 100, size - 1), ("bytes=-17", size - 17, size - 1),
                 ("bytes=-999999999", 0, size - 1), ("bytes=0-", 0, size - 1),
                 ("bytes=0000002-0000004", 2, 4),
                 ("bytes=3-" + "9" * 5000, 3, size - 1),
                 ("bytes=-" + "9" * 5000, 0, size - 1)]
        for field, start, end in cases:
            with self.subTest(range=field[:50]):
                status, headers, body = self.request({"Range": field})
                self.assertEqual(status, 206)
                self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{size}")
                self.assertEqual(headers["Content-Length"], str(end - start + 1))
                self.assertEqual(headers["ETag"], self.etag)
                self.assertEqual(headers["Accept-Ranges"], "bytes")
                self.assertEqual(body, self.raw[start:end + 1])

    def test_unsatisfiable_ranges_return_416_with_current_full_size(self):
        size = len(self.raw)
        for field in (f"bytes={size}-", f"bytes={size + 1}-{size + 10}",
                      "bytes=-0", "bytes=" + "9" * 5000 + "-"):
            with self.subTest(range=field[:50]):
                status, headers, body = self.request({"Range": field})
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], f"bytes */{size}")
                self.assertEqual(headers["ETag"], self.etag)
                self.assertEqual(headers["Content-Length"], str(len(body)))
                self.assertEqual(body, b"range_not_satisfiable")

    def test_malformed_unknown_and_multiple_ranges_are_ignored(self):
        for field in ("items=1-2", "bytes=", "bytes=-", "bytes=9-3", "bytes=abc-",
                      "bytes=0-1,3-4", "bytes= 1-2", "bytes=+1-2", "bytes=1.5-2"):
            with self.subTest(range=field):
                status, headers, body = self.request({"Range": field})
                self.assertEqual((status, body), (200, self.raw))
                self.assertNotIn("Content-Range", headers)
        status, headers, body = self.request([("Range", "bytes=0-1"), ("Range", "bytes=3-4")])
        self.assertEqual((status, body), (200, self.raw))
        self.assertNotIn("Content-Range", headers)

    def test_if_range_only_resumes_with_one_matching_strong_etag(self):
        status, headers, body = self.request({"Range": "bytes=123-", "If-Range": self.etag})
        self.assertEqual((status, body), (206, self.raw[123:]))
        self.assertEqual(headers["Content-Range"], f"bytes 123-{len(self.raw) - 1}/{len(self.raw)}")
        for validator in ('"older-package"', "W/" + self.etag, "Wed, 25 Nov 2026 00:00:00 GMT", ""):
            for field in ("bytes=123-", "bytes=999999999-"):
                with self.subTest(validator=validator, range=field):
                    status, headers, body = self.request({"Range": field, "If-Range": validator})
                    self.assertEqual((status, body), (200, self.raw))
                    self.assertNotIn("Content-Range", headers)
        status, _, body = self.request([("Range", "bytes=123-"), ("If-Range", self.etag), ("If-Range", self.etag)])
        self.assertEqual((status, body), (200, self.raw))

    def test_interrupted_download_resumes_in_both_length_modes_and_hashes_match(self):
        for without_length in (False, True):
            with self.subTest(without_length=without_length):
                self.httpd.package_without_length = without_length
                self.httpd.package_delay = 0.005
                connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
                try:
                    connection.request("GET", self.route)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Length"), None if without_length else str(len(self.raw)))
                    prefix = response.read(70001)
                    self.assertEqual(len(prefix), 70001)
                    etag = response.getheader("ETag")
                    response.close()
                finally:
                    connection.close()
                status, headers, tail = self.request({"Range": f"bytes={len(prefix)}-", "If-Range": etag})
                self.assertEqual(status, 206)
                self.assertEqual(headers["Content-Length"], str(len(self.raw) - len(prefix)))
                self.assertEqual(headers["Content-Range"], f"bytes {len(prefix)}-{len(self.raw) - 1}/{len(self.raw)}")
                self.assertEqual(prefix + tail, self.raw)
                self.assertEqual(hashlib.sha256(prefix + tail).hexdigest(), self.item.sha256)

    def test_range_uses_verified_descriptor_across_atomic_replace(self):
        replacement = self.root / "replacement.tmp"
        replacement.write_bytes((server.ROOT / "samples/nomad-demo-1.1.crx").read_bytes())
        original_send = server.Handler.send_headers

        def replace_before_headers(handler, code, content_type, length, extra_headers=None):
            if content_type == "application/x-chrome-extension":
                replacement.replace(self.path)
            return original_send(handler, code, content_type, length, extra_headers)

        with mock.patch.object(server.Handler, "send_headers", replace_before_headers):
            status, headers, body = self.request({"Range": "bytes=12-100"})
        self.assertEqual((status, body), (206, self.raw[12:101]))
        self.assertEqual(headers["ETag"], self.etag)
        # A new request sees the new package; it cannot append its bytes to the old partial file.
        updated = self.path.read_bytes()
        status, headers, body = self.request({"Range": "bytes=12-", "If-Range": self.etag})
        self.assertEqual((status, body), (200, updated))
        self.assertNotEqual(headers["ETag"], self.etag)
        status, headers, body = self.request({"Range": "bytes=12-"}, route=self.route + "&sha256=" + self.item.sha256)
        self.assertEqual((status, body), (409, b"package_changed"))
        self.assertNotIn("Content-Range", headers)


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
                    try:
                        connection.request("GET", "/api/package?id=" + item.extension_id,
                                           headers={"Range": "bytes=70001-"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 206)
                        self.assertEqual(response.getheader("Content-Length"), str(len(raw) - 70001))
                        self.assertEqual(response.read(), raw[70001:])
                    finally: connection.close()
                    connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
                    connection.request("GET", "/api/package?id=" + item.extension_id)
                    response = connection.getresponse(); response.read(1); connection.close()
                    wait_until(lambda: all(stream.closed for stream in streams))
                self.assertEqual(len(streams), 4)
                self.assertGreaterEqual(sum(position == 0 for position, _ in reads), 7, "same open file is verified then positioned for each transfer")
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

            def replace_after_verification(handler, code, content_type, length, extra_headers=None):
                if content_type == "application/x-chrome-extension":
                    replacement = root / "replacement.tmp"
                    replacement.write_bytes(b"different file")
                    replacement.replace(package)
                return send_headers(handler, code, content_type, length, extra_headers)

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

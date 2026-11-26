import tempfile
import unittest
from urllib.parse import urlencode
from unittest import mock
import shutil
import struct
import subprocess
import http.client
import io
import json
import threading
import zipfile
from pathlib import Path

import crx_catalog
import server


class LocalCrxCatalogTests(unittest.TestCase):

    def mivpn_catalog_fixture(self, *, metadata=True, signature_size=64, ordinary=False,
                             platform="desktop", version="0.0.27", product="mivpn",
                             stem="META-INF/mivpn-package", extension_id=None):
        # Structural catalog fixture only. Signature verification belongs to the browser.
        original = (server.ROOT / "packages" / "nomad-demo-1.0.crx").read_bytes()
        prefix = original[:12 + struct.unpack_from("<I", original, 8)[0]]
        manifest = {"manifest_version": 3, "name": "MiVPN catalog fixture", "version": version,
                    "key": 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzy0BFXToH46UMXZztwgClKb8Xb6WFn2dJtcrwfu+oCZSeZ/9rID8PWarTFUCDqRT5qAjZ6cHKYN5noj4ZjdF4z4iktxkB849CGDyfJMhwmolfXjObGgdAril5oNxvorOaiEByGbzlvR5xHTTWPjQoCLOeBPzJASg0l4NnK/T1QVK9cBRDsUlt0Ch8XEEGy40PUVZJSOG4MrGp/4ct2Wf6cwYVQ92d7lxoyD0HdN75j9O0iIk7XkppjYh2gTNYAI8Gf5kRyc57Pv/7bJDaT/3TlbKo7rPyBtBZytJsnvc7j3FutKNB0ZLuF/ufxWDmrqioZoFCFHPT4/vq9S1JHGgDwIDAQAB'}
        if ordinary:
            manifest.pop("key")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            if platform in ("desktop", "both"):
                archive.writestr("engine/mac-arm64/mivpn-browser.dylib", b"fixture")
            if platform in ("android", "both"):
                archive.writestr("engine/classes.dex", b"fixture")
            archive.writestr("engine/nodes.json", b"{}")
            if metadata:
                archive.writestr(stem + ".json", json.dumps({
                    "schema": 1, "product": product, "extensionId": extension_id or crx_catalog.MIVPN_ID,
                    "version": manifest["version"]}))
                archive.writestr(stem + ".sig", bytes(signature_size))
        return prefix + stream.getvalue()

    def test_platform_hint_does_not_hide_newest_same_id_package(self):
        identity = crx_catalog.crx_id((server.PACKAGES / "nomad-demo-1.0.crx").read_bytes())
        cases = [("mivpn-desktop", "desktop", "1.0"), ("mivpn-android", "android", "2.0"),
                 ("mivpn-ios", "ios", "3.0")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); raw = {}
            for product, platform, version in cases:
                raw[platform] = self.mivpn_catalog_fixture(ordinary=True, platform=platform,
                    product=product, version=version, extension_id=identity, stem="META-INF/nomad-package")
                (root / (platform + ".crx")).write_bytes(raw[platform])
            httpd = server.DemoServer(("127.0.0.1", 0), root)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
            try:
                for platform in ("desktop", "android", "ios"):
                    connection.request("GET", "/api/catalog?platform=" + platform)
                    response = connection.getresponse(); self.assertEqual(response.status, 200)
                    item, = json.loads(response.read())["packages"]
                    self.assertEqual((item["version"], item["platforms"]), ("3.0", ["ios"]))
                    connection.request("GET", f'/api/package?id={identity}&platform={platform}&sha256={item["sha256"]}')
                    response = connection.getresponse(); self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), raw["ios"])
                    old = crx_catalog.read_package(root / "desktop.crx")
                    connection.request("GET", f'/api/package?id={identity}&platform={platform}&sha256={old.sha256}')
                    response = connection.getresponse(); self.assertEqual(response.status, 409); response.read()
            finally:
                connection.close(); httpd.shutdown(); httpd.server_close(); thread.join()

    def test_engine_routing_is_independent_of_product_name(self):
        identity = crx_catalog.crx_id((server.PACKAGES / "nomad-demo-1.0.crx").read_bytes())
        for stem in ("META-INF/nomad-package", "META-INF/mivpn-package"):
            for platform, expected in [("desktop", ("desktop",)), ("android", ("android",)),
                                       ("both", ("desktop", "android"))]:
                with self.subTest(stem=stem, platform=platform):
                    raw = self.mivpn_catalog_fixture(ordinary=True, stem=stem, platform=platform,
                        product="publisher-renamed-addon", extension_id=identity)
                    self.assertEqual(crx_catalog.signed_package_metadata(raw, crx_catalog.extract_manifest(raw)),
                                     ("publisher-renamed-addon", expected))

    def test_ios_package_is_visible_and_downloadable_on_every_platform(self):
        identity = crx_catalog.crx_id((server.PACKAGES / "nomad-demo-1.0.crx").read_bytes())
        raw = self.mivpn_catalog_fixture(ordinary=True, product="mivpn-ios", platform="ios",
            extension_id=identity, stem="META-INF/nomad-package")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / "ios.crx").write_bytes(raw)
            httpd = server.DemoServer(("127.0.0.1", 0), root)
            for platform in (None, "desktop", "android", "ios"):
                packages, errors = httpd.catalog.get(platform)
                self.assertEqual(errors, []); self.assertEqual(len(packages), 1)
                self.assertEqual(packages[0].platforms, ("ios",))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
            try:
                for platform in (None, "desktop", "android", "ios"):
                    suffix = "" if platform is None else "?platform=" + platform
                    connection.request("GET", "/api/catalog" + suffix)
                    response = connection.getresponse(); self.assertEqual(response.status, 200)
                    item, = json.loads(response.read())["packages"]
                    self.assertEqual(item["signedProduct"], "mivpn-ios")
                    suffix = "" if platform is None else "&platform=" + platform
                    connection.request("GET", f'/api/package?id={identity}&sha256={item["sha256"]}' + suffix)
                    response = connection.getresponse(); self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), raw)
            finally:
                connection.close(); httpd.shutdown(); httpd.server_close(); thread.join()

    def test_global_version_selection_and_download_pinning_ignore_platform(self):
        desktop = self.mivpn_catalog_fixture(version="0.0.34")
        android = self.mivpn_catalog_fixture(platform="android", version="0.0.48")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "desktop.crx").write_bytes(desktop)
            (root / "android.crx").write_bytes(android)
            httpd = server.DemoServer((server.HOST, 0), root)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address)
            try:
                for platform in ("desktop", "android", "ios"):
                    connection.request("GET", "/api/catalog?platform=" + platform)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    package, = json.loads(response.read())["packages"]
                    self.assertEqual((package["version"], package["signedProduct"], package["platforms"]),
                                     ("0.0.48", "mivpn", ["android"]))
                    route = f'/api/package?id={package["id"]}&platform={platform}&sha256={package["sha256"]}'
                    connection.request("GET", route)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), android)
                    connection.request("GET", route.replace(package["sha256"], "0" * 64))
                    response = connection.getresponse()
                    self.assertEqual(response.status, 409)
                    self.assertEqual(response.read(), b"package_changed")
                for route in ("/api/catalog?platform=watchos", "/api/catalog?platform=desktop&platform=android",
                              f"/api/package?id={crx_catalog.MIVPN_ID}&sha256=invalid"):
                    connection.request("GET", route)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    response.read()
            finally:
                connection.close()
                httpd.shutdown(); httpd.server_close(); thread.join()

    def test_general_publisher_metadata_is_a_hint_not_a_signature_verdict(self):
        original = (server.ROOT / "packages" / "nomad-demo-1.0.crx").read_bytes()
        prefix = original[:12 + struct.unpack_from("<I", original, 8)[0]]
        manifest = crx_catalog.extract_manifest(original)
        metadata = {"schema": 1, "product": "demo-addon", "extensionId": crx_catalog.crx_id(original),
                    "version": manifest["version"]}
        for signature_size, product in ((64, "demo-addon"), (63, None)):
            memory = io.BytesIO()
            with zipfile.ZipFile(memory, "w") as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                archive.writestr("META-INF/nomad-package.json", json.dumps(metadata))
                archive.writestr("META-INF/nomad-package.sig", bytes(signature_size))
            self.assertEqual(crx_catalog.signed_package_metadata(prefix + memory.getvalue(), manifest),
                             (product, crx_catalog.PLATFORMS))

    def test_mivpn_uses_inner_install_id_and_http_preserves_crx_bytes(self):
        raw = self.mivpn_catalog_fixture()
        self.assertNotEqual(crx_catalog.crx_id(raw), crx_catalog.MIVPN_ID)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mivpn.crx").write_bytes(raw)
            packages, errors = crx_catalog.catalog(root)
            self.assertEqual(errors, [])
            self.assertEqual([p.extension_id for p in packages], [crx_catalog.MIVPN_ID])
            httpd = server.DemoServer((server.HOST, 0), root)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(*httpd.server_address)
                connection.request("GET", "/api/catalog")
                response = connection.getresponse()
                self.assertEqual(json.loads(response.read())["packages"][0]["id"], crx_catalog.MIVPN_ID)
                connection.request("GET", "/api/package?id=" + crx_catalog.MIVPN_ID)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), raw)
                connection.request("GET", "/api/package?id=" + crx_catalog.crx_id(raw))
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
                connection.close()
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()

    def test_mivpn_requires_signed_payload_metadata_for_identity_mapping(self):
        for options in ({"metadata": False}, {"signature_size": 63}):
            with self.subTest(options=options):
                raw = self.mivpn_catalog_fixture(**options)
                with self.assertRaisesRegex(ValueError, "mivpn_signed_payload_required"):
                    crx_catalog.install_id(raw, crx_catalog.extract_manifest(raw))

    def test_mivpn_named_metadata_cannot_remap_an_ordinary_extension(self):
        raw = self.mivpn_catalog_fixture(ordinary=True)
        self.assertEqual(crx_catalog.install_id(raw, crx_catalog.extract_manifest(raw)), crx_catalog.crx_id(raw))

    def test_documentation_is_served_as_a_static_page(self):
        filename, content_type = server.ASSETS["/docs"]
        self.assertEqual(content_type, "text/html; charset=utf-8")
        page = (server.ROOT / filename).read_text(encoding="utf-8")
        for method in ("beginInstallWithManifest3", "completeInstall", "checkUpdate", "applyUpdate", "management.uninstall"):
            self.assertIn(method, page)

    def test_bundled_crx_identity_and_manifest(self):
        first = crx_catalog.read_package(server.ROOT / "packages" / "nomad-demo-1.0.crx")
        second = crx_catalog.read_package(server.ROOT / "samples" / "nomad-demo-1.1.crx")
        self.assertEqual(first.extension_id, second.extension_id)
        self.assertTrue(crx_catalog.valid_extension_id(first.extension_id))
        self.assertEqual((first.version, second.version), ("1.0", "1.1"))
        self.assertEqual(len(first.sha256), 64)

    def test_catalog_reports_active_response_stage_rules_without_rejecting_package(self):
        rules = [{"condition": {"responseHeaders": [{"header": f"x-test-{index}"}]}} for index in range(5)]
        manifest = {"declarative_net_request": {"rule_resources": [
            {"id": "active", "enabled": True, "path": "rules.json"}]}}
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w") as archive:
            archive.writestr("rules.json", json.dumps(rules))
        self.assertEqual(
            crx_catalog.active_rule_requirements(memory.getvalue(), manifest),
            {"responseStageRules": 5},
        )

    def test_diagnostic_ignores_disabled_rules_and_malformed_sources(self):
        manifest = {"declarative_net_request": {"rule_resources": [
            {"id": "active", "enabled": True, "path": "active.json"},
            {"id": "disabled", "enabled": False, "path": "disabled.json"}]}}
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w") as archive:
            archive.writestr("active.json", json.dumps([{"condition": {"responseHeaders": [{"header": "x-test"}]}}]))
            archive.writestr("disabled.json", json.dumps([{"condition": {"responseHeaders": [{"header": "x-other"}]}}]))
        self.assertEqual(crx_catalog.active_rule_requirements(memory.getvalue(), manifest), {"responseStageRules": 1})
        manifest["declarative_net_request"]["rule_resources"][0]["path"] = "../active.json"
        self.assertEqual(crx_catalog.active_rule_requirements(memory.getvalue(), manifest), {})

    def test_catalog_picks_newest_package_for_same_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "old.crx").write_bytes((server.ROOT / "packages" / "nomad-demo-1.0.crx").read_bytes())
            (root / "new.crx").write_bytes((server.ROOT / "samples" / "nomad-demo-1.1.crx").read_bytes())
            (root / "broken.crx").write_bytes(b"not a CRX")
            packages, errors = crx_catalog.catalog(root)
            self.assertEqual(len(packages), 1)
            self.assertEqual(packages[0].version, "1.1")
            self.assertEqual(errors[0]["file"], "broken.crx")

    def test_all_files_catalog_and_explicit_file_download_preserve_id_only_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = (server.PACKAGES / "nomad-demo-1.0.crx").read_bytes()
            new = (server.ROOT / "samples/nomad-demo-1.1.crx").read_bytes()
            # Same ID, different versions, and identical bytes in a second file.
            data = {"旧版 1.0.crx": old, "a-new.crx": new, "b-duplicate.crx": new}
            for name, raw in data.items():
                (root / name).write_bytes(raw)
            identity = crx_catalog.crx_id(old)
            httpd = server.DemoServer(("127.0.0.1", 0), root)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()

            def request(route, headers=None, method="GET"):
                connection = http.client.HTTPConnection(*httpd.server_address, timeout=3)
                try:
                    connection.request(method, route, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            try:
                with mock.patch.object(crx_catalog, "read_package", wraps=crx_catalog.read_package) as reader:
                    for platform in ("desktop", "android", "ios"):
                        code, _, payload = request("/api/catalog?all=1&platform=" + platform)
                        self.assertEqual(code, 200)
                        items = json.loads(payload)["packages"]
                        self.assertEqual(len(items), 3)
                        self.assertEqual({p["id"] for p in items}, {identity})
                        self.assertEqual([p["file"] for p in items], ["a-new.crx", "b-duplicate.crx", "旧版 1.0.crx"])
                        self.assertEqual([p["isLatest"] for p in items], [True, False, False])
                    code, _, payload = request("/api/catalog")
                    item, = json.loads(payload)["packages"]
                    self.assertEqual(item["file"], "a-new.crx")
                    self.assertNotIn("isLatest", item)
                    self.assertEqual(request("/api/package?id=" + identity)[2], new)
                    for item in items:
                        route = "/api/package?" + urlencode({"id": identity, "file": item["file"], "sha256": item["sha256"]})
                        code, headers, body = request(route)
                        self.assertEqual((code, body), (200, data[item["file"]]))
                        self.assertEqual(headers["ETag"], '"' + item["sha256"] + '"')
                        code, headers, body = request(route, {"Range": "bytes=11-29"})
                        self.assertEqual((code, body), (206, data[item["file"]][11:30]))
                        self.assertEqual(headers["Content-Range"], f'bytes 11-29/{item["size"]}')
                        code, head, body = request(route, method="HEAD")
                        self.assertEqual((code, body), (200, b""))
                        self.assertEqual(head["ETag"], headers["ETag"])
                    self.assertEqual(reader.call_count, 3, "all/default catalogs and selected downloads share the metadata cache")
                old_item = next(p for p in items if p["version"] == "1.0")
                self.assertEqual(request(f'/api/package?id={identity}&sha256={old_item["sha256"]}')[0], 409,
                                 "legacy SHA pins the ID-only default, not another historical file")
                for selector in ({"file": "missing.crx"}, {"file": "a-new.crx", "id": "a" * 32}):
                    self.assertEqual(request("/api/package?" + urlencode({"id": identity, **selector}))[0], 404)
                for filename in ("../a-new.crx", "sub/a-new.crx", "sub\\a-new.crx", "\x00.crx", ""):
                    self.assertEqual(request("/api/package?" + urlencode({"id": identity, "file": filename}))[0], 400)
                self.assertEqual(request(f'/api/package?id={identity}&file=a-new.crx&file=b-duplicate.crx')[0], 400)
                self.assertEqual(request(f'/api/package?id={identity}&file=a-new.crx&sha256={old_item["sha256"]}')[0], 409)
                for query in ("all=2", "all=", "all=1&all=0"):
                    self.assertEqual(request("/api/catalog?" + query)[0], 400)
                route = "/api/package?" + urlencode({"id": identity, "file": old_item["file"], "sha256": old_item["sha256"]})
                replacement = root / "replace.tmp"; replacement.write_bytes(new); replacement.replace(root / old_item["file"])
                self.assertEqual(request(route)[0], 409, "replacing a selected file must not return bytes with the old digest")
                (root / old_item["file"]).unlink()
                self.assertEqual(request(route)[0], 404, "deleting one selected version must not silently select another")
                self.assertEqual(len(json.loads(request("/api/catalog?all=1")[2])["packages"]), 2)
            finally:
                httpd.shutdown(); httpd.server_close(); thread.join()

    def test_equivalent_version_default_is_deterministic_by_filename(self):
        from dataclasses import replace
        item = crx_catalog.read_package(server.PACKAGES / "nomad-demo-1.0.crx")
        shorter = replace(item, path=Path("a.crx"), version="1.0")
        longer = replace(item, path=Path("b.crx"), version="1.0.0")
        for items in ([shorter, longer], [longer, shorter]):
            self.assertEqual(crx_catalog.latest_by_id(items)[item.extension_id], shorter)

    def test_versions(self):
        self.assertEqual(crx_catalog.version_parts("1.2"), (1, 2))
        for value in ("0", "1.02", "1.2.3.4.5", "1.x", "65536"):
            self.assertIsNone(crx_catalog.version_parts(value))

    def test_invalid_identity_rejected(self):
        original = (server.ROOT / "packages" / "nomad-demo-1.0.crx").read_bytes()
        with self.assertRaises(ValueError):
            crx_catalog.crx_id(original[:12])
        with self.assertRaises(ValueError):
            crx_catalog.crx_id(b"bad!" + original[4:])

    def test_health_and_host_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            httpd = server.DemoServer((server.HOST, 0), Path(temporary))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            host, port = httpd.server_address
            try:
                connection = http.client.HTTPConnection(host, port)
                connection.request("GET", "/api/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read()),
                    {"status": "ok", "service": "arupa-webstore-demo"},
                )

                connection.request("GET", "/api/health", headers={"Host": "example.test"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()

    def test_sample_author_signature(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl unavailable")
        data = (server.ROOT / "packages" / "nomad-demo-1.0.crx").read_bytes()
        header_size = struct.unpack_from("<I", data, 8)[0]
        header = data[12:12 + header_size]
        proof = crx_catalog._proto_bytes(header, 2)
        public = crx_catalog._proto_bytes(proof, 1)
        signature = crx_catalog._proto_bytes(proof, 2)
        signed = crx_catalog._proto_bytes(header, 10000)
        message = b"CRX3 SignedData\0" + struct.pack("<I", len(signed)) + signed + data[12 + header_size:]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "public.der").write_bytes(public)
            (root / "signature.bin").write_bytes(signature)
            (root / "message.bin").write_bytes(message)
            subprocess.run(["openssl", "pkey", "-pubin", "-inform", "DER", "-in", str(root / "public.der"),
                            "-out", str(root / "public.pem")], check=True, capture_output=True)
            subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(root / "public.pem"),
                            "-signature", str(root / "signature.bin"), str(root / "message.bin")],
                           check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()

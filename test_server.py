import tempfile
import unittest
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

    def mivpn_catalog_fixture(self, *, metadata=True, signature_size=64, ordinary=False):
        # Structural catalog fixture only. Signature verification belongs to the browser.
        original = (server.ROOT / "packages" / "nomad-demo-1.0.crx").read_bytes()
        prefix = original[:12 + struct.unpack_from("<I", original, 8)[0]]
        manifest = {"manifest_version": 3, "name": "MiVPN catalog fixture", "version": "0.0.27",
                    "key": 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzy0BFXToH46UMXZztwgClKb8Xb6WFn2dJtcrwfu+oCZSeZ/9rID8PWarTFUCDqRT5qAjZ6cHKYN5noj4ZjdF4z4iktxkB849CGDyfJMhwmolfXjObGgdAril5oNxvorOaiEByGbzlvR5xHTTWPjQoCLOeBPzJASg0l4NnK/T1QVK9cBRDsUlt0Ch8XEEGy40PUVZJSOG4MrGp/4ct2Wf6cwYVQ92d7lxoyD0HdN75j9O0iIk7XkppjYh2gTNYAI8Gf5kRyc57Pv/7bJDaT/3TlbKo7rPyBtBZytJsnvc7j3FutKNB0ZLuF/ufxWDmrqioZoFCFHPT4/vq9S1JHGgDwIDAQAB'}
        if ordinary:
            manifest.pop("key")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            if metadata:
                archive.writestr("META-INF/mivpn-package.json", json.dumps({
                    "schema": 1, "product": "mivpn", "extensionId": crx_catalog.MIVPN_ID,
                    "version": manifest["version"]}))
                archive.writestr("META-INF/mivpn-package.sig", bytes(signature_size))
        return prefix + stream.getvalue()

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

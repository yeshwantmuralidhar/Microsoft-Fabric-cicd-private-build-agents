import base64
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from deploy_fabric import (
    FabricClient, NoRedirect, check_workspace, deploy, exchange, load_notebooks, main,
)


WORKSPACE = "12345678-1234-1234-1234-123456789abc"
OPERATION = "22345678-1234-1234-1234-123456789abc"


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.network_guard = patch(
            "urllib.request.OpenerDirector.open",
            side_effect=AssertionError("Live network calls are forbidden in unit tests."),
        )
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def test_definition_encodes_source_and_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "Example.Notebook"
            folder.mkdir()
            (folder / ".platform").write_text(json.dumps({
                "metadata": {"type": "Notebook", "displayName": "Real name"}
            }), encoding="utf-8")
            source = b"# Fabric notebook source\nprint('hello')"
            (folder / "notebook-content.py").write_bytes(source)
            name, _, definition = load_notebooks(temporary)[0]
            self.assertEqual(name, "Real name")
            self.assertEqual(definition["format"], "fabricGitSource")
            parts = {part["path"]: part for part in definition["parts"]}
            self.assertEqual(
                base64.b64decode(parts["notebook-content.py"]["payload"]), source
            )
            (Path(temporary) / "parameter.yml").touch()
            with self.assertRaisesRegex(ValueError, "not supported"):
                load_notebooks(temporary)

    def test_missing_source_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "No Notebook"):
                load_notebooks(temporary)

    def test_pagination(self):
        client = FabricClient(WORKSPACE)
        client.request = Mock(side_effect=[
            (200, {}, {"value": [{"id": "one"}], "continuationToken": "a+b="}),
            (200, {}, {"value": [{"id": "two"}]}),
        ])
        self.assertEqual(len(client.notebooks(WORKSPACE)), 2)
        self.assertIn("continuationToken=a%2Bb%3D", client.request.call_args.args[1])
        self.assertIn(".z12.w.api.fabric.microsoft.com", client.base)

    @patch("deploy_fabric.time.sleep")
    def test_async_publish_waits_for_success(self, sleep):
        client = FabricClient(WORKSPACE)
        client.request = Mock(side_effect=[
            (202, {"x-ms-operation-id": OPERATION, "Retry-After": "2"}, None),
            (200, {}, {"status": "Running"}),
            (200, {}, {"status": "Succeeded"}),
        ])
        client.publish("path", {})
        self.assertEqual(client.request.call_count, 3)
        sleep.assert_any_call(2)

    @patch("deploy_fabric.time.sleep")
    def test_failed_operation_fails_deployment(self, sleep):
        client = FabricClient(WORKSPACE)
        client.request = Mock(side_effect=[
            (202, {"x-ms-operation-id": OPERATION}, None),
            (200, {}, {"status": "Failed", "error": {"message": "invalid"}}),
        ])
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            client.publish("path", {})

    def test_create_update_and_verify(self):
        client = Mock()
        existing = {"id": OPERATION, "displayName": "Existing"}
        client.notebooks.side_effect = [
            [existing], [existing, {"displayName": "New"}]
        ]
        deploy(client, WORKSPACE, [
            ("Existing", "", {"parts": []}), ("New", "", {"parts": []})
        ])
        self.assertIn("updateDefinition?updateMetadata=true",
                      client.publish.call_args_list[0].args[0])
        self.assertEqual(client.publish.call_args_list[1].args[1]["displayName"], "New")

    def test_ambiguous_target_fails_before_writes(self):
        client = Mock()
        client.notebooks.return_value = [
            {"displayName": "Same"}, {"displayName": "same"}
        ]
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            deploy(client, WORKSPACE, [])
        client.publish.assert_not_called()

    @patch("deploy_fabric.exchange")
    def test_token_audience_and_cache(self, exchange):
        client = FabricClient(WORKSPACE)
        exchange.side_effect = [
            (200, {}, {"access_token": "not-a-real-token", "expires_on": 99999999999}),
            (200, {}, {"value": []}),
            (200, {}, {"value": []}),
        ]
        client.request("GET", "path")
        client.request("GET", "path")
        self.assertEqual(exchange.call_count, 3)
        metadata_call = exchange.call_args_list[0]
        self.assertIn("resource=https%3A%2F%2Fapi.fabric.microsoft.com",
                      metadata_call.args[2])
        self.assertEqual(metadata_call.args[3], {"Metadata": "true"})
        fabric_headers = exchange.call_args_list[1].args[3]
        self.assertEqual(fabric_headers["Authorization"], "Bearer not-a-real-token")

    def test_read_only_checks_workspace_without_publishing(self):
        client = Mock()
        client.request.return_value = (
            200, {}, {"id": WORKSPACE, "displayName": "Test"}
        )
        check_workspace(client, WORKSPACE)
        client.request.assert_called_once_with("GET", f"workspaces/{WORKSPACE}")
        client.publish.assert_not_called()
        client.request.return_value = (200, {}, {"id": OPERATION})
        with self.assertRaisesRegex(RuntimeError, "Unexpected workspace"):
            check_workspace(client, WORKSPACE)

    @patch("deploy_fabric.exchange")
    def test_expired_token_is_refreshed(self, exchange):
        client = FabricClient(WORKSPACE)
        client.token = "expired"
        client.expires_on = 0
        exchange.side_effect = [
            (200, {}, {"access_token": "new-token", "expires_on": 99999999999}),
            (200, {}, {"value": []}),
        ]
        client.request("GET", "path")
        self.assertEqual(client.token, "new-token")
        self.assertEqual(exchange.call_count, 2)

    def test_http_failure_exposes_status_and_request_id(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://example.invalid", 403, "Forbidden",
            {"requestId": "test-request"}, BytesIO(b'{"errorCode":"Forbidden"}'),
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 403; request ID: test-request"):
            exchange(opener, "GET", "https://example.invalid", {})

    @patch("deploy_fabric.time.monotonic", side_effect=[0, 1801])
    def test_operation_timeout_is_not_success(self, monotonic):
        client = FabricClient(WORKSPACE)
        client.request = Mock(return_value=(
            202, {"x-ms-operation-id": OPERATION}, None
        ))
        with self.assertRaisesRegex(TimeoutError, "may still be running"):
            client.publish("path", {})

    def test_redirect_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "redirect refused"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other")

    @patch("deploy_fabric.build_opener")
    def test_fabric_and_metadata_openers_disable_proxies(self, build_opener):
        FabricClient(WORKSPACE)
        self.assertEqual(build_opener.call_count, 2)
        for call in build_opener.call_args_list:
            self.assertEqual(call.args[0].proxies, {})
            self.assertIsInstance(call.args[1], NoRedirect)

    @patch("deploy_fabric.FabricClient")
    @patch("deploy_fabric.load_notebooks")
    @patch.dict("os.environ", {"FABRIC_WORKSPACE_ID": WORKSPACE}, clear=True)
    @patch("sys.argv", ["deploy_fabric.py", "--check"])
    def test_check_cli_does_not_load_or_publish_source(self, load, client_type):
        client_type.return_value.request.return_value = (
            200, {}, {"id": WORKSPACE, "displayName": "Test"}
        )
        main()
        load.assert_not_called()
        client_type.return_value.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()

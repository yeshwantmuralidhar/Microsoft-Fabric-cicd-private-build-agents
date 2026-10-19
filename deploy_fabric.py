"""Deploy standalone Fabric Git-format Notebooks using Azure VM managed identity.

Required environment variables:
    FABRIC_WORKSPACE_ID: Target workspace UUID.
    FABRIC_REPOSITORY_DIRECTORY: Directory containing *.Notebook folders;
        not required with --check (read-only workspace information).

Uses workspace-level private-link endpoints. Matches existing Notebooks by
display name, not Git logical ID. Does not delete items, run notebooks, remap
dependencies, apply parameter.yml, or reproduce Fabric workspace folder layouts.
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("HTTP redirect refused; verify the configured endpoint.")


def exchange(opener, method, url, headers, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=60) as response:
            raw = response.read()
            return (
                response.status,
                response.headers,
                json.loads(raw) if raw else None,
            )
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        request_id = error.headers.get("requestId", error.headers.get("x-ms-request-id"))
        raise RuntimeError(
            f"{method} {url}: HTTP {error.code}; "
            f"request ID: {request_id}; response: {body}"
        ) from None


class FabricClient:
    def __init__(self, workspace_id):
        compact = UUID(workspace_id).hex
        self.base = (
            f"https://{compact}.z{compact[:2]}.w.api.fabric.microsoft.com/v1/"
        )
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.metadata_opener = build_opener(ProxyHandler({}), NoRedirect())
        self.token = None
        self.expires_on = 0

    def request(self, method, path, payload=None):
        if self.token is None or time.time() >= self.expires_on - 120:
            query = urlencode({
                "api-version": "2018-02-01",
                "resource": "https://api.fabric.microsoft.com",
            })
            status, _, body = exchange(
                self.metadata_opener,
                "GET",
                f"http://169.254.169.254/metadata/identity/oauth2/token?{query}",
                {"Metadata": "true"},
            )
            if status != 200 or not body or not body.get("access_token"):
                raise RuntimeError("VM metadata endpoint did not return a token.")
            self.token = body["access_token"]
            self.expires_on = float(body["expires_on"])

        return exchange(
            self.opener,
            method,
            self.base + path,
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "x-ms-fabric-skill": "git-integration-operations-cli",
            },
            payload,
        )

    def notebooks(self, workspace_id):
        path = f"workspaces/{workspace_id}/notebooks"
        items = []
        seen = set()
        while True:
            status, _, body = self.request("GET", path)
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError("Unexpected Notebook list response.")
            items.extend(body["value"])
            token = body.get("continuationToken")
            if not token:
                return items
            if token in seen:
                raise RuntimeError("Notebook pagination returned a repeated token.")
            seen.add(token)
            path = (
                f"workspaces/{workspace_id}/notebooks?"
                + urlencode({"continuationToken": token})
            )

    def publish(self, path, payload):
        status, headers, _ = self.request("POST", path, payload)
        if status in (200, 201):
            return
        if status != 202:
            raise RuntimeError(f"Unexpected publish status: {status}")

        operation_id = str(UUID(headers["x-ms-operation-id"]))
        deadline = time.monotonic() + 1800
        while True:
            delay = max(1, int(headers.get("Retry-After", "5")))
            if time.monotonic() + delay >= deadline:
                raise TimeoutError(
                    f"Operation {operation_id} did not finish within 30 minutes. "
                    "It may still be running; check its state before rerunning."
                )
            time.sleep(delay)
            status, headers, body = self.request(
                "GET", f"operations/{operation_id}"
            )
            if status != 200 or not isinstance(body, dict):
                raise RuntimeError("Unexpected operation state response.")
            state = body.get("status")
            if state == "Succeeded":
                return
            if state not in ("NotStarted", "Running"):
                raise RuntimeError(
                    f"Operation {operation_id} ended with state {state}: "
                    f"{json.dumps(body.get('error'))}"
                )


def load_notebooks(directory):
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"Not a source directory: {directory}")
    if any(directory.rglob("parameter.yml")):
        raise ValueError(
            "parameter.yml is not supported by this REST deployment. "
            "Do not deploy parameterized definitions without implementing substitution."
        )

    notebooks = []
    names = set()
    for folder in sorted(directory.rglob("*.Notebook")):
        if not folder.is_dir():
            continue
        platform_file = folder / ".platform"
        metadata = json.loads(platform_file.read_text(encoding="utf-8-sig"))["metadata"]
        name = metadata["displayName"]
        if metadata["type"] != "Notebook" or not isinstance(name, str) or not name.strip():
            raise ValueError(f"Invalid Notebook metadata: {platform_file}")
        if name.casefold() in names:
            raise ValueError(f"Duplicate source Notebook display name: {name}")
        names.add(name.casefold())
        contents = [
            file for file in folder.iterdir()
            if file.name in {
                "notebook-content.py", "notebook-content.sql",
                "notebook-content.scala", "notebook-content.r",
            } and file.is_file()
        ]
        if len(contents) != 1:
            raise ValueError(f"Expected exactly one Git-format Notebook source in {folder}")
        files = [platform_file, contents[0]]
        for file in files:
            if file.is_symlink() or not file.resolve().is_relative_to(directory):
                raise ValueError(f"Source escapes the deployment directory: {file}")
        definition = {
            "format": "fabricGitSource",
            "parts": [
                {
                    "path": file.name,
                    "payload": base64.b64encode(file.read_bytes()).decode("ascii"),
                    "payloadType": "InlineBase64",
                }
                for file in files
            ],
        }
        notebooks.append((name, metadata.get("description", ""), definition))
    if not notebooks:
        raise ValueError(f"No Notebook definitions found in {directory}")
    return notebooks


def deploy(client, workspace_id, sources):
    existing = {}
    for item in client.notebooks(workspace_id):
        key = item["displayName"].casefold()
        if key in existing:
            raise ValueError(f"Ambiguous target Notebook name: {item['displayName']}")
        existing[key] = item

    for name, description, definition in sources:
        item = existing.get(name.casefold())
        if item:
            notebook_id = str(UUID(item["id"]))
            print(f"Updating Notebook: {name}", flush=True)
            client.publish(
                f"workspaces/{workspace_id}/notebooks/{notebook_id}"
                "/updateDefinition?updateMetadata=true",
                {"definition": definition},
            )
        else:
            print(f"Creating Notebook: {name}", flush=True)
            client.publish(
                f"workspaces/{workspace_id}/notebooks",
                {
                    "displayName": name,
                    "description": description,
                    "definition": definition,
                },
            )

    published = {}
    for item in client.notebooks(workspace_id):
        key = item["displayName"].casefold()
        if key in published:
            raise RuntimeError(f"Ambiguous target Notebook name: {item['displayName']}")
        published[key] = item
    for name, _, _ in sources:
        if name.casefold() not in published:
            raise RuntimeError(f"Published Notebook not found in target: {name}")
    print(f"Deployment completed: {len(sources)} Notebook(s).", flush=True)


def check_workspace(client, workspace_id):
    status, _, body = client.request("GET", f"workspaces/{workspace_id}")
    if status != 200 or not isinstance(body, dict) or body.get("id") != workspace_id:
        raise RuntimeError(f"Unexpected workspace response: HTTP {status}")
    print(f"PASS: HTTP 200 for workspace {body['displayName']} ({body['id']})",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Read workspace information without publishing.")
    args = parser.parse_args()
    workspace_id = str(UUID(os.environ["FABRIC_WORKSPACE_ID"]))
    print(f"Python: {sys.version}", flush=True)
    print(f"Target workspace: {workspace_id}", flush=True)
    client = FabricClient(workspace_id)
    if args.check:
        check_workspace(client, workspace_id)
    else:
        sources = load_notebooks(os.environ["FABRIC_REPOSITORY_DIRECTORY"])
        deploy(client, workspace_id, sources)


if __name__ == "__main__":
    main()

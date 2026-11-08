# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Helpers for calling a deployed LiteLLM proxy.

The tests run where Kubernetes Service and pod addresses are routable, as on
a MicroK8s node, so requests go straight to the proxy.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import jubilant

from defaults import PORT


@dataclass
class Response:
    status: int
    body: str

    def json(self):
        return json.loads(self.body)


def service_address(juju: jubilant.Juju, app: str) -> str:
    """The cluster address of the application's Kubernetes Service."""
    address = juju.status().apps[app].address
    assert address, f"{app} has no Service address"
    return address


def namespace(juju: jubilant.Juju) -> str:
    """The Kubernetes namespace of the model. juju.model may be "controller:model"."""
    return (juju.model or "").split(":")[-1]


def unit_message(juju: jubilant.Juju, unit: str) -> str:
    app = unit.split("/")[0]
    return juju.status().apps[app].units[unit].workload_status.message


def unit_address(juju: jubilant.Juju, unit: str) -> str:
    app = unit.split("/")[0]
    return juju.status().apps[app].units[unit].address


def _request(
    address: str,
    method: str,
    path: str,
    body: dict | None,
    *,
    key: str | None,
    headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    data = None if body is None else json.dumps(body).encode()
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    if key is not None:
        all_headers["Authorization"] = f"Bearer {key}"
    return urllib.request.Request(
        f"http://{address}:{PORT}{path}", data=data, method=method, headers=all_headers
    )


def request(
    address: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    key: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> Response:
    req = _request(address, method, path, body, key=key, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read().decode(errors="replace"))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read().decode(errors="replace"))


def chat(address: str, model: str, key: str | None, content: str = "Say hello.", **kw) -> Response:
    body = {"model": model, "messages": [{"role": "user", "content": content}]}
    return request(address, "POST", "/v1/chat/completions", body, key=key, **kw)


def answer(response: Response) -> str:
    assert response.status == 200, response.body
    return response.json()["choices"][0]["message"]["content"] or ""


def chat_stream(address: str, model: str, key: str, content: str = "Count to five.") -> list[str]:
    """Send a streaming request and return the content deltas in arrival order."""
    body = {
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": content}],
    }
    req = _request(address, "POST", "/v1/chat/completions", body, key=key)
    deltas: list[str] = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        assert resp.status == 200
        assert resp.headers.get_content_type() == "text/event-stream"
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            choices = json.loads(line.removeprefix("data:").strip()).get("choices") or []
            if choices and choices[0].get("delta", {}).get("content"):
                deltas.append(choices[0]["delta"]["content"])
    return deltas


def model_names(address: str, key: str) -> set[str]:
    response = request(address, "GET", "/v1/models", key=key)
    assert response.status == 200, response.body
    return {model["id"] for model in response.json()["data"]}


def wait_for(predicate, *, timeout: float = 300, interval: float = 5, what: str = "condition"):
    """Poll ``predicate`` until it returns a truthy value, and return that value."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if result := predicate():
                return result
        except (OSError, AssertionError, ValueError) as exc:
            last_error = exc
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}: {last_error}")


def workload_logs(juju: jubilant.Juju, unit: str) -> str:
    """The LiteLLM service's logs, read through Pebble in the workload container."""
    return juju.ssh(unit, "/charm/bin/pebble", "logs", "-n", "all", container="litellm")

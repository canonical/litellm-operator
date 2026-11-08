# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Helpers for calling a deployed LiteLLM proxy.

The tests run where Kubernetes Service and pod addresses are routable, as on
a MicroK8s node, so requests go straight to the proxy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

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


class Load:
    """Steady chat requests through the Service in a background thread.

    Use as a context manager. ``failed`` and ``total`` count the requests
    sent while it ran.
    """

    def __init__(self, address: str, model: str, key: str, rate: float = 2.0):
        self.address, self.model, self.key = address, model, key
        self.interval = 1 / rate
        self.total = 0
        self.failures: list[str] = []
        # Wall-clock times of failed requests, comparable with Kubernetes timestamps.
        self.failure_times: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            self.total += 1
            try:
                response = chat(self.address, self.model, self.key, timeout=30)
                if response.status != 200:
                    self.failures.append(f"{response.status}: {response.body[:200]}")
                    self.failure_times.append(started)
            except OSError as exc:
                self.failures.append(repr(exc))
                self.failure_times.append(started)
            self._stop.wait(max(0.0, self.interval - (time.time() - started)))

    @property
    def failure_seconds(self) -> float:
        """Seconds from the first failed request to the last."""
        if not self.failure_times:
            return 0.0
        return max(self.failure_times) - min(self.failure_times)

    def __repr__(self) -> str:
        return (
            f"Load({len(self.failures)}/{self.total} failed over "
            f"{self.failure_seconds:.0f}s: {self.failures[:3]})"
        )

    @property
    def failure_rate(self) -> float:
        return len(self.failures) / self.total if self.total else 0.0

    def __enter__(self) -> Load:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=60)


def workload_starts(namespace: str, app: str, container: str = "litellm") -> list[float]:
    """When each pod's workload container last started, as Unix times."""
    pods = json.loads(
        kubectl(
            "-n", namespace, "get", "pods", "-l", f"app.kubernetes.io/name={app}", "-o", "json"
        )
    )["items"]
    starts = []
    for pod in pods:
        for status in pod["status"].get("containerStatuses", []):
            started = status["state"].get("running", {}).get("startedAt")
            if status["name"] == container and started:
                starts.append(datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp())
    return starts


def failures_outside(load: Load, starts: list[float], seconds: float) -> list[str]:
    """Failed requests that are not within ``seconds`` of a workload starting (SC-008, SC-009).

    Kubernetes timestamps have a resolution of one second.
    """
    return [
        message
        for message, t in zip(load.failures, load.failure_times, strict=True)
        if not any(start - 1 <= t <= start + seconds for start in starts)
    ]


def kubectl(*args: str) -> str:
    """Run kubectl against the cluster the controller runs on (MicroK8s)."""
    command = ["kubectl"] if shutil.which("kubectl") else ["microk8s", "kubectl"]
    return subprocess.run([*command, *args], check=True, capture_output=True, text=True).stdout

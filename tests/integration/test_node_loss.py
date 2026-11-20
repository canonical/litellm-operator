# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Units on separate nodes, and the loss of one node: US-3 scenarios 6 and 7.

Runs only with MULTINODE=1, on the controller that
tests/integration/multinode/setup.sh bootstraps, from its first VM. The test
loses a node by taking the network of another VM down over SSH, with a
timer on that VM that brings it back.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import jubilant
import pytest

from conftest import APP, MASTER_KEY, Upstream, credentials
from helpers import Load, answer, chat, kubectl, namespace, service_address

pytestmark = pytest.mark.skipif(
    os.environ.get("MULTINODE") != "1", reason="needs MULTINODE=1 and a 3-node cluster"
)

VALKEY = "valkey"
OUTAGE_SECONDS = 240
RECOVERY_SECONDS = 90  # SC-013
OBSERVE_SECONDS = 5 * 60  # SC-013
MAX_FAILURE_RATE = 0.05  # SC-013


def _pods(namespace: str) -> list[dict]:
    return json.loads(kubectl("-n", namespace, "get", "pods", "-o", "json"))["items"]


def _nodes() -> dict[str, str]:
    """Node names to their internal addresses."""
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    return {
        node["metadata"]["name"]: next(
            a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP"
        )
        for node in nodes
    }


@pytest.fixture(scope="module")
def deployed(juju: jubilant.Juju, charm: Path, resources: dict[str, str], upstream: Upstream):
    primary = os.uname().nodename
    secret = juju.add_secret("litellm-credentials", credentials())
    # The store stays on this VM, which the test never cuts off.
    juju.deploy(
        VALKEY,
        channel="9/stable",
        trust=True,
        constraints={"tags": f"kubernetes.io/hostname={primary}"},
    )
    juju.deploy(
        charm,
        APP,
        num_units=3,
        resources=resources,
        config={"credentials-secret": secret, "models": upstream.models("chat")},
        constraints={
            "tags": f"anti-pod.app.kubernetes.io/name={APP},"
            "anti-pod.topology-key=kubernetes.io/hostname"
        },
    )
    juju.grant_secret(secret, APP)
    juju.integrate(APP, VALKEY)
    juju.wait(lambda status: jubilant.all_active(status, APP, VALKEY))
    return {"primary": primary}


def test_units_on_separate_nodes(juju: jubilant.Juju, deployed):
    """FR-027, US-3 scenario 6: each unit runs on its own node."""
    nodes = [
        pod["spec"]["nodeName"]
        for pod in _pods(namespace(juju))
        if pod["metadata"]["labels"].get("app.kubernetes.io/name") == APP
    ]
    assert len(nodes) == 3
    assert len(set(nodes)) == 3, nodes


def test_node_loss(juju: jubilant.Juju, deployed):
    """FR-015, SC-013, US-3 scenario 7: losing a node, then getting it back."""
    busy = {
        pod["spec"]["nodeName"]
        for pod in _pods(namespace(juju)) + _pods(f"controller-{juju.model.split(':')[0]}")
        # The model operators only matter for deploying, not for serving.
        if pod["metadata"]["labels"].get("app.kubernetes.io/name") != APP
        and not pod["metadata"]["name"].startswith("modeloperator")
    }
    nodes = _nodes()
    candidates = [name for name in nodes if name != deployed["primary"] and name not in busy]
    assert candidates, f"every worker hosts something besides {APP}: {busy}"
    address = nodes[candidates[0]]

    service = service_address(juju, APP)
    assert answer(chat(service, "chat", MASTER_KEY))
    cut = (
        "dev=$(ip route show default | awk '{print $5; exit}'); "
        f"systemd-run --on-active={OUTAGE_SECONDS} ip link set $dev up; "
        "systemd-run --on-active=2 ip link set $dev down"
    )
    with Load(service, "chat", MASTER_KEY) as load:
        time.sleep(10)
        subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", f"root@{address}", cut],
            check=True,
            timeout=30,
        )
        lost_at = time.time() + 2
        time.sleep(OBSERVE_SECONDS)
    late = [t for t in load.failure_times if t > lost_at + RECOVERY_SECONDS]
    assert not late, f"{len(late)} requests failed after {RECOVERY_SECONDS}s: {load.failures}"
    assert load.failure_rate < MAX_FAILURE_RATE, (load.failure_rate, load.failures)

    # The node returns after OUTAGE_SECONDS. Its unit is active within 5 minutes.
    juju.wait(lambda status: jubilant.all_active(status, APP), timeout=OUTAGE_SECONDS + 5 * 60)

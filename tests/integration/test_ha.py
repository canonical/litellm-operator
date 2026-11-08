# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Several units on one node: US-3 except node loss.

The units share one node here, so they are deployed without the anti-pod
constraint, which would leave two of them pending. test_node_loss.py covers
placement on separate nodes.
"""

from __future__ import annotations

import time
from pathlib import Path

import jubilant
import pytest
import yaml

from conftest import APP, MASTER_KEY, Upstream, credentials
from helpers import (
    Load,
    answer,
    chat,
    failures_outside,
    kubectl,
    namespace,
    request,
    service_address,
    wait_for,
    workload_starts,
)

VALKEY = "valkey"
POSTGRESQL = "postgresql-k8s"
ROTATED_KEY = "sk-integration-rotated"
MAX_FAILURE_RATE = 0.01  # SC-009, for restarts the charm makes
NEW_POD_SECONDS = 30  # SC-008, SC-009, for events that start pods


def _models(upstream: Upstream, *extra: dict) -> str:
    """The "chat" model followed by ``extra`` entries."""
    return yaml.safe_dump([upstream.entry("chat"), *extra])


def _units(juju: jubilant.Juju) -> list[str]:
    return sorted(juju.status().apps[APP].units)


@pytest.fixture(scope="module")
def deployed(juju: jubilant.Juju, charm: Path, resources: dict[str, str], upstream: Upstream):
    content = credentials(**{"encryption-key": "integration-encryption-key"})
    secret = juju.add_secret("litellm-credentials", content)
    juju.deploy(
        charm,
        APP,
        num_units=3,
        resources=resources,
        config={"credentials-secret": secret, "models": _models(upstream)},
    )
    juju.grant_secret(secret, APP)
    juju.deploy(VALKEY, channel="9/stable", trust=True)
    juju.deploy(POSTGRESQL, channel="16/stable", trust=True)
    return {"secret": secret, "content": content}


def test_several_units_need_the_store(juju: jubilant.Juju, deployed):
    """FR-028, US-3 scenario 9: without the store every unit blocks."""
    juju.wait(lambda status: jubilant.all_blocked(status, APP), timeout=20 * 60)
    for unit in juju.status().apps[APP].units.values():
        assert unit.workload_status.message == (
            "more than one unit needs a Redis-compatible store: relate valkey"
        )


def test_units_share_the_store(juju: jubilant.Juju, deployed):
    """FR-014, FR-026, US-3 scenario 1: with the store every unit serves the same models."""
    juju.integrate(APP, VALKEY)
    juju.integrate(APP, POSTGRESQL)
    juju.wait(lambda status: jubilant.all_active(status, APP, VALKEY, POSTGRESQL))
    for name, unit in juju.status().apps[APP].units.items():
        assert answer(chat(unit.address, "chat", MASTER_KEY)), name


def test_pod_deletion_under_load(juju: jubilant.Juju, deployed):
    """FR-015, SC-008, US-3 scenario 2: only the replacement pod's start fails requests."""
    address = service_address(juju, APP)
    with Load(address, "chat", MASTER_KEY) as load:
        time.sleep(10)
        kubectl("-n", namespace(juju), "delete", "pod", f"{APP}-1", "--wait=false")
        started = time.monotonic()
        time.sleep(20)
        juju.wait(lambda status: jubilant.all_active(status, APP), timeout=5 * 60)
        assert time.monotonic() - started < 5 * 60
        time.sleep(10)
    starts = workload_starts(namespace(juju), APP)
    assert not failures_outside(load, starts, NEW_POD_SECONDS), (load, starts)


def test_config_change_under_load(juju: jubilant.Juju, deployed, upstream: Upstream):
    """FR-008, SC-002, SC-009, US-3 scenario 3: units restart in turn."""
    address = service_address(juju, APP)
    second = upstream.entry("second")
    with Load(address, "chat", MASTER_KEY) as load:
        time.sleep(5)
        juju.config(APP, {"models": _models(upstream, second)})
        started = time.monotonic()
        wait_for(
            lambda: all(chat(address, "second", MASTER_KEY).status == 200 for _ in range(15)),
            timeout=10 * 60,
            what="every unit to serve the new model",
        )
        assert time.monotonic() - started < 10 * 60
        juju.wait(lambda status: jubilant.all_active(status, APP), delay=5)
    assert load.failure_rate < MAX_FAILURE_RATE, load


def test_secret_rotation_under_load(juju: jubilant.Juju, deployed):
    """SC-009, US-3 scenario 3: a rotated key reaches every unit."""
    address = service_address(juju, APP)
    juju.update_secret(deployed["secret"], {**deployed["content"], "master-key": ROTATED_KEY})
    wait_for(
        lambda: all(chat(address, "chat", ROTATED_KEY).status == 200 for _ in range(15)),
        timeout=10 * 60,
        what="every unit to take the new key",
    )
    juju.update_secret(deployed["secret"], deployed["content"])
    wait_for(
        lambda: all(chat(address, "chat", MASTER_KEY).status == 200 for _ in range(15)),
        timeout=10 * 60,
        what="every unit to take the old key back",
    )
    juju.wait(lambda status: jubilant.all_active(status, APP))


def test_scale_down_and_up_under_load(juju: jubilant.Juju, deployed):
    """SC-009, US-3 scenario 4: scaling from 3 to 1 and back keeps serving."""
    address = service_address(juju, APP)
    with Load(address, "chat", MASTER_KEY) as load:
        juju.cli("scale-application", APP, "1")
        juju.wait(
            lambda status: len(status.apps[APP].units) == 1 and jubilant.all_active(status, APP)
        )
        juju.cli("scale-application", APP, "3")
        juju.wait(
            lambda status: len(status.apps[APP].units) == 3 and jubilant.all_active(status, APP)
        )
    starts = workload_starts(namespace(juju), APP)
    assert not failures_outside(load, starts, NEW_POD_SECONDS), (load, starts)


def test_unready_unit_gets_no_requests(juju: jubilant.Juju, deployed):
    """FR-014, US-3 scenario 5: a unit whose workload is down leaves the Service."""
    address = service_address(juju, APP)
    unit = _units(juju)[-1]
    juju.ssh(unit, "/charm/bin/pebble", "stop", "litellm", container="litellm")
    try:
        time.sleep(15)
        results = [chat(address, "chat", MASTER_KEY).status for _ in range(30)]
        assert results.count(200) == len(results), results
    finally:
        juju.ssh(unit, "/charm/bin/pebble", "start", "litellm", container="litellm")
    juju.wait(lambda status: jubilant.all_active(status, APP))


def test_rate_limit_is_shared(juju: jubilant.Juju, deployed):
    """FR-026, SC-014, US-3 scenario 8: a key's limit holds across all units."""
    address = service_address(juju, APP)
    generated = request(address, "POST", "/key/generate", {"rpm_limit": 60}, key=MASTER_KEY)
    assert generated.status == 200, generated.body
    key = generated.json()["key"]
    started = time.monotonic()
    statuses = []
    for _ in range(180):
        statuses.append(chat(address, "chat", key).status)
    elapsed = time.monotonic() - started
    assert elapsed < 60, f"180 requests took {elapsed:.0f}s, more than the limit's minute"
    assert statuses.count(200) <= 66, statuses.count(200)
    assert statuses.count(429) >= 100


def test_cooldown_is_shared(juju: jubilant.Juju, deployed, upstream: Upstream):
    """FR-026, US-3 scenario 10: no unit routes to an upstream in cooldown."""
    broken = {
        "name": "pool",
        "model": "openai/none",
        "api-base": "http://127.0.0.1:9/v1",
    }
    working = upstream.entry("pool")
    juju.config(
        APP,
        {
            "models": _models(upstream, broken, working),
            "router-settings": "{allowed_fails: 0, cooldown_time: 300}",
            "num-retries": 0,
        },
    )
    juju.wait(lambda status: jubilant.all_active(status, APP), delay=5)
    address = service_address(juju, APP)
    # Until one unit has seen the broken upstream fail, some requests fail.
    wait_for(
        lambda: chat(address, "pool", MASTER_KEY).status != 200,
        timeout=120,
        interval=0.5,
        what="the broken upstream to fail once",
    )
    time.sleep(5)
    responses = [chat(address, "pool", MASTER_KEY) for _ in range(40)]
    failed = [(r.status, r.body[:300]) for r in responses if r.status != 200]
    assert not failed, failed
    juju.config(APP, {"models": _models(upstream)}, reset=["router-settings", "num-retries"])
    juju.wait(lambda status: jubilant.all_active(status, APP), delay=5)


def test_refresh_under_load(juju: jubilant.Juju, deployed, charm: Path):
    """SC-009: refreshing the charm keeps serving."""
    address = service_address(juju, APP)
    with Load(address, "chat", MASTER_KEY) as load:
        juju.refresh(APP, path=charm)
        time.sleep(30)
        juju.wait(lambda status: jubilant.all_active(status, APP), delay=5)
    starts = workload_starts(namespace(juju), APP)
    assert not failures_outside(load, starts, NEW_POD_SECONDS), (load, starts)


def test_store_relation_removed_blocks(juju: jubilant.Juju, deployed):
    """US-3 edge case: without the store several units block again."""
    juju.remove_relation(APP, VALKEY)
    juju.wait(lambda status: jubilant.all_blocked(status, APP))
    # Relating again fails while the old relation is still dying.
    juju.wait(lambda status: not status.apps[APP].relations.get("valkey"))
    juju.integrate(APP, VALKEY)
    juju.wait(lambda status: jubilant.all_active(status, APP))

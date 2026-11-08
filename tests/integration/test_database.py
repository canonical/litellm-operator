# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Virtual keys, budgets and spend with PostgreSQL: US-4 and US-2 scenario 5."""

from __future__ import annotations

import time
from pathlib import Path

import jubilant
import pytest

from conftest import APP, MASTER_KEY, Upstream, credentials
from helpers import answer, chat, request, service_address, unit_message, wait_for

POSTGRESQL = "postgresql-k8s"
ENCRYPTION_KEY = "integration-encryption-key"
# High prices, so that one request exceeds a small budget.
INPUT_COST = 0.001
OUTPUT_COST = 0.002
PRICES = {"input-cost-per-token": INPUT_COST, "output-cost-per-token": OUTPUT_COST}


@pytest.fixture(scope="module")
def deployed(juju: jubilant.Juju, charm: Path, resources: dict[str, str], upstream: Upstream):
    secret = juju.add_secret(
        "litellm-credentials", credentials(**{"encryption-key": ENCRYPTION_KEY})
    )
    juju.deploy(
        charm,
        APP,
        resources=resources,
        config={"credentials-secret": secret, "models": upstream.models("chat", **PRICES)},
    )
    juju.grant_secret(secret, APP)
    juju.deploy(POSTGRESQL, channel="16/stable", trust=True)
    juju.wait(lambda status: jubilant.all_active(status, APP, POSTGRESQL))
    return {"secret": secret}


def test_virtual_keys_need_a_database(juju: jubilant.Juju, deployed):
    """US-4 scenario 1: without a database the master key works and keys cannot be issued."""
    address = service_address(juju, APP)
    assert answer(chat(address, "chat", MASTER_KEY))
    response = request(address, "POST", "/key/generate", {}, key=MASTER_KEY)
    assert response.status >= 400
    assert "database" in response.body.lower() or "db" in response.body.lower()


def test_virtual_key_with_database(juju: jubilant.Juju, deployed):
    """FR-016, US-4 scenario 2: a virtual key issued with the master key works."""
    juju.integrate(APP, POSTGRESQL)
    juju.wait(lambda status: jubilant.all_active(status, APP, POSTGRESQL))
    address = service_address(juju, APP)
    response = wait_for(
        lambda: (
            (r := request(address, "POST", "/key/generate", {}, key=MASTER_KEY)).status == 200
            and r
        ),
        timeout=180,
        what="key generation",
    )
    key = response.json()["key"]
    assert answer(chat(address, "chat", key))


def test_budget_and_spend(juju: jubilant.Juju, deployed):
    """FR-032, SC-015, US-4 scenarios 3 and 6: spend at configured prices, budget enforced."""
    address = service_address(juju, APP)
    generated = request(address, "POST", "/key/generate", {"max_budget": 0.000001}, key=MASTER_KEY)
    assert generated.status == 200, generated.body
    key = generated.json()["key"]

    first = chat(address, "chat", key)
    assert first.status == 200, first.body
    usage = first.json()["usage"]
    expected = usage["prompt_tokens"] * INPUT_COST + usage["completion_tokens"] * OUTPUT_COST

    def spend() -> float:
        info = request(address, "GET", f"/key/info?key={key}", key=MASTER_KEY)
        return float(info.json()["info"]["spend"] or 0)

    recorded = wait_for(spend, timeout=180, what="spend to be recorded")
    assert abs(recorded - expected) <= 0.01 * expected, (recorded, expected)
    seen = []
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = chat(address, "chat", key)
        seen.append((response.status, response.body[:160]))
        if 400 <= response.status < 500 and "budget" in response.body.lower():
            break
        time.sleep(5)
    else:
        raise AssertionError(f"budget not enforced: spend {spend()}, responses {seen[-3:]}")


def test_changed_encryption_key_blocks(juju: jubilant.Juju, deployed):
    """FR-021, US-2 scenario 5: a new encryption key blocks the units."""
    juju.update_secret(deployed["secret"], credentials(**{"encryption-key": "a-different-key"}))
    juju.wait(lambda status: jubilant.all_blocked(status, APP))
    message = unit_message(juju, f"{APP}/0")
    assert message == "encryption-key changed: restore the key the database uses"
    juju.update_secret(deployed["secret"], credentials(**{"encryption-key": ENCRYPTION_KEY}))
    juju.wait(lambda status: jubilant.all_active(status, APP))


def test_losing_the_database_blocks(juju: jubilant.Juju, deployed):
    """FR-018, US-4 scenario 4: removal blocks until the action is run."""
    juju.remove_relation(APP, POSTGRESQL)
    juju.wait(lambda status: jubilant.all_blocked(status, APP))
    message = unit_message(juju, f"{APP}/0")
    assert "run continue-without-database" in message

    juju.run(f"{APP}/leader", "continue-without-database")
    juju.wait(lambda status: jubilant.all_active(status, APP))
    assert answer(chat(service_address(juju, APP), "chat", MASTER_KEY))

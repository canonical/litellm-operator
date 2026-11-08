# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""One unit routing to an upstream: US-1, US-2 and US-5."""

from __future__ import annotations

import time
from pathlib import Path

import jubilant
import pytest
import yaml

from conftest import APP, MASTER_KEY, MOCK_ANSWER, UPSTREAM_KEY, Upstream, credentials
from helpers import (
    answer,
    chat,
    chat_stream,
    model_names,
    request,
    service_address,
    unit_message,
    wait_for,
    workload_logs,
)

SECRET_NAME = "litellm-credentials"
ROTATED_KEY = "sk-integration-rotated"


@pytest.fixture(scope="module")
def deployed(juju: jubilant.Juju, charm: Path, resources: dict[str, str], upstream: Upstream):
    """Deploy without models, so the first test sees the waiting status."""
    secret = juju.add_secret(SECRET_NAME, credentials())
    started = time.monotonic()
    juju.deploy(charm, APP, resources=resources, config={"credentials-secret": secret})
    juju.grant_secret(secret, APP)
    juju.wait(
        lambda status: (
            jubilant.all_waiting(status, APP)
            and "model configuration" in status.apps[APP].units[f"{APP}/0"].workload_status.message
        )
    )
    return {"secret": secret, "started": started}


def test_waits_for_models(juju: jubilant.Juju, deployed):
    """FR-007: with no model, the unit waits and serves nothing."""
    status = juju.status().apps[APP].units[f"{APP}/0"].workload_status
    assert status.current == "waiting"
    assert status.message == "waiting for model configuration"


def test_serves_configured_model(juju: jubilant.Juju, deployed, upstream: Upstream):
    """FR-002, FR-004, SC-007: a structured model is listed and answers."""
    juju.config(APP, {"models": upstream.models("chat")})
    juju.wait(lambda status: jubilant.all_active(status, APP))
    assert time.monotonic() - deployed["started"] < 5 * 60, "SC-007: active within 5 minutes"

    address = service_address(juju, APP)
    assert "chat" in model_names(address, MASTER_KEY)
    text = answer(chat(address, "chat", MASTER_KEY))
    assert text
    if upstream.stand_in:
        assert text == MOCK_ANSWER


def test_streams(juju: jubilant.Juju, deployed, upstream: Upstream):
    """FR-004, US-5: a streaming request arrives in pieces."""
    deltas = chat_stream(service_address(juju, APP), "chat", MASTER_KEY)
    assert deltas
    if upstream.stand_in:
        assert "".join(deltas) == MOCK_ANSWER
    else:
        assert len(deltas) > 1


def test_refuses_requests_without_the_master_key(juju: jubilant.Juju, deployed):
    """FR-006, SC-003: no key or a wrong key is refused."""
    address = service_address(juju, APP)
    assert chat(address, "chat", None).status == 401
    # Without a database LiteLLM refuses an unknown sk- key with 400 "No connected db".
    for key in ("sk-wrong", "wrong"):
        assert chat(address, "chat", key).status in (400, 401)


def test_model_changes_apply(juju: jubilant.Juju, deployed, upstream: Upstream):
    """FR-008, SC-002: add, rename and remove models without a manual restart."""
    address = service_address(juju, APP)
    juju.config(APP, {"models": upstream.models("chat", "second")})
    started = time.monotonic()
    wait_for(lambda: "second" in model_names(address, MASTER_KEY), timeout=120, what="new model")
    assert time.monotonic() - started < 60, "SC-002: applied within 60 seconds"
    assert answer(chat(address, "second", MASTER_KEY))

    juju.config(APP, {"models": upstream.models("chat")})
    wait_for(lambda: "second" not in model_names(address, MASTER_KEY), timeout=120, what="removal")


def test_raw_model_entry(juju: jubilant.Juju, deployed, upstream: Upstream):
    """FR-003: a raw model_list entry is served alongside the structured ones."""
    raw = [
        {
            "model_name": "raw",
            "litellm_params": {
                "model": f"openai/{upstream.model}",
                "api_base": upstream.api_base,
                "api_key": "os.environ/LITELLM_CREDENTIAL_UPSTREAM",
            },
        }
    ]
    juju.config(APP, {"model-list": yaml.safe_dump(raw)})
    address = service_address(juju, APP)
    wait_for(lambda: "raw" in model_names(address, MASTER_KEY), timeout=120, what="raw model")
    assert answer(chat(address, "raw", MASTER_KEY))
    juju.config(APP, reset="model-list")


def test_missing_credential_blocks(juju: jubilant.Juju, deployed, upstream: Upstream):
    """FR-009: an entry naming a key the secret lacks blocks the unit."""
    bad = yaml.safe_dump([{"name": "bad", "model": "openai/x", "credential": "missing"}])
    juju.config(APP, {"models": bad})
    juju.wait(lambda status: jubilant.all_blocked(status, APP))
    message = unit_message(juju, f"{APP}/0")
    assert message == "model 'bad': credentials-secret has no key 'missing'"
    juju.config(APP, {"models": upstream.models("chat")})
    juju.wait(lambda status: jubilant.all_active(status, APP))


def test_secret_rotation(juju: jubilant.Juju, deployed):
    """FR-008, US-2: a new master key replaces the old one."""
    juju.update_secret(deployed["secret"], credentials(**{"master-key": ROTATED_KEY}))
    address = service_address(juju, APP)
    wait_for(lambda: chat(address, "chat", ROTATED_KEY).status == 200, timeout=180, what="new key")
    assert chat(address, "chat", MASTER_KEY).status in (400, 401)
    juju.update_secret(deployed["secret"], credentials())
    wait_for(lambda: chat(address, "chat", MASTER_KEY).status == 200, timeout=180, what="old key")


def test_no_secret_values_exposed(juju: jubilant.Juju, deployed):
    """FR-005, SC-004: keys are not in config, status or logs."""
    exposed = "\n".join(
        [
            juju.cli("config", APP),
            juju.cli("status", "--format=yaml"),
            juju.debug_log(limit=2000),
            workload_logs(juju, f"{APP}/0"),
        ]
    )
    for value in (MASTER_KEY, ROTATED_KEY, UPSTREAM_KEY):
        assert value not in exposed


def test_no_prompt_in_logs(juju: jubilant.Juju, deployed):
    """SC-012: prompts and completions are not logged by default."""
    marker = "integration-marker-7f3a9c"
    address = service_address(juju, APP)
    answer(chat(address, "chat", MASTER_KEY, content=f"Repeat {marker}"))
    redaction_off = {"litellm-disable-message-redaction": "true"}
    chat(address, "chat", MASTER_KEY, content=f"Repeat {marker}", headers=redaction_off)
    assert marker not in workload_logs(juju, f"{APP}/0")


def test_bundled_price_map(juju: jubilant.Juju, deployed):
    """FR-013, FR-030: the bundled price map is used, without a download."""
    source = request(service_address(juju, APP), "GET", "/model/cost_map/source", key=MASTER_KEY)
    assert source.status == 200, source.body
    assert source.json()["source"] == "local"

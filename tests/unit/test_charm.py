# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""State-transition tests for the LiteLLM charm."""

import pytest
from ops import ActiveStatus, BlockedStatus, WaitingStatus, pebble, testing

import charm as charm_module
import workload
from charm import LiteLLMCharm
from defaults import CONFIG_PATH, CONTAINER, PORT, READY_CHECK, SERVICE

MODELS = "[{name: local, model: openai/qwen, api-base: 'http://10.0.0.1:8888/v1'}]"
SECRET_CONTENT = {"master-key": "sk-master", "openai": "sk-upstream"}


@pytest.fixture(autouse=True)
def workload_ready(monkeypatch):
    """Stub the direct readiness probe. Tests flip it through the returned dict."""
    state = {"ready": True}
    monkeypatch.setattr(charm_module, "_ready", lambda: state["ready"])
    monkeypatch.setattr(charm_module, "_wait_ready", lambda timeout: None)
    return state


@pytest.fixture
def ctx():
    return testing.Context(LiteLLMCharm)


def _secret(content=None):
    return testing.Secret(tracked_content=content or SECRET_CONTENT)


def _container(**kwargs):
    kwargs.setdefault("can_connect", True)
    return testing.Container(CONTAINER, **kwargs)


def _running(check_status=pebble.CheckStatus.UP):
    layer = workload.build_layer({}, "digest")
    return _container(
        layers={CONTAINER: pebble.Layer(layer)},
        service_statuses={SERVICE: pebble.ServiceStatus.ACTIVE},
        check_infos={
            testing.CheckInfo(READY_CHECK, level=pebble.CheckLevel.READY, status=check_status)
        },
    )


def _state(container, config=None, secret=None, **kwargs):
    secret = secret or _secret()
    options = {"credentials-secret": secret.id, "models": MODELS}
    options.update(config or {})
    return testing.State(
        containers={container}, config=options, secrets={secret}, leader=True, **kwargs
    )


def test_waits_without_models(ctx):
    """FR-007: no models, no service."""
    out = ctx.run(ctx.on.config_changed(), _state(_container(), config={"models": ""}))
    assert out.unit_status == WaitingStatus("waiting for model configuration")
    assert SERVICE not in out.get_container(CONTAINER).plan.services


def test_blocks_without_the_secret_option(ctx):
    state = _state(_container())
    state = testing.State(
        containers=state.containers, config={"models": MODELS}, secrets=state.secrets
    )
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, BlockedStatus)
    assert "credentials-secret" in out.unit_status.message


def test_blocks_when_the_secret_cannot_be_read(ctx):
    """A secret that is not granted blocks with a message naming it."""
    container = _container()
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(
            containers={container},
            config={"credentials-secret": "secret:cvn0000000000000000g", "models": MODELS},
        ),
    )
    assert out.unit_status == BlockedStatus(
        "cannot read credentials-secret: run `juju grant-secret <secret> litellm`"
    )


def test_blocks_on_a_missing_credential(ctx):
    """FR-009: an entry naming a key the secret lacks blocks the unit."""
    models = "[{name: m, model: openai/x, credential: missing}]"
    out = ctx.run(ctx.on.config_changed(), _state(_container(), config={"models": models}))
    assert out.unit_status == BlockedStatus("model 'm': credentials-secret has no key 'missing'")


def test_plans_the_workload(ctx):
    """FR-001, FR-004, FR-005: the service runs with credentials in its environment."""
    out = ctx.run(ctx.on.pebble_ready(_container()), _state(_container()))
    container = out.get_container(CONTAINER)
    service = container.plan.services[SERVICE]
    assert "--config /etc/litellm/config.yaml --port 4000 --num_workers 1" in service.command
    assert service.environment["LITELLM_MASTER_KEY"] == "sk-master"
    assert service.environment["LITELLM_CREDENTIAL_OPENAI"] == "sk-upstream"
    pushed = container.get_filesystem(ctx) / CONFIG_PATH.lstrip("/")
    text = pushed.read_text()
    assert "sk-master" not in text and "sk-upstream" not in text
    assert out.opened_ports == {testing.TCPPort(PORT)}


def test_config_change_restarts_the_service(ctx):
    """FR-008: a changed model changes the service, so the replan restarts it."""
    first = ctx.run(ctx.on.pebble_ready(_container()), _state(_container()))
    digest = first.get_container(CONTAINER).plan.services[SERVICE].environment
    container = first.get_container(CONTAINER)
    changed = _state(container, config={"models": "[{name: other, model: openai/y}]"})
    out = ctx.run(ctx.on.config_changed(), changed)
    after = out.get_container(CONTAINER).plan.services[SERVICE].environment
    assert after["LITELLM_CHARM_CONFIG_DIGEST"] != digest["LITELLM_CHARM_CONFIG_DIGEST"]


def test_secret_rotation_is_applied(ctx):
    """FR-008: a new secret revision reaches the workload."""
    secret = testing.Secret(
        tracked_content=SECRET_CONTENT,
        latest_content={**SECRET_CONTENT, "master-key": "sk-rotated"},
    )
    out = ctx.run(ctx.on.secret_changed(secret), _state(_container(), secret=secret))
    service = out.get_container(CONTAINER).plan.services[SERVICE]
    assert service.environment["LITELLM_MASTER_KEY"] == "sk-rotated"


def test_invalid_configuration_stops_the_service(ctx):
    out = ctx.run(ctx.on.config_changed(), _state(_running(), config={"models": "[: bad"}))
    assert out.unit_status == BlockedStatus("models is not valid YAML")
    assert out.get_container(CONTAINER).service_statuses[SERVICE] == pebble.ServiceStatus.INACTIVE


def test_active_when_ready(ctx):
    """FR-010: a ready workload is active."""
    out = ctx.run(ctx.on.update_status(), _state(_running()))
    assert out.unit_status == ActiveStatus()


def test_waiting_when_not_ready(ctx, workload_ready):
    """FR-010: a failing readiness check is reported."""
    workload_ready["ready"] = False
    out = ctx.run(ctx.on.update_status(), _state(_running()))
    assert out.unit_status == WaitingStatus("waiting for LiteLLM to become ready")


def test_price_map_fallback_is_reported(ctx, monkeypatch):
    """FR-031: a fallback to the bundled price map shows in status."""
    monkeypatch.setattr(
        LiteLLMCharm,
        "_check_cost_map",
        lambda self, key: setattr(self._stored, "cost_map_fallback", "too small"),
    )
    out = ctx.run(ctx.on.update_status(), _state(_running()))
    assert out.unit_status == ActiveStatus("model-cost-map-url not used, bundled price map in use")


def test_no_secret_in_status(ctx):
    """FR-005: status never shows a secret value."""
    models = "[{name: m, model: openai/x, credential: missing}]"
    out = ctx.run(ctx.on.config_changed(), _state(_container(), config={"models": models}))
    for value in SECRET_CONTENT.values():
        assert value not in out.unit_status.message


def test_proxy_change_on_update_status(ctx, monkeypatch):
    """FR-012: Juju fires no hook when the proxy changes, so update-status checks."""
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "http://squid:3128")
    out = ctx.run(ctx.on.update_status(), _state(_running()))
    service = out.get_container(CONTAINER).plan.services[SERVICE]
    assert service.environment["HTTPS_PROXY"] == "http://squid:3128"


def test_named_options_reach_the_file(ctx):
    """FR-024: named options are rendered into the pushed configuration."""
    out = ctx.run(
        ctx.on.config_changed(),
        _state(_container(), config={"routing-strategy": "least-busy", "num-retries": 3}),
    )
    container = out.get_container(CONTAINER)
    text = (container.get_filesystem(ctx) / CONFIG_PATH.lstrip("/")).read_text()
    assert "routing_strategy: least-busy" in text
    assert "num_retries: 3" in text


def test_charm_owned_passthrough_blocks(ctx):
    """FR-022: a charm-owned key in the passthrough blocks, naming it."""
    out = ctx.run(
        ctx.on.config_changed(),
        _state(_container(), config={"extra-env": "DATABASE_URL=postgres://x"}),
    )
    assert out.unit_status == BlockedStatus(
        "extra-env must not set DATABASE_URL, the charm manages it"
    )


def test_conflicts_are_logged(ctx, caplog):
    """A passthrough setting that a named option overrides is logged."""
    ctx.run(
        ctx.on.config_changed(),
        _state(_container(), config={"router-settings": "{routing_strategy: least-busy}"}),
    )
    assert "overrides router_settings.routing_strategy" in caplog.text

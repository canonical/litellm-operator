# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for the pure configuration module."""

import pytest
import yaml

from litellm_config import ConfigError, Inputs, credential_variable, has_models, proxy_environment
from litellm_config import render as _render

SECRET = {"master-key": "sk-master", "openai": "sk-upstream", "local-key": "abc"}


def render(**kwargs):
    kwargs.setdefault("secret", SECRET)
    return _render(Inputs(**kwargs))


def test_structured_entry_renders_model_list():
    """FR-002: a structured entry becomes one model_list element."""
    rendered = render(
        models="""
- name: local
  model: openai/qwen
  api-base: http://10.0.0.1:8888/v1
  credential: local-key
  input-cost-per-token: 0.000001
  output-cost-per-token: 0.000002
"""
    )
    assert rendered.config["model_list"] == [
        {
            "model_name": "local",
            "litellm_params": {
                "model": "openai/qwen",
                "api_base": "http://10.0.0.1:8888/v1",
                "api_key": "os.environ/LITELLM_CREDENTIAL_LOCAL_KEY",
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
            },
        }
    ]
    assert rendered.environment["LITELLM_CREDENTIAL_LOCAL_KEY"] == "abc"


def test_raw_entries_are_served_with_structured_ones():
    """FR-003, and one public name in both forms makes one group."""
    raw = [
        {"model_name": "local", "litellm_params": {"model": "openai/b", "api_base": "http://b"}},
    ]
    rendered = render(
        models="[{name: local, model: openai/a}]",
        model_list=yaml.safe_dump(raw),
    )
    names = [entry["model_name"] for entry in rendered.config["model_list"]]
    assert names == ["local", "local"]
    assert rendered.config["model_list"][1] == raw[0]


def test_no_secret_value_in_the_file():
    """FR-005, SC-004: secret values only reach the environment."""
    rendered = render(models="[{name: m, model: openai/x, credential: openai}]")
    text = rendered.config_yaml
    for value in SECRET.values():
        assert value not in text
    assert rendered.environment["LITELLM_MASTER_KEY"] == "sk-master"
    assert "os.environ/LITELLM_MASTER_KEY" in text


@pytest.mark.parametrize(
    ("models", "message"),
    [
        ("{name: x}", "models must be a YAML list"),
        ("[: bad", "models is not valid YAML"),
        ("[{model: openai/x}]", "models entry 1 needs a name"),
        ("[{name: m, model: x}]", "model 'm' needs model as provider/model"),
        ("[{name: m, model: openai/x, api-base: ftp://h}]", "api-base must be an http"),
        ("[{name: m, model: openai/x, credential: nope}]", "has no key 'nope'"),
        ("[{name: m, model: openai/x, credential: master-key}]", "has no key 'master-key'"),
        ("[{name: m, model: openai/x, input-cost-per-token: -1}]", "must be a number"),
        ("[{name: m, model: openai/x, colour: red}]", "unknown field 'colour'"),
    ],
)
def test_invalid_models_name_the_problem(models, message):
    """FR-009: an invalid entry or a missing credential is named."""
    with pytest.raises(ConfigError, match=message):
        render(models=models)


def test_raw_entry_with_missing_credential_is_refused():
    """FR-009: raw entries are checked for the credentials they reference."""
    raw = (
        "[{model_name: m, litellm_params: "
        "{model: openai/x, api_key: os.environ/LITELLM_CREDENTIAL_NOPE}}]"
    )
    with pytest.raises(ConfigError, match="LITELLM_CREDENTIAL_NOPE"):
        render(model_list=raw)


def test_raw_entry_needs_model():
    with pytest.raises(ConfigError, match="model-list entry 1 needs litellm_params.model"):
        render(model_list="[{model_name: m, litellm_params: {}}]")


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ({}, "has no master-key"),
        ({"master-key": "plain"}, "must start with sk-"),
        ({"master-key": "sk-a", "Bad_Key": "x"}, "not lowercase kebab-case"),
    ],
)
def test_secret_is_checked(secret, message):
    with pytest.raises(ConfigError, match=message):
        render(models="[{name: m, model: openai/x}]", secret=secret)


def test_bundled_price_map_by_default():
    """FR-013, FR-030: no price map download unless an address is set."""
    env = render(models="[{name: m, model: openai/x}]").environment
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert "LITELLM_MODEL_COST_MAP_URL" not in env


def test_configured_price_map():
    """FR-031: the configured address replaces the bundled map."""
    env = render(
        models="[{name: m, model: openai/x}]", model_cost_map_url="https://example.com/map.json"
    ).environment
    assert env["LITELLM_MODEL_COST_MAP_URL"] == "https://example.com/map.json"
    assert "LITELLM_LOCAL_MODEL_COST_MAP" not in env


def test_price_map_address_must_be_http():
    with pytest.raises(ConfigError, match="model-cost-map-url"):
        render(models="[{name: m, model: openai/x}]", model_cost_map_url="file:///map.json")


def test_charm_owned_settings():
    """FR-023 and SC-012: models stay out of the database, content out of logs."""
    rendered = render(models="[{name: m, model: openai/x}]")
    assert rendered.environment["STORE_MODEL_IN_DB"] == "False"
    assert rendered.environment["LITELLM_LOG"] == "INFO"
    assert rendered.config["litellm_settings"]["turn_off_message_logging"] is True


def test_proxy_settings_in_both_cases():
    """FR-012: Juju's proxy settings reach the workload."""
    proxy = proxy_environment(
        {"JUJU_CHARM_HTTPS_PROXY": "http://squid:3128", "JUJU_CHARM_NO_PROXY": "10.0.0.1"}
    )
    assert proxy == {
        "HTTPS_PROXY": "http://squid:3128",
        "https_proxy": "http://squid:3128",
        "NO_PROXY": "10.0.0.1",
        "no_proxy": "10.0.0.1",
    }
    env = render(models="[{name: m, model: openai/x}]", proxy=proxy).environment
    assert env["no_proxy"] == "10.0.0.1"


def test_has_models():
    """FR-007: either option counts."""
    assert not has_models(Inputs())
    assert has_models(Inputs(models="[{name: m, model: openai/x}]"))
    assert has_models(Inputs(model_list="[{model_name: m}]"))


def test_digest_follows_the_configuration():
    a = render(models="[{name: m, model: openai/x}]")
    b = render(models="[{name: m, model: openai/y}]")
    assert a.digest != b.digest
    assert a.digest == render(models="[{name: m, model: openai/x}]").digest


def test_credential_variable():
    assert credential_variable("my-key") == "LITELLM_CREDENTIAL_MY_KEY"


MODEL = "[{name: m, model: openai/x}]"


def test_named_options():
    """FR-024: each named option lands in LiteLLM's configuration."""
    rendered = render(
        models=MODEL,
        options={
            "routing-strategy": "least-busy",
            "num-retries": 2,
            "request-timeout": 30,
            "fallbacks": "[{m: [other]}]",
            "log-level": "warning",
            "log-request-content": True,
            "drop-params": True,
            "max-request-size-mb": 5,
            "serve-without-database": True,
            "spend-logs": False,
        },
    )
    config = rendered.config
    assert config["router_settings"] == {
        "routing_strategy": "least-busy",
        "fallbacks": [{"m": ["other"]}],
    }
    assert config["litellm_settings"] == {
        "turn_off_message_logging": False,
        "num_retries": 2,
        "request_timeout": 30,
        "drop_params": True,
    }
    assert config["general_settings"] == {
        "max_request_size_mb": 5,
        "allow_requests_on_db_unavailable": True,
        "disable_spend_logs": True,
        "master_key": "os.environ/LITELLM_MASTER_KEY",
    }
    assert rendered.environment["LITELLM_LOG"] == "WARNING"


def test_unset_options_leave_litellm_defaults():
    """FR-024: unset numeric options are left out, content logging is off."""
    config = render(models=MODEL).config
    assert config["litellm_settings"] == {"turn_off_message_logging": True}
    assert config["router_settings"] == {"routing_strategy": "simple-shuffle"}
    assert config["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY"}


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"routing-strategy": "random"}, "routing-strategy must be one of"),
        ({"log-level": "loud"}, "log-level must be one of"),
        ({"num-retries": -1}, "num-retries must be 0 or more"),
        ({"max-request-size-mb": -1}, "max-request-size-mb must be 0 or more"),
        ({"fallbacks": "[m]"}, "fallbacks must be a list"),
        ({"fallbacks": "[{m: other}]"}, "fallbacks must be a list"),
    ],
)
def test_invalid_named_options(options, message):
    with pytest.raises(ConfigError, match=message):
        render(models=MODEL, options=options)


def test_passthrough_settings_are_merged():
    """FR-025: passthrough settings and variables reach LiteLLM."""
    rendered = render(
        models=MODEL,
        litellm_settings="{set_verbose: false, max_budget: 100}",
        router_settings="{allowed_fails: 3}",
        extra_env="# comment\nAIOHTTP_TRUST_ENV=True\n\nOTHER = x\n",
    )
    assert rendered.config["litellm_settings"]["max_budget"] == 100
    assert rendered.config["router_settings"]["allowed_fails"] == 3
    assert rendered.environment["AIOHTTP_TRUST_ENV"] == "True"
    assert rendered.environment["OTHER"] == "x"
    assert rendered.conflicts == ()


def test_named_options_win_over_passthrough():
    """FR-025: a named option overrides the passthrough, and the conflict is reported."""
    rendered = render(
        models=MODEL,
        options={"num-retries": 1},
        litellm_settings="{num_retries: 9, turn_off_message_logging: false}",
        router_settings="{routing_strategy: least-busy}",
        extra_env="LITELLM_LOG=DEBUG",
    )
    assert rendered.config["litellm_settings"]["num_retries"] == 1
    assert rendered.config["litellm_settings"]["turn_off_message_logging"] is True
    assert rendered.config["router_settings"]["routing_strategy"] == "simple-shuffle"
    assert rendered.environment["LITELLM_LOG"] == "INFO"
    assert set(rendered.conflicts) == {
        "litellm_settings.num_retries",
        "litellm_settings.turn_off_message_logging",
        "router_settings.routing_strategy",
        "LITELLM_LOG",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"litellm_settings": "{master_key: sk-x}"}, "litellm-settings must not set master_key"),
        ({"litellm_settings": "{cache_params: {}}"}, "must not set cache_params"),
        ({"router_settings": "{redis_host: h}"}, "router-settings must not set redis_host"),
        ({"router_settings": "{num_workers: 4}"}, "must not set num_workers"),
        ({"litellm_settings": "{store_model_in_db: true}"}, "must not set store_model_in_db"),
        ({"extra_env": "DATABASE_URL=postgres://"}, "extra-env must not set DATABASE_URL"),
        ({"extra_env": "LITELLM_SALT_KEY=x"}, "must not set LITELLM_SALT_KEY"),
        ({"extra_env": "REDIS_HOST=h"}, "must not set REDIS_HOST"),
        ({"extra_env": "PORT=8000"}, "must not set PORT"),
        ({"extra_env": "LITELLM_MODEL_COST_MAP_URL=http://x"}, "must not set LITELLM_MODEL"),
        ({"extra_env": "LITELLM_CREDENTIAL_X=y"}, "must not set LITELLM_CREDENTIAL_X"),
        ({"extra_env": "not a pair"}, "extra-env line 1 must be KEY=VALUE"),
        ({"litellm_settings": "[1]"}, "litellm-settings must be a YAML mapping"),
    ],
)
def test_charm_owned_settings_are_refused(kwargs, message):
    """FR-022: the passthrough cannot set what the charm manages."""
    with pytest.raises(ConfigError, match=message):
        render(models=MODEL, **kwargs)

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

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Turn config options, secret content and proxy settings into LiteLLM's inputs.

Pure: no ``ops``, no container (architecture.md§2). The result is the
configuration file's content and the workload environment. Secret values only
ever appear in the environment, and the file refers to them as
``os.environ/<NAME>`` (architecture.md§4).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from defaults import ENCRYPTION_KEY, MASTER_KEY

CREDENTIAL_PREFIX = "LITELLM_CREDENTIAL_"
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
_SECRET_KEY = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Fields of a structured model entry (data-model.md) and where each one goes.
_ENTRY_FIELDS = {
    "name",
    "model",
    "api-base",
    "credential",
    "input-cost-per-token",
    "output-cost-per-token",
}


class ConfigError(Exception):
    """The configuration cannot be applied. The message is shown in unit status."""


@dataclass(frozen=True)
class Inputs:
    """Everything the rendered configuration depends on."""

    models: str = ""
    model_list: str = ""
    model_cost_map_url: str = ""
    secret: Mapping[str, str] = field(default_factory=dict)
    proxy: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Rendered:
    """The workload's configuration file and environment."""

    config: dict[str, Any]
    environment: dict[str, str]

    @property
    def config_yaml(self) -> str:
        return yaml.safe_dump(self.config, sort_keys=False)

    @property
    def digest(self) -> str:
        """Changes whenever the file or the environment changes."""
        material = self.config_yaml + "\0" + repr(sorted(self.environment.items()))
        return hashlib.sha256(material.encode()).hexdigest()[:16]


def credential_variable(key: str) -> str:
    """The environment variable holding the secret key ``key``."""
    return CREDENTIAL_PREFIX + key.upper().replace("-", "_")


def proxy_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The model's HTTP proxy settings, as Juju passes them to the charm (FR-012)."""
    result: dict[str, str] = {}
    for name in PROXY_VARIABLES:
        value = environ.get(f"JUJU_CHARM_{name}", "")
        if value:
            result[name] = value
            result[name.lower()] = value
    return result


def has_models(inputs: Inputs) -> bool:
    """Whether any model is configured, structured or raw (FR-007)."""
    return bool(_load_list(inputs.models, "models") or _load_list(inputs.model_list, "model-list"))


def render(inputs: Inputs) -> Rendered:
    """Render the configuration, or raise :class:`ConfigError` naming the problem."""
    secret = dict(inputs.secret)
    _check_secret(secret)
    model_list = [_structured_entry(entry, i, secret) for i, entry in _entries(inputs)]
    model_list += [_raw_entry(entry, i, secret) for i, entry in _raw_entries(inputs)]

    config: dict[str, Any] = {
        "model_list": model_list,
        "litellm_settings": {
            # Prompts and completions stay out of logging callbacks (SC-012).
            "turn_off_message_logging": True,
        },
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
        },
    }

    environment: dict[str, str] = {
        "LITELLM_MASTER_KEY": secret[MASTER_KEY],
        # Models live in this file only, never in the database (FR-023).
        "STORE_MODEL_IN_DB": "False",
        # The admin UI is a separate feature.
        "DISABLE_ADMIN_UI": "True",
        # LiteLLM's own default is DEBUG.
        "LITELLM_LOG": "INFO",
        **_cost_map_environment(inputs.model_cost_map_url),
        **inputs.proxy,
    }
    for key, value in secret.items():
        if key not in (MASTER_KEY, ENCRYPTION_KEY):
            environment[credential_variable(key)] = value
    return Rendered(config=config, environment=environment)


def _check_secret(secret: Mapping[str, str]) -> None:
    master_key = secret.get(MASTER_KEY, "")
    if not master_key:
        raise ConfigError(f"credentials-secret has no {MASTER_KEY}")
    if not master_key.startswith("sk-"):
        raise ConfigError(f"{MASTER_KEY} must start with sk-")
    for key in secret:
        if not _SECRET_KEY.match(key):
            raise ConfigError(f"credentials-secret key {key!r} is not lowercase kebab-case")


def _cost_map_environment(url: str) -> dict[str, str]:
    """Bundled price map unless an address is configured (FR-013, FR-030, FR-031)."""
    url = url.strip()
    if not url:
        return {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    if not url.startswith(("http://", "https://")):
        raise ConfigError("model-cost-map-url must be an http or https address")
    return {"LITELLM_MODEL_COST_MAP_URL": url}


def _load_list(text: str, option: str) -> list[Any]:
    if not text.strip():
        return []
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{option} is not valid YAML") from exc
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{option} must be a YAML list")
    return value


def _entries(inputs: Inputs) -> list[tuple[int, Any]]:
    return list(enumerate(_load_list(inputs.models, "models"), start=1))


def _raw_entries(inputs: Inputs) -> list[tuple[int, Any]]:
    return list(enumerate(_load_list(inputs.model_list, "model-list"), start=1))


def _structured_entry(entry: Any, index: int, secret: Mapping[str, str]) -> dict[str, Any]:
    where = f"models entry {index}"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} must be a mapping")
    unknown = sorted(set(entry) - _ENTRY_FIELDS)
    if unknown:
        raise ConfigError(f"{where} has unknown field {unknown[0]!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where} needs a name")
    where = f"model {name!r}"
    model = entry.get("model")
    if not isinstance(model, str) or "/" not in model:
        raise ConfigError(f"{where} needs model as provider/model")

    params: dict[str, Any] = {"model": model}
    api_base = entry.get("api-base")
    if api_base is not None:
        if not isinstance(api_base, str) or not api_base.startswith(("http://", "https://")):
            raise ConfigError(f"{where}: api-base must be an http or https address")
        params["api_base"] = api_base
    credential = entry.get("credential")
    if credential is not None:
        if credential not in secret or credential in (MASTER_KEY, ENCRYPTION_KEY):
            raise ConfigError(f"{where}: credentials-secret has no key {credential!r}")
        params["api_key"] = f"os.environ/{credential_variable(credential)}"
    for option, param in (
        ("input-cost-per-token", "input_cost_per_token"),
        ("output-cost-per-token", "output_cost_per_token"),
    ):
        if option in entry:
            cost = entry[option]
            if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0:
                raise ConfigError(f"{where}: {option} must be a number of 0 or more")
            params[param] = cost
    return {"model_name": name, "litellm_params": params}


def _raw_entry(entry: Any, index: int, secret: Mapping[str, str]) -> dict[str, Any]:
    where = f"model-list entry {index}"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} must be a mapping")
    if not isinstance(entry.get("model_name"), str) or not entry["model_name"]:
        raise ConfigError(f"{where} needs model_name")
    params = entry.get("litellm_params")
    if not isinstance(params, dict) or not isinstance(params.get("model"), str):
        raise ConfigError(f"{where} needs litellm_params.model")
    for reference in _environment_references(entry):
        if reference.startswith(CREDENTIAL_PREFIX) and not any(
            credential_variable(key) == reference for key in secret
        ):
            raise ConfigError(f"{where}: credentials-secret has no key for {reference}")
    return entry


def _environment_references(value: Any) -> list[str]:
    """Every ``os.environ/NAME`` reference inside a raw entry."""
    if isinstance(value, str):
        return [value.removeprefix("os.environ/")] if value.startswith("os.environ/") else []
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _environment_references(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _environment_references(item)]
    return []

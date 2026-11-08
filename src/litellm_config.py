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

ROUTING_STRATEGIES = (
    "simple-shuffle",
    "least-busy",
    "usage-based-routing",
    "latency-based-routing",
    "cost-based-routing",
    "usage-based-routing-v2",
)
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Settings the charm manages, refused in the passthrough options (FR-022).
OWNED_ROUTER_SETTINGS = frozenset({"redis_host", "redis_port", "redis_password", "redis_url"})
OWNED_LITELLM_SETTINGS = frozenset({"cache_params"})
OWNED_ENVIRONMENT = frozenset(
    {
        "LITELLM_MASTER_KEY",
        "LITELLM_SALT_KEY",
        "DATABASE_URL",
        "STORE_MODEL_IN_DB",
        "PORT",
        "NUM_WORKERS",
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "LITELLM_MODEL_COST_MAP_URL",
        "DISABLE_ADMIN_UI",
        "LITELLM_CHARM_CONFIG_DIGEST",
    }
)
OWNED_ENVIRONMENT_PREFIXES = ("REDIS_", CREDENTIAL_PREFIX)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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
    # Named options (FR-024), by option name. Missing or None means unset.
    options: Mapping[str, Any] = field(default_factory=dict)
    # Passthrough options (FR-025).
    litellm_settings: str = ""
    router_settings: str = ""
    extra_env: str = ""


@dataclass(frozen=True)
class Rendered:
    """The workload's configuration file and environment."""

    config: dict[str, Any]
    environment: dict[str, str]
    # Passthrough settings a named option overrode, to be logged.
    conflicts: tuple[str, ...] = ()

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

    litellm_settings = _load_mapping(inputs.litellm_settings, "litellm-settings")
    router_settings = _load_mapping(inputs.router_settings, "router-settings")
    extra_env = _load_environment(inputs.extra_env)
    _refuse_owned("litellm-settings", litellm_settings, OWNED_LITELLM_SETTINGS)
    _refuse_owned("router-settings", router_settings, OWNED_ROUTER_SETTINGS)
    for name in extra_env:
        if name in OWNED_ENVIRONMENT or name.startswith(OWNED_ENVIRONMENT_PREFIXES):
            raise ConfigError(f"extra-env must not set {name}, the charm manages it")

    named = _named_settings(inputs.options)
    conflicts: list[str] = []
    for section, passthrough in (
        ("litellm_settings", litellm_settings),
        ("router_settings", router_settings),
    ):
        for key in sorted(set(passthrough) & set(named[section])):
            conflicts.append(f"{section}.{key}")
    if "LITELLM_LOG" in extra_env:
        conflicts.append("LITELLM_LOG")

    config: dict[str, Any] = {
        "model_list": model_list,
        "litellm_settings": {**litellm_settings, **named["litellm_settings"]},
        "router_settings": {**router_settings, **named["router_settings"]},
        "general_settings": {
            **named["general_settings"],
            "master_key": "os.environ/LITELLM_MASTER_KEY",
        },
    }
    if not config["router_settings"]:
        del config["router_settings"]

    environment: dict[str, str] = {
        **extra_env,
        "LITELLM_MASTER_KEY": secret[MASTER_KEY],
        # Models live in this file only, never in the database (FR-023).
        "STORE_MODEL_IN_DB": "False",
        # The admin UI is a separate feature.
        "DISABLE_ADMIN_UI": "True",
        # Always set: LiteLLM's own default is DEBUG.
        "LITELLM_LOG": named["log_level"],
        **_cost_map_environment(inputs.model_cost_map_url),
        **inputs.proxy,
    }
    for key, value in secret.items():
        if key not in (MASTER_KEY, ENCRYPTION_KEY):
            environment[credential_variable(key)] = value
    return Rendered(config=config, environment=environment, conflicts=tuple(conflicts))


def _named_settings(options: Mapping[str, Any]) -> dict[str, Any]:
    """The settings the named options set (FR-024). Unset options set nothing."""
    litellm: dict[str, Any] = {
        # Prompts and completions stay out of logging callbacks unless asked (SC-012).
        "turn_off_message_logging": not bool(options.get("log-request-content", False)),
    }
    router: dict[str, Any] = {}
    general: dict[str, Any] = {}

    strategy = options.get("routing-strategy") or "simple-shuffle"
    if strategy not in ROUTING_STRATEGIES:
        raise ConfigError(f"routing-strategy must be one of {', '.join(ROUTING_STRATEGIES)}")
    router["routing_strategy"] = strategy
    for option, key in (("num-retries", "num_retries"), ("request-timeout", "request_timeout")):
        value = options.get(option)
        if value is not None:
            if int(value) < 0:
                raise ConfigError(f"{option} must be 0 or more")
            litellm[key] = int(value)
    if options.get("drop-params"):
        litellm["drop_params"] = True
    fallbacks = _load_list(str(options.get("fallbacks") or ""), "fallbacks")
    if fallbacks:
        for item in fallbacks:
            if not (isinstance(item, dict) and len(item) == 1):
                raise ConfigError("fallbacks must be a list of {model: [fallback, ...]}")
            targets = next(iter(item.values()))
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                raise ConfigError("fallbacks must be a list of {model: [fallback, ...]}")
        router["fallbacks"] = fallbacks

    size = int(options.get("max-request-size-mb") or 0)
    if size < 0:
        raise ConfigError("max-request-size-mb must be 0 or more")
    if size:
        general["max_request_size_mb"] = size
    if options.get("serve-without-database"):
        general["allow_requests_on_db_unavailable"] = True
    if not options.get("spend-logs", True):
        general["disable_spend_logs"] = True

    log_level = str(options.get("log-level") or "INFO").upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"log-level must be one of {', '.join(LOG_LEVELS)}")
    return {
        "litellm_settings": litellm,
        "router_settings": router,
        "general_settings": general,
        "log_level": log_level,
    }


def _load_mapping(text: str, option: str) -> dict[str, Any]:
    return _load_yaml(text, option, dict)


def _refuse_owned(option: str, settings: Mapping[str, Any], owned: frozenset[str]) -> None:
    for key in sorted(settings):
        if key in owned or key in _ALWAYS_OWNED:
            raise ConfigError(f"{option} must not set {key}, the charm manages it")


# Keys refused in every section: what FR-022 lists, by LiteLLM's names.
_ALWAYS_OWNED = frozenset(
    {
        "master_key",
        "database_url",
        "salt_key",
        "store_model_in_db",
        "port",
        "num_workers",
        "coordination_redis",
        "model_list",
    }
)


def _load_environment(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines. Blank lines and lines starting with # are skipped."""
    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _ENV_NAME.match(name):
            raise ConfigError(f"extra-env line {number} must be KEY=VALUE")
        result[name] = value.strip()
    return result


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
    return _load_yaml(text, option, list)


def _load_yaml(text: str, option: str, kind: type) -> Any:
    """An option's YAML value of type ``kind``. Empty text means an empty value."""
    if not text.strip():
        return kind()
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{option} is not valid YAML") from exc
    if value is None:
        return kind()
    if not isinstance(value, kind):
        raise ConfigError(f"{option} must be a YAML {'list' if kind is list else 'mapping'}")
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

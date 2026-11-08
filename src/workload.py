# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""The Pebble layer for the LiteLLM proxy."""

from __future__ import annotations

from collections.abc import Mapping

import ops

from defaults import (
    ALIVE_CHECK,
    CONFIG_PATH,
    ENTRYPOINT,
    LIVENESS_PATH,
    PORT,
    READINESS_PATH,
    READY_CHECK,
    SERVICE,
    WORKING_DIR,
    WORKLOAD_UID,
)

DIGEST_VARIABLE = "LITELLM_CHARM_CONFIG_DIGEST"


def build_layer(environment: Mapping[str, str], config_digest: str) -> ops.pebble.LayerDict:
    """The layer running one LiteLLM worker on PORT.

    ``config_digest`` is part of the service environment, so that a changed
    configuration file changes the service and the replan restarts it:
    LiteLLM does not watch its configuration file.
    """
    command = f"{ENTRYPOINT} --config {CONFIG_PATH} --port {PORT} --num_workers 1"
    return {
        "summary": "LiteLLM proxy",
        "services": {
            SERVICE: {
                "override": "replace",
                "summary": "LiteLLM proxy",
                "command": command,
                "startup": "enabled",
                "user-id": WORKLOAD_UID,
                "working-dir": WORKING_DIR,
                "environment": {**environment, DIGEST_VARIABLE: config_digest},
                "on-check-failure": {ALIVE_CHECK: "restart"},
            }
        },
        "checks": {
            READY_CHECK: {
                "override": "replace",
                "level": "ready",
                "period": "10s",
                "timeout": "5s",
                "threshold": 3,
                "http": {"url": f"http://localhost:{PORT}{READINESS_PATH}"},
            },
            ALIVE_CHECK: {
                "override": "replace",
                "level": "alive",
                "period": "10s",
                "timeout": "5s",
                "threshold": 6,
                "http": {"url": f"http://localhost:{PORT}{LIVENESS_PATH}"},
            },
        },
    }

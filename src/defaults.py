# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Container layout of the upstream LiteLLM image, and charm-wide names.

Checked against ghcr.io/berriai/litellm-non_root:v1.108.3. Update together
with `upstream-source` in charmcraft.yaml (architecture.md§7).
"""

CONTAINER = "litellm"
SERVICE = "litellm"
READY_CHECK = "litellm-ready"
DRAIN_CHECK = "litellm-drain"
# While this file exists the drain check fails, so Kubernetes stops routing
# requests to the unit before it restarts (architecture.md§8).
DRAIN_FILE = "/tmp/litellm-drain"
# Juju probes readiness every 5 seconds. Then kube-proxy updates the Service.
DRAIN_SECONDS = 8
ALIVE_CHECK = "litellm-alive"

PORT = 4000
WORKLOAD_UID = 65534
WORKING_DIR = "/app"
ENTRYPOINT = "/app/docker/prod_entrypoint.sh"
CONFIG_DIR = "/etc/litellm"
CONFIG_PATH = f"{CONFIG_DIR}/config.yaml"

READINESS_PATH = "/health/readiness"
LIVENESS_PATH = "/health/liveliness"
COST_MAP_SOURCE_PATH = "/model/cost_map/source"

INGRESS_RELATION = "ingress"
DATABASE_RELATION = "database"
DATABASE_NAME = "litellm"
VALKEY_RELATION = "valkey"
# The key pattern requested from Valkey. LiteLLM does not prefix its keys.
VALKEY_KEYS = "*"
RESTART_RELATION = "restart"
PEER_RELATION = "litellm-peers"

CREDENTIALS_OPTION = "credentials-secret"
MASTER_KEY = "master-key"
ENCRYPTION_KEY = "encryption-key"

# Peer application data, written by the leader (architecture.md, Relations).
DATABASE_USED = "database-used"
KEY_DIGEST = "encryption-key-digest"

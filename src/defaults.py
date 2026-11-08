# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Container layout of the upstream LiteLLM image, and charm-wide names.

Checked against ghcr.io/berriai/litellm-non_root:v1.108.3. Update together
with `upstream-source` in charmcraft.yaml (architecture.md§7).
"""

CONTAINER = "litellm"
SERVICE = "litellm"
READY_CHECK = "litellm-ready"
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
PEER_RELATION = "litellm-peers"

CREDENTIALS_OPTION = "credentials-secret"
MASTER_KEY = "master-key"
ENCRYPTION_KEY = "encryption-key"

# Peer application data, written by the leader (architecture.md, Relations).
DATABASE_USED = "database-used"
KEY_DIGEST = "encryption-key-digest"

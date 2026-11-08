# Preface

What the charm deploys, how one reconcile turns Juju state into the LiteLLM workload, and which module implements each concern. Read this before changing anything under `src/` or `charmcraft.yaml`.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - Holistic reconcile

Every state-changing event runs the one `_reconcile` handler in `src/charm.py`, which derives the whole desired state and applies it. Do not add an event handler that changes the workload partially.

## §2 - Pure configuration

`src/litellm_config.py` turns config options, secret content, relation data and proxy settings into the LiteLLM configuration file and the workload environment without touching the Juju model or the container. Keep it pure so it is unit tested without `ops`.

## §3 - Status from collect-status

Unit status is set only in the collect-status handler. A reconcile that cannot proceed raises an error carrying the status, stops the workload, and leaves the message for collect-status to report.

## §4 - Credentials only in the environment

Secret values reach the workload as environment variables. The configuration file refers to them as `os.environ/<NAME>`. Never write a secret value into the file, a status message, a log line or relation data.

## §5 - No silent loss of the database

A deployment that has used the `database` relation never serves without it on its own. Units block until the relation returns or the `continue-without-database` action is run.

## §6 - Shared state with more than one unit

With more than one planned unit, the workload runs only while a Valkey relation provides the shared store. Never let several units serve with per-unit rate limits and budgets.

## §7 - Upstream image

The workload is the upstream `ghcr.io/berriai/litellm-non_root` image, pinned by version tag in `upstream-source` in `charmcraft.yaml`. `src/defaults.py` mirrors its layout. Update both together when the image changes its paths, user or port, and re-check the facts under Workload below.

## §8 - One unit restarts at a time

Configuration changes restart the workload through the rolling restart lock, never on every unit at once. Every restart drains first: the charm creates the drain file, waits for the unit to leave the Service, restarts LiteLLM, waits until it is ready, and removes the file. Do not restart the `litellm` service any other way.


## §9 - A leaving unit stands still

When a relation-departed hook names this unit as the departing one, the unit is being removed. It records that and stops reconciling, so that its relations going away never stop or restart LiteLLM while its pod is still in the Service. Kubernetes takes the pod out of the Service when it stops it.

# Concepts

## Modules

- `src/charm.py`: the `CharmBase` subclass. Event wiring, `_reconcile`, the rolling restart, the price map check, the `continue-without-database` action and status.
- `src/litellm_config.py`: options, secret content and relation data to `config.yaml` and the environment, with validation.
- `src/workload.py`: the Pebble layer. One service, `litellm`, and its checks.
- `src/defaults.py`: port, paths, user, service, check, relation and secret key names.
- `lib/charms/`: vendored `data_platform_libs.v0.data_interfaces` (PostgreSQL) and `traefik_k8s.v2.ingress`. Update them with `charmcraft fetch-libs`, never by hand.
- Python packages in `pyproject.toml`: `dpcharmlibs-interfaces` for the Valkey relation, which is the client library the Valkey charm documents, and `charmlibs-rollingops` for the restart lock. `charms.rolling_ops` is deprecated.

## Workload

These facts were checked against LiteLLM v1.108.3.

- The image runs `docker/prod_entrypoint.sh`, which ends in `exec litellm "$@"`. The charm runs `litellm --config /etc/litellm/config.yaml --port 4000 --num_workers 1` as UID 65534. Units scale out, workers do not.
- `GET /health/liveliness` and `GET /health/readiness` need no key. Readiness returns 503 while the database is not connected, unless `allow_requests_on_db_unavailable` is set. The Pebble checks use them as `alive` and `ready`. Juju's readiness probe on each pod follows Pebble's ready health every 5 seconds, and the `<app>` Service routes only to ready pods.
- Pebble counts a container with no checks as healthy, and a newly added check as up until it fails `threshold` times. The charm uses fast checks, and adds `litellm-drain`, an exec check at the `ready` level that fails while `/tmp/litellm-drain` exists, in the first hook that reaches Pebble.
- Measured on MicroK8s with Juju 3.6: a stopping pod leaves the Service within a second and no request fails while it stops. A new pod is ready about 2 seconds after its containers start, before the charm's first hook, and leaves the Service again about 10 seconds later when the drain check fails. Requests that reach it in between are refused. With two new pods at once the first hook came about 15 seconds after the start, and requests failed for up to about 21 seconds. This is why SC-008 and SC-009 bound failures by time for events that start pods.
- Juju means to keep a pod out of the Service until its start hook has run: the charm container's readiness asks the unit agent, which answers ready only after `start` (`internal/worker/uniter/probe.go` in Juju). But Juju wraps that in a Pebble check with a 10-second period and a threshold of 3 (`cmd/containeragent/initialize/command.go`), and Pebble counts a new check as up until it fails `threshold` times, so the gate only takes effect about 30 seconds into a pod. The charm finishes `start` only when LiteLLM is ready, so that a fix in Juju removes the gap without a charm change.
- LiteLLM does not watch its configuration file. A change needs a restart.
- With a database, startup runs `prisma migrate deploy`. The Prisma engines are in the image and `PRISMA_OFFLINE_MODE` is set, so nothing is downloaded.
- `LITELLM_LOCAL_MODEL_COST_MAP=True` forces the bundled price map and ignores any URL. The charm sets it unless `model-cost-map-url` is set, in which case it sets `LITELLM_MODEL_COST_MAP_URL` instead. `GET /model/cost_map/source`, with the master key, reports `fallback_reason` when the configured map was not used.
- `DISABLE_ADMIN_UI=True` turns off the admin UI, which is a separate feature.
- `litellm_settings.turn_off_message_logging` redacts content sent to logging callbacks. A request can override it with the `litellm-disable-message-redaction` header. `LITELLM_LOG` defaults to `DEBUG`, so the charm always sets it.

## Configuration and secrets

- `credentials-secret` names a Juju user secret. `master-key` becomes `LITELLM_MASTER_KEY`, `encryption-key` becomes `LITELLM_SALT_KEY`, and every other key `<name>` becomes `LITELLM_CREDENTIAL_<NAME>` for model entries to reference.
- `models` holds structured entries, rendered into `model_list`. `model-list` holds raw entries, appended unchanged.
- Named options map to `litellm_settings`, `router_settings`, `general_settings` and `LITELLM_LOG`. `litellm-settings`, `router-settings` and `extra-env` are passthrough. The charm-owned keys listed in `src/litellm_config.py` are refused in passthrough.
- Models are never stored in the database: `STORE_MODEL_IN_DB` is `False`.
- The model's `juju-http-proxy`, `juju-https-proxy` and `juju-no-proxy` reach the workload as `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` in upper and lower case. Juju fires no hook when they change, so `update-status` compares them with the plan.

## Relations

- `database` (`postgresql_client`, optional): `DATABASE_URL`. The leader records `database-used` and a SHA-256 digest of the encryption key in `litellm-peers` application data once a unit has been ready with the database.
- `valkey` (`valkey_client`, optional, required with more than one unit): the charm requests the key pattern `*`, because LiteLLM does not prefix its keys. The primary endpoint, username and password reach the workload as `REDIS_*` variables. `general_settings.coordination_redis` refers to them as `os.environ/`, and holds rate limits, spend counters and locks. LiteLLM attaches the same store to the router for usage and cooldowns, and a response cache enabled through `litellm-settings` uses the variables. Valkey with TLS blocks, until private CAs are supported. LiteLLM falls back to memory when the store is down, so the charm checks the endpoint on `update-status` and reports it.
- `ingress` (`ingress` v2, optional): publishes port 4000.
- `litellm-peers`: charm state. `restart`: owned by `charmlibs-rollingops`.

## Placement

A charm cannot set pod anti-affinity. Operators deploy with `--constraints "tags=anti-pod.app.kubernetes.io/name=<app>,anti-pod.topology-key=kubernetes.io/hostname"`, which Juju turns into required anti-affinity. With more units than nodes, the extra pods stay pending.

## Node loss

From Kubernetes 1.32 a node is marked not ready about 50 seconds after it stops answering, and its pods are then removed from Service endpoints. Until then, requests that land on it fail. Its StatefulSet pod is not recreated elsewhere while the node is unreachable.

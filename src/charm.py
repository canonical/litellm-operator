#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Kubernetes charm for the LiteLLM proxy.

* :mod:`litellm_config` -- options, secret content and relation data to the
  configuration file and environment
* :mod:`workload`       -- the Pebble layer
* :mod:`defaults`       -- container layout and names

Every state-changing event runs :meth:`LiteLLMCharm._reconcile`, which derives
the whole desired state (architecture.md§1). Status is set only in
collect-status (architecture.md§3).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request

import ops
from charmlibs.rollingops import OperationResult, RollingOpsManager
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer
from dpcharmlibs.interfaces import (
    RequirerCommonModel,
    ResourceRequirerEventHandler,
    ValkeyResponseModel,
)

import litellm_config
import workload
from defaults import (
    CONFIG_DIR,
    CONFIG_PATH,
    CONTAINER,
    COST_MAP_SOURCE_PATH,
    CREDENTIALS_OPTION,
    DATABASE_NAME,
    DATABASE_RELATION,
    DATABASE_USED,
    DRAIN_CHECK,
    DRAIN_FILE,
    DRAIN_SECONDS,
    ENCRYPTION_KEY,
    INGRESS_RELATION,
    KEY_DIGEST,
    MASTER_KEY,
    PEER_RELATION,
    PORT,
    READINESS_PATH,
    READY_CHECK,
    RESTART_RELATION,
    SERVICE,
    VALKEY_KEYS,
    VALKEY_RELATION,
    WORKLOAD_UID,
)

logger = logging.getLogger(__name__)

# Named options (FR-024), passed to litellm_config by option name.
NAMED_OPTIONS = (
    "routing-strategy",
    "num-retries",
    "request-timeout",
    "fallbacks",
    "log-level",
    "log-request-content",
    "drop-params",
    "max-request-size-mb",
    "serve-without-database",
    "spend-logs",
)


class StatusError(Exception):
    """The workload cannot run. Carries the status to report."""

    def __init__(self, status: ops.StatusBase):
        super().__init__(status.message)
        self.status = status


class LiteLLMCharm(ops.CharmBase):
    """Charm the LiteLLM proxy."""

    _stored = ops.StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.container = self.unit.get_container(CONTAINER)
        self._stored.set_default(cost_map_fallback="", store_unreachable=False, leaving=False)
        self.ingress = IngressPerAppRequirer(self, relation_name=INGRESS_RELATION, port=PORT)
        self.database = DatabaseRequires(
            self, relation_name=DATABASE_RELATION, database_name=DATABASE_NAME
        )
        self.valkey = ResourceRequirerEventHandler(
            self,
            relation_name=VALKEY_RELATION,
            requests=[RequirerCommonModel(resource=VALKEY_KEYS)],
            response_model=ValkeyResponseModel,
        )
        # One unit restarts at a time (architecture.md§8).
        self.rollingops = RollingOpsManager(
            self, callback_targets={"restart": self._restart}, peer_relation_name=RESTART_RELATION
        )
        # Set while a relation is being removed: in its relation-broken hook
        # the relation is still listed.
        self._database_broken = False
        self._valkey_broken = False

        for event in (
            self.on[CONTAINER].pebble_ready,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.secret_changed,
            # A new pod's first hook: add the drain check as soon as possible.
            self.on.install,
            self.on.leader_elected,
            self.on[PEER_RELATION].relation_changed,
            self.on[PEER_RELATION].relation_joined,
            self.database.on.database_created,
            self.database.on.endpoints_changed,
            self.valkey.on.resource_created,
            self.valkey.on.endpoints_changed,
            self.valkey.on.authentication_updated,
        ):
            framework.observe(event, self._reconcile)
        framework.observe(self.on.start, self._on_start)
        for relation in (PEER_RELATION, DATABASE_RELATION, VALKEY_RELATION):
            framework.observe(self.on[relation].relation_departed, self._on_relation_departed)
        framework.observe(self.on[DATABASE_RELATION].relation_broken, self._on_database_broken)
        framework.observe(self.on[VALKEY_RELATION].relation_broken, self._on_valkey_broken)
        framework.observe(
            self.on.continue_without_database_action, self._on_continue_without_database
        )
        framework.observe(self.on[CONTAINER].pebble_check_failed, self._on_check_event)
        framework.observe(self.on[CONTAINER].pebble_check_recovered, self._on_check_event)
        framework.observe(self.on.update_status, self._on_update_status)
        framework.observe(self.on.collect_unit_status, self._on_collect_unit_status)

    def _on_check_event(self, event: ops.EventBase) -> None:
        """Exists so that a check transition runs collect-status."""
        logger.debug("Pebble check event: %s", event)

    def _on_update_status(self, event: ops.EventBase) -> None:
        """Pick up a changed model proxy, which Juju fires no hook for, and check
        which price map is in use."""
        self._reconcile(event)
        if not self._serving():
            return
        service = self.container.get_plan().services[SERVICE]
        self._check_cost_map(service.environment.get("LITELLM_MASTER_KEY", ""))
        self._check_store(service.environment)

    def _on_start(self, event: ops.StartEvent) -> None:
        """Finish start only once LiteLLM is ready.

        Juju means to keep a pod out of the Service until its start hook has run
        (architecture.md, Workload), so the pod should not get requests before
        LiteLLM serves them.
        """
        self._reconcile(event)
        if self._serving():
            _wait_ready(timeout=120)

    def _on_relation_departed(self, event: ops.RelationDepartedEvent) -> None:
        """Note when this unit is the one leaving (architecture.md§9).

        A unit being removed leaves each of its relations before Kubernetes stops
        its pod. It keeps serving its current configuration until then, instead of
        reacting to its relations going away.
        """
        if event.departing_unit == self.unit:
            logger.info("This unit is leaving: keeping LiteLLM as it is")
            self._stored.leaving = True
            return
        self._reconcile(event)

    def _on_valkey_broken(self, event: ops.RelationBrokenEvent) -> None:
        self._valkey_broken = True
        self._reconcile(event)

    def _on_database_broken(self, event: ops.RelationBrokenEvent) -> None:
        self._database_broken = True
        self._reconcile(event)

    def _on_continue_without_database(self, event: ops.ActionEvent) -> None:
        """Serve with the master key only after the database was removed (FR-018)."""
        if not self.unit.is_leader():
            event.fail("run this action on the leader unit")
            return
        if self.model.relations[DATABASE_RELATION]:
            event.fail("the database relation still exists: remove it first")
            return
        peers = self._peer_data()
        if peers is None:
            event.fail("the peer relation is not ready yet")
            return
        peers.pop(DATABASE_USED, None)
        peers.pop(KEY_DIGEST, None)
        self._reconcile(event)
        event.set_results({"result": "serving with the master key only"})

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def _guard_readiness(self) -> None:
        """Keep a new pod out of the Service until LiteLLM is ready (architecture.md§8)."""
        if not self.container.can_connect():
            return
        if DRAIN_CHECK in self.container.get_plan().checks:
            return
        self.container.push(DRAIN_FILE, "", make_dirs=True)
        self.container.add_layer(CONTAINER, workload.drain_layer(), combine=True)

    def _reconcile(self, _: ops.EventBase) -> None:
        if self._stored.leaving:
            return
        if not self.container.can_connect():
            logger.debug("Pebble is not ready, waiting for pebble-ready")
            return
        self._guard_readiness()
        rendered = self._desired_state_or_stop()
        if rendered is None:
            return
        if "DATABASE_URL" in rendered.environment:
            self._record_database_use(rendered.environment["LITELLM_SALT_KEY"])
        if self._serving() and self._planned_digest() == rendered.digest:
            return
        if self._serving() and self.app.planned_units() > 1:
            # Other units are serving too: take turns (SC-009).
            self.rollingops.request_async_lock("restart")
            return
        self._apply(rendered, drain=False)

    def _restart(self) -> OperationResult:
        """Apply the configuration while holding the restart lock."""
        if self._stored.leaving:
            return OperationResult.RELEASE
        if not self.container.can_connect():
            return OperationResult.RETRY_RELEASE
        rendered = self._desired_state_or_stop()
        if rendered is not None and self._planned_digest() != rendered.digest:
            self._apply(rendered, drain=self._serving())
        return OperationResult.RELEASE

    def _desired_state_or_stop(self) -> litellm_config.Rendered | None:
        """The desired state, or None after stopping LiteLLM when it cannot run."""
        try:
            return self._desired_state()
        except StatusError as exc:
            logger.info("Not running LiteLLM: %s", exc.status.message)
            self._stop()
            return None

    def _apply(self, rendered: litellm_config.Rendered, *, drain: bool) -> None:
        """Push the configuration and restart LiteLLM, out of the Service meanwhile.

        With ``drain``, other units are serving: wait for the readiness probe to
        take this unit out of the Service before restarting it.
        """
        self.container.push(DRAIN_FILE, "", make_dirs=True)
        if drain:
            time.sleep(DRAIN_SECONDS)
        self.container.make_dir(
            CONFIG_DIR, make_parents=True, permissions=0o750, user_id=0, group_id=WORKLOAD_UID
        )
        self.container.push(
            CONFIG_PATH,
            rendered.config_yaml,
            make_dirs=True,
            permissions=0o640,
            user_id=0,
            group_id=WORKLOAD_UID,
        )
        layer = workload.build_layer(rendered.environment, rendered.digest)
        self.container.add_layer(CONTAINER, layer, combine=True)
        try:
            self.container.replan()
        except ops.pebble.ChangeError as exc:
            # For example, a database migration that another unit holds the lock
            # for. Pebble keeps restarting the service, and status shows it.
            logger.warning("LiteLLM did not start, Pebble restarts it: %s", exc)
        self.unit.set_ports(PORT)
        # Wait for the restarted workload before it rejoins the Service, so that
        # this hook's status is current.
        _wait_ready(timeout=120)
        self.container.remove_path(DRAIN_FILE, recursive=True)

    def _desired_state(self) -> litellm_config.Rendered:
        """Derive the workload's configuration, or raise :class:`StatusError`."""
        secret = self._credentials()
        database_url = self._database_url()
        peers = self._peer_data() or {}
        if not database_url and peers.get(DATABASE_USED):
            raise StatusError(
                ops.BlockedStatus(
                    "database relation removed: relate postgresql again, "
                    "or run continue-without-database"
                )
            )
        store = self._store_settings()
        if store is None and self.app.planned_units() > 1:
            raise StatusError(
                ops.BlockedStatus(
                    "more than one unit needs a Redis-compatible store: relate valkey"
                )
            )
        digest = peers.get(KEY_DIGEST)
        key = secret.get(ENCRYPTION_KEY, "")
        if database_url and digest and key and litellm_config.key_digest(key) != digest:
            raise StatusError(
                ops.BlockedStatus(f"{ENCRYPTION_KEY} changed: restore the key the database uses")
            )
        inputs = litellm_config.Inputs(
            models=str(self.config.get("models", "")),
            model_list=str(self.config.get("model-list", "")),
            model_cost_map_url=str(self.config.get("model-cost-map-url", "")),
            secret=secret,
            database_url=database_url,
            store=store,
            proxy=litellm_config.proxy_environment(os.environ),
            options={name: self.config.get(name) for name in NAMED_OPTIONS},
            litellm_settings=str(self.config.get("litellm-settings", "")),
            router_settings=str(self.config.get("router-settings", "")),
            extra_env=str(self.config.get("extra-env", "")),
        )
        try:
            if not litellm_config.has_models(inputs):
                raise StatusError(ops.WaitingStatus("waiting for model configuration"))
            rendered = litellm_config.render(inputs)
            for setting in rendered.conflicts:
                logger.warning("A named option overrides %s from a passthrough option", setting)
            return rendered
        except litellm_config.ConfigError as exc:
            raise StatusError(ops.BlockedStatus(str(exc))) from exc

    def _database_url(self) -> str:
        """The URL from the database relation, or empty without one (FR-016, FR-017)."""
        relations = self.model.relations[DATABASE_RELATION]
        if not relations or self._database_broken:
            return ""
        relation = relations[0]
        data = self.database.fetch_relation_data([relation.id]).get(relation.id, {})
        username, password, endpoints = (
            data.get("username"),
            data.get("password"),
            data.get("endpoints"),
        )
        if not (username and password and endpoints):
            raise StatusError(ops.WaitingStatus("waiting for the database relation"))
        return litellm_config.database_url(
            username, password, endpoints, data.get("database") or DATABASE_NAME
        )

    def _store_settings(self) -> litellm_config.StoreSettings | None:
        """The shared state store from the valkey relation, or None without one (FR-026)."""
        relation = self.model.get_relation(VALKEY_RELATION)
        if relation is None or self._valkey_broken or relation.app is None:
            return None
        try:
            model = self.valkey.interface.build_model(relation.id, component=relation.app)
            response = next(iter(model.requests), None)
        except Exception as exc:  # noqa: BLE001 - malformed or partial relation data
            logger.debug("Valkey relation data is not complete: %s", exc)
            response = None
        fields = {
            name: _plain(getattr(response, name, None))
            for name in ("endpoints", "username", "password", "tls")
        }
        if not (fields["endpoints"] and fields["username"] and fields["password"]):
            raise StatusError(ops.WaitingStatus("waiting for the valkey relation"))
        if str(fields["tls"]).lower() == "true":
            raise StatusError(ops.BlockedStatus("valkey with TLS is not supported yet"))
        try:
            host, port = litellm_config.parse_endpoint(str(fields["endpoints"]))
        except litellm_config.ConfigError as exc:
            raise StatusError(ops.BlockedStatus(str(exc))) from exc
        return litellm_config.StoreSettings(
            host=host,
            port=port,
            username=str(fields["username"]),
            password=str(fields["password"]),
        )

    def _check_store(self, environment: dict[str, str]) -> None:
        """Record whether the shared state store answers (FR-029)."""
        host, port = environment.get("REDIS_HOST"), environment.get("REDIS_PORT")
        if not (host and port):
            self._stored.store_unreachable = False
            return
        try:
            with socket.create_connection((host, int(port)), timeout=3):
                self._stored.store_unreachable = False
        except OSError as exc:
            logger.warning("The shared state store at %s:%s is unreachable: %s", host, port, exc)
            self._stored.store_unreachable = True

    def _peer_data(self) -> ops.RelationDataContent | None:
        relation = self.model.get_relation(PEER_RELATION)
        return None if relation is None else relation.data[self.app]

    def _record_database_use(self, key: str) -> None:
        """Remember that the database holds data, and under which key (FR-018, FR-021)."""
        peers = self._peer_data()
        if peers is None or not self.unit.is_leader():
            return
        if not peers.get(DATABASE_USED):
            peers[DATABASE_USED] = "true"
        if not peers.get(KEY_DIGEST):
            peers[KEY_DIGEST] = litellm_config.key_digest(key)

    def _credentials(self) -> dict[str, str]:
        """Content of the secret named by credentials-secret (FR-005)."""
        secret_id = self.config.get(CREDENTIALS_OPTION)
        if not secret_id:
            raise StatusError(
                ops.BlockedStatus(f"set {CREDENTIALS_OPTION} to a secret with {MASTER_KEY}")
            )
        try:
            return self.model.get_secret(id=str(secret_id)).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError) as exc:
            logger.warning("Cannot read %s %s: %s", CREDENTIALS_OPTION, secret_id, exc)
            raise StatusError(
                ops.BlockedStatus(
                    f"cannot read {CREDENTIALS_OPTION}: "
                    f"run `juju grant-secret <secret> {self.app.name}`"
                )
            ) from exc

    def _serving(self) -> bool:
        if not self.container.can_connect():
            return False
        services = self.container.get_services(SERVICE)
        return any(service.is_running() for service in services.values())

    def _planned_digest(self) -> str:
        service = self.container.get_plan().services.get(SERVICE)
        return "" if service is None else service.environment.get(workload.DIGEST_VARIABLE, "")

    def _stop(self) -> None:
        if self._serving():
            self.container.stop(SERVICE)

    def _check_cost_map(self, master_key: str) -> None:
        """Record whether LiteLLM fell back to its bundled price map (FR-031)."""
        request = urllib.request.Request(
            f"http://localhost:{PORT}{COST_MAP_SOURCE_PATH}",
            headers={"Authorization": f"Bearer {master_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                source = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.debug("Cannot read the price map source: %s", exc)
            return
        self._stored.cost_map_fallback = str(source.get("fallback_reason") or "")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def _on_collect_unit_status(self, event: ops.CollectStatusEvent) -> None:
        if self._stored.leaving:
            event.add_status(ops.MaintenanceStatus("leaving, serving until the pod stops"))
            return
        if not self.container.can_connect():
            event.add_status(ops.WaitingStatus("waiting for Pebble in the workload container"))
            return
        try:
            desired = self._desired_state().digest
        except StatusError as exc:
            event.add_status(exc.status)
            return
        if SERVICE not in self.container.get_plan().services:
            event.add_status(ops.MaintenanceStatus("configuring LiteLLM"))
            return
        if self.app.planned_units() > 1 and self._planned_digest() != desired:
            event.add_status(
                ops.MaintenanceStatus("waiting to restart with the new configuration")
            )
            return
        check = self.container.get_checks(READY_CHECK).get(READY_CHECK)
        # A check keeps its last result across a restart, so probe directly too.
        # The charm container shares the pod's network.
        if check is None or check.status != ops.pebble.CheckStatus.UP or not _ready():
            event.add_status(ops.WaitingStatus("waiting for LiteLLM to become ready"))
            return
        if self._stored.store_unreachable:
            event.add_status(
                ops.ActiveStatus("shared state store unreachable, limits apply per unit")
            )
            return
        if self._stored.cost_map_fallback:
            event.add_status(
                ops.ActiveStatus("model-cost-map-url not used, bundled price map in use")
            )
            return
        event.add_status(ops.ActiveStatus())


def _plain(value: object) -> object:
    """The value of a pydantic secret field, or the value itself."""
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else value


def _wait_ready(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not _ready() and time.monotonic() < deadline:
        time.sleep(2)


def _ready() -> bool:
    """Whether LiteLLM answers its readiness endpoint."""
    try:
        with urllib.request.urlopen(  # noqa: S310
            f"http://localhost:{PORT}{READINESS_PATH}", timeout=5
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


if __name__ == "__main__":  # pragma: nocover
    ops.main(LiteLLMCharm)

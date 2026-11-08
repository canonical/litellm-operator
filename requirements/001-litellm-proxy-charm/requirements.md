# Requirements: LiteLLM proxy charm

## Why

Applications that call several model servers each need the addresses, credentials and quirks of every one. The LiteLLM proxy gives them one OpenAI-compatible endpoint with keys, budgets, retries and fallbacks. Operators need it deployed and kept running by Juju on Kubernetes, without credentials in plain configuration, and without one unit or one node being a single point of failure.

## Stories

- **US-1 (P1)**: An operator needs one OpenAI-compatible endpoint that routes to the model servers they configure, so applications stop calling each model server directly. Motivates FR-001, FR-002, FR-003, FR-004, FR-007, FR-008, FR-009, FR-010, FR-011, FR-012, FR-013.
- **US-2 (P1)**: An operator needs upstream API keys, the master key and the encryption key kept in a Juju secret, so they never appear in configuration, status or logs. Motivates FR-005, FR-006, FR-021.
- **US-3 (P1)**: An operator needs several units that keep answering when a pod or a whole node is lost, or during a restart, upgrade or scaling, so the proxy is not a single point of failure for every LLM call. Motivates FR-014, FR-015, FR-026, FR-027, FR-028, FR-029.
- **US-4 (P2)**: An operator needs per-team virtual keys with budgets and spend tracked in PostgreSQL and shared by every unit, so several teams can share one proxy. Motivates FR-016, FR-017, FR-018, FR-030, FR-031, FR-032.
- **US-5 (P1)**: A contributor needs the integration tests to run in a disposable LXD virtual machine against a real local model server, so requests, streaming and errors are proven end to end. Motivates FR-019, FR-020.
- **US-6 (P2)**: An operator needs named options for common routing, retry and logging settings and a passthrough for the rest, while the charm keeps control of the settings it manages, so tuning needs no charm release. Motivates FR-022, FR-023, FR-024, FR-025.

## Requirements

- **FR-001**: THE SYSTEM SHALL run the LiteLLM proxy in Kubernetes, managed by Juju, from the upstream LiteLLM container image.
- **FR-002**: THE SYSTEM SHALL accept models as structured entries and validate each one. An entry has:
  - the public name clients use
  - the upstream provider and model
  - the upstream address
  - optionally, the name of a credential in the secret
  - optionally, input and output token prices
- **FR-003**: THE SYSTEM SHALL accept additional raw LiteLLM `model_list` entries and serve them together with the structured entries.
- **FR-004**: THE SYSTEM SHALL serve the OpenAI-compatible API, including model listing and streaming and non-streaming chat completions, on a Kubernetes service in the model.
- **FR-005**: THE SYSTEM SHALL read the master key and upstream credentials from a Juju secret that the operator names in config, and SHALL NOT write their values to config, status or logs.
- **FR-006**: WHILE a master key is set, THE SYSTEM SHALL refuse API requests that carry neither the master key nor a valid virtual key.
- **FR-007**: WHILE no model is configured, THE SYSTEM SHALL report a waiting status and serve no requests.
- **FR-008**: WHEN the model configuration or the secret changes, THE SYSTEM SHALL apply the change without a manual restart.
- **FR-009**: IF the configuration is invalid or a referenced credential is missing, THEN THE SYSTEM SHALL block with a message naming the problem.
- **FR-010**: THE SYSTEM SHALL report the workload's health in unit status and restart a workload that fails its health check.
- **FR-011**: THE SYSTEM SHALL support an ingress relation that publishes the API.
- **FR-012**: THE SYSTEM SHALL pass the model's HTTP proxy settings to the workload.
- **FR-013**: THE SYSTEM SHALL make no outbound network call at startup other than to configured upstreams, the related database and the related Redis-compatible store, and SHALL NOT download model price data unless a price map address is configured.
- **FR-014**: THE SYSTEM SHALL support several units serving the same configuration behind one Kubernetes service, and route requests only to units whose workload is ready.
- **FR-015**: WHEN a unit or the Kubernetes node it runs on is lost, THE SYSTEM SHALL keep serving from the units on other nodes, and return the lost unit to service without operator action once it can be scheduled again.
- **FR-016**: WHERE a PostgreSQL relation exists, THE SYSTEM SHALL store virtual keys, teams, budgets and spend in it, shared by every unit.
- **FR-017**: WHILE no PostgreSQL relation exists, THE SYSTEM SHALL serve with the master key only.
- **FR-018**: IF a unit that has used the database loses the relation, THEN THE SYSTEM SHALL block until the relation returns or the operator runs an action to continue without a database.
- **FR-019**: THE SYSTEM SHALL be built and published for amd64 and arm64, and its integration tests SHALL run on both architectures.
- **FR-020**: THE SYSTEM's integration tests SHALL run against an OpenAI-compatible model server on the host of a disposable LXD virtual machine when its address is given, and against a stand-in upstream in the test model when it is not.
- **FR-021**: THE SYSTEM SHALL read the encryption key for credentials stored in the database from the Juju secret and use the same key on every unit. IF the key changes after the database holds data encrypted with the previous one, THEN THE SYSTEM SHALL block.
- **FR-022**: THE SYSTEM SHALL own the following settings, and IF any of them is set through a passthrough option, THEN THE SYSTEM SHALL block with a message naming it:
  - the master key
  - the database address
  - the encryption key
  - the Redis-compatible store's address and credentials
  - the listening port
  - the worker count
  - the setting that keeps models out of the database
  - the price map source
- **FR-023**: THE SYSTEM SHALL keep models in its own configuration and never in the database, so that the configured models are the only models served.
- **FR-024**: THE SYSTEM SHALL offer named options for:
  - the routing strategy
  - the number of retries
  - the request timeout
  - fallbacks between models
  - the log level
  - whether request and response content is logged, off by default
  - dropping parameters an upstream does not support
  - the maximum request size
  - whether requests with the master key are served while the database is unavailable
  - whether spend logs are written
- **FR-025**: THE SYSTEM SHALL accept raw LiteLLM `litellm_settings`, `router_settings` and environment variables through passthrough options, with named options taking precedence.
- **FR-026**: WHERE a relation to a Redis-compatible store exists, THE SYSTEM SHALL share rate limit counters, budget and spend counters, usage-based routing state, upstream cooldowns and, when response caching is enabled, the response cache across every unit.
- **FR-027**: WHERE the application is deployed as the documentation describes and the cluster has at least as many nodes as there are units, THE SYSTEM SHALL place each unit on a different node.
- **FR-028**: IF more than one unit runs and no Redis-compatible store is related, THEN THE SYSTEM SHALL block with a message saying the store is required, and serve no requests. A single unit SHALL run without a store.
- **FR-029**: IF the related Redis-compatible store is unreachable, THEN THE SYSTEM SHALL keep serving with per-unit state and report that shared state is unavailable in unit status.
- **FR-030**: THE SYSTEM SHALL use the model price map bundled in the workload image unless the operator sets the address of another price map.
- **FR-031**: WHERE a price map address is set, THE SYSTEM SHALL load the map from it at startup. IF the map is unreachable or rejected, THEN THE SYSTEM SHALL use the bundled map and say so in unit status.
- **FR-032**: WHERE a structured model entry sets token prices, THE SYSTEM SHALL record spend for that model at those prices.

## Key Entities

- **Model entry**: one routable model, structured or raw, with a public name, an upstream provider and model, an upstream address, an optional credential name and optional token prices.
- **Credentials secret**: a Juju secret holding the master key, the encryption key and named upstream API keys.
- **Virtual key**: a key issued through the proxy, with an optional team and budget, stored in the database.
- **Shared state store**: a Redis-compatible store, required with more than one unit, holding the counters, routing state, cooldowns and cache every unit shares.
- **Price map**: the per-model token prices used to record spend, bundled in the workload image or loaded from an address the operator sets.

## Success Criteria

- **SC-001**: An operator goes from an empty model to an endpoint that answers a chat completion with 1 secret and at most 3 Juju commands, in under 10 minutes on a prepared controller.
- **SC-002**: A configuration change, including models, named options and passthrough settings, is served by every unit within 60 seconds of `juju config` returning.
- **SC-003**: 100% of requests without a valid master key or virtual key are refused while a master key is set.
- **SC-004**: 0 occurrences of the master key or an upstream key in `juju config`, `juju status` and the workload and charm logs during the integration tests.
- **SC-005**: Proxying adds less than 100 ms to the time to first token compared with calling the upstream directly, measured from the same VM.
- **SC-006**: The integration suite completes against a local model server in a fresh LXD VM in under 45 minutes, with every test passing.
- **SC-007**: A unit settles to active within 5 minutes of deployment on a prepared controller.
- **SC-008**: With 3 units under a steady load of at least 1 request per second, deleting one pod causes no failed requests while it stops, failed requests only within 30 seconds of its replacement starting, and the deleted unit is active again within 5 minutes.
- **SC-009**: With 3 units under the same load, a configuration change and a secret rotation each complete with fewer than 1% failed requests, and a charm refresh and scaling from 3 to 1 and back fail requests only within 30 seconds of a new unit's workload starting.
- **SC-010**: A virtual key created through one of 3 units is accepted by all 3 within 10 seconds.
- **SC-011**: Every release has a charm revision for amd64 and for arm64, and the integration tests, including the high-availability tests, pass on both.
- **SC-012**: 0 occurrences of prompt or completion content in the workload logs during the integration tests with the default configuration.
- **SC-013**: With 3 units on 3 nodes under the same load, losing one node leaves the endpoint answering every request again within 90 seconds, with fewer than 5% failed requests over the 5 minutes after the loss, and all 3 units are active within 5 minutes of the node returning.
- **SC-014**: With 3 units related to the store and a limit of 60 requests per minute on a key, sending 180 requests in one minute spread across all units admits no more than 66 of them.
- **SC-015**: For a model entry with configured prices, the spend recorded for a request is within 1% of its token counts multiplied by those prices.

## Edge Cases

- A structured entry and a raw entry with the same public name form one load-balanced group, as LiteLLM treats them.
- A secret that is not granted to the application, or is removed, blocks the unit with a message naming the secret.
- An unreachable upstream fails requests to that model with an upstream error. The proxy stays up and other models are unaffected.
- A passthrough setting that conflicts with a named option loses to the named option, and the conflict is logged.
- A lost node keeps receiving requests until Kubernetes marks it not ready, so requests that land on it in that window fail. Its unit cannot start elsewhere while the node is unreachable, because Juju runs units as stateful pods bound to their identity.
- The model's no-proxy setting can exclude a local model server from the HTTP proxy.
- An unreachable database is reported in status, and requests with the master key keep succeeding.
- A configured price map that is unreachable, not in LiteLLM's format, or much smaller than the bundled map is not used, and the unit status says so.
- Removing the store relation while more than one unit runs blocks the units as if no store had been related.
- A new pod joins the Service before the charm can check its workload, so requests that reach it fail until the charm takes it out again, about 10 seconds after it starts. Pod deletion, scaling up and refresh start new pods.

## Assumptions

- The local model server used in testing is OpenAI-compatible, may need no API key, and is reachable from LXD guests on the host's LXD bridge address.
- The upstream LiteLLM image is published for amd64 and arm64.
- CI has no local model server, so CI runs the integration tests against a stand-in upstream.
- CI runners host a single-node cluster, so CI covers the loss of a pod. Node-loss tests run on a 3-node cluster of LXD VMs and are skipped in CI.
- The database is the `postgresql-k8s` charm. The Redis-compatible store is the Charmed Valkey charm (`valkey`, track 9), published for amd64 and arm64. Their own availability is the concern of their charms.
- A price map set by address is a full map in LiteLLM's format. LiteLLM rejects a map much smaller than its bundled one, so a few models are priced through their entries instead.
- A charm cannot set pod anti-affinity itself. Juju sets it from a deploy constraint.
- Juju marks a new pod ready as soon as Pebble answers in each container, before the charm's first hook can add a check. Juju means to hold a pod back until its start hook has run, but its check for that counts as passing for the first 30 seconds of the pod. The charm cannot change the probes or the pod without access to the Kubernetes API.

## Out of Scope

- The LiteLLM admin UI ([#2](https://github.com/canonical/litellm-operator/issues/2)).
- Upstreams whose certificates are signed by a private CA, which would need a `receive-ca-cert` relation ([#3](https://github.com/canonical/litellm-operator/issues/3)).
- A relation that publishes the endpoint and issues each consuming application its own virtual key ([#4](https://github.com/canonical/litellm-operator/issues/4)). Until then, consuming charms are configured with the endpoint address and a key.
- Metrics, logs, dashboards and alert rules for the Canonical Observability Stack ([#5](https://github.com/canonical/litellm-operator/issues/5)).
- LiteLLM callbacks for tracing, telemetry, log sinks, alerting and guardrails ([#6](https://github.com/canonical/litellm-operator/issues/6)).

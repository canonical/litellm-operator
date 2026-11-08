# LiteLLM Operator

## Description

The LiteLLM Operator deploys and operates the [LiteLLM](https://github.com/BerriAI/litellm) proxy, an OpenAI-compatible gateway to more than 100 LLM providers, on Kubernetes. Applications call one endpoint, and the proxy routes each request to the model servers you configure, with keys, budgets, retries and fallbacks.

The charm runs the upstream `ghcr.io/berriai/litellm-non_root` image on amd64 and arm64. Credentials live in a Juju secret. PostgreSQL adds virtual keys and spend tracking, and Valkey shares rate limits and routing state between units. The behavior the charm commits to is defined in [`requirements/`](requirements/).

## Usage

Bootstrap a Kubernetes controller to Juju (Juju 3.6+) and create a model:

```shell
juju add-model litellm
```

### One unit

Store the master key, which clients use to call the proxy, and any upstream API keys in a secret. The master key must start with `sk-`.

```shell
juju add-secret litellm-credentials master-key=sk-<random> openai=sk-<openai key>
juju deploy litellm --channel edge --config credentials-secret=<secret URI>
juju grant-secret litellm-credentials litellm
```

Configure models. Each entry has the name clients use (`name`), the LiteLLM provider and model (`model`), optionally the upstream address (`api-base`), the secret key holding its API key (`credential`) and token prices (`input-cost-per-token`, `output-cost-per-token`):

```shell
juju config litellm models='
- name: gpt-4o
  model: openai/gpt-4o
  credential: openai
- name: local
  model: openai/my-model
  api-base: http://model-server.example:8000/v1
'
```

Clients call port 4000 of the `litellm` service with the master key:

```shell
curl http://litellm.<model>.svc.cluster.local:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-<random>" -H "Content-Type: application/json" \
  -d '{"model": "local", "messages": [{"role": "user", "content": "Hello"}]}'
```

A model that the structured entries cannot express goes into `model-list` as a raw LiteLLM `model_list` entry. Raw entries reference secret keys as `os.environ/LITELLM_CREDENTIAL_<KEY>`, upper-cased with `-` as `_`.

The unit waits until at least one model is configured, and blocks with a message naming the problem when the configuration is invalid or names a credential the secret does not have. Changes are applied without redeploying.

### Several units

More than one unit needs Valkey, which shares rate limits, budgets, routing state and cooldowns between units. Relate it before scaling out: without it, every unit blocks. Deploy with the constraint below so that each unit runs on its own node:

```shell
juju deploy valkey --channel 9/stable --trust
juju deploy litellm --channel edge -n 3 --config credentials-secret=<secret URI> \
  --constraints "tags=anti-pod.app.kubernetes.io/name=litellm,anti-pod.topology-key=kubernetes.io/hostname"
juju integrate litellm valkey
```

The constraint keeps any two units off the same node, so with more units than nodes the extra units stay pending. Configuration changes restart one unit at a time, and each unit leaves the Service before it restarts. When Kubernetes starts a new pod, for a replaced pod, scaling up or a refresh, some requests that reach the new pod fail for about 10 seconds, until the charm takes it out of the Service while LiteLLM starts. Clients that retry refused connections do not notice.

### Virtual keys and spend

Relate PostgreSQL to issue virtual keys with budgets and to track spend. Add an `encryption-key` to the secret first. It encrypts credentials stored in the database, and must not change afterwards:

```shell
juju update-secret litellm-credentials master-key=sk-<random> openai=sk-<openai key> encryption-key=<random>
juju deploy postgresql-k8s --channel 16/stable --trust
juju integrate litellm postgresql-k8s
```

Without the relation the proxy serves with the master key only. Once a deployment has used the database, removing the relation blocks the units, so that virtual keys never stop working silently. Relate it again, or run `juju run litellm/leader continue-without-database` to serve with the master key only.

Spend is calculated from the price map bundled in the image. Set prices on a model entry for models the map does not know, such as your own. To use a newer full map, set `model-cost-map-url` to an address serving a file in [LiteLLM's format](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json). The unit status says so if that map cannot be used.

### Options

| Option | Default | Effect |
|---|---|---|
| `routing-strategy` | `simple-shuffle` | How requests are spread across entries with the same name. |
| `num-retries` | LiteLLM's | Retries for a failed upstream request. |
| `request-timeout` | LiteLLM's | Seconds before an upstream request fails. |
| `fallbacks` | | YAML list of `{model: [fallback, ...]}`. |
| `log-level` | `INFO` | Workload log level. |
| `log-request-content` | `false` | Whether prompts and completions may be logged. |
| `drop-params` | `false` | Drop request parameters an upstream does not support. |
| `max-request-size-mb` | `0` | Largest request accepted, 0 for no limit. |
| `serve-without-database` | `false` | Keep serving master key requests while the database is down. |
| `spend-logs` | `true` | Write a spend log entry per request. |
| `model-cost-map-url` | | Address of a full price map to use instead of the bundled one. |

Other LiteLLM settings go into `litellm-settings` and `router-settings` as YAML, and environment variables into `extra-env` as `KEY=VALUE` lines. Named options win over them. Settings the charm manages, such as the master key, the database, the store, the port and the price map source, are refused there with a blocked status naming the setting.

## Relations / Integrations

- `database`: PostgreSQL (`postgresql-k8s`) for virtual keys, teams, budgets and spend. Optional.
- `valkey`: Valkey (`valkey`) for state shared between units. Required with more than one unit.
- `ingress`: Traefik (`traefik-k8s`) to publish the API outside the cluster. Optional.

## Security

- Keys reach the workload as environment variables and never appear in `juju config`, status, logs or the configuration file.
- Every request needs the master key or a virtual key.
- Prompts and completions are not logged unless `log-request-content` is set.
- The proxy makes no network calls at startup beyond the upstreams, the database, the store and a configured price map.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how features are defined, built and tested.

## License

Copyright 2026 Canonical Ltd. The LiteLLM Operator is free software, distributed under the Apache Software License, version 2.0. See [LICENSE](LICENSE) for more information. LiteLLM itself is MIT licensed by BerriAI, except its `enterprise/` directory.

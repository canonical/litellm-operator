# Preface

How the charm is tested: the unit suite, the integration suite, and the controllers the integration suite may run against. Read this before running or changing a test.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - Never a shared controller

Integration tests create and destroy models. Run them only on a controller you bootstrapped for the purpose, named in `JUJU_CONTROLLER`. Never run them, or deploy anything, on JAAS or on the current controller by default. `tests/integration/conftest.py` must refuse a controller with no cloud or with `jaas` in its name, and that check must not be weakened.

## §2 - Tests travel with behavior

A change to behavior adds or updates the tests that show it in the same commit. Unit tests cite the `FR-*` or `SC-*` identifier they verify when the behavior has one.

## §3 - Lint and unit before review

Run `tox -e lint,unit` before opening or updating a pull request. CI runs the same environments.


# Concepts

## Unit tests

`tests/unit/` uses `ops.testing` (Scenario) for charm state transitions and plain pytest for pure functions. `test_requirements_scripts.py` covers the repository tooling in `scripts/`.

```bash
tox -e format,lint,unit
# without tox:
uv run --group unit pytest tests/unit
uv run --group lint ruff check src tests scripts
```

## Integration tests

`tests/integration/` uses Jubilant against a real Kubernetes model.

- `test_published.py`: deploys a Charmhub release instead of a packed charm. Skipped unless `CHARM_CHANNEL` is set. `EXPECTED_CHARM_REVISION` pins the revision.

Environment variables: `JUJU_CONTROLLER` (required), `CHARM_PATH`, `LITELLM_IMAGE`, `KEEP_MODELS=1`, `LITELLM_TEST_UPSTREAM_URL`, `LITELLM_TEST_UPSTREAM_MODEL` and `MULTINODE=1`.

## Upstreams

The tests send real requests through the charm to an OpenAI-compatible upstream.

- With `LITELLM_TEST_UPSTREAM_URL` (ending in `/v1`) and `LITELLM_TEST_UPSTREAM_MODEL` set, the upstream is a model server on the host of the LXD VM, reached on the host's LXD bridge address. Do not name a specific product in committed files.
- Without them, as in CI, the tests deploy a second application of this charm named `upstream`, whose raw `model_list` entries use LiteLLM's `mock_response`. It answers streaming and non-streaming requests without network calls.

## Multi-node environment

Node-loss tests need a Kubernetes cluster of 3 nodes, which concierge cannot build. `tests/integration/multinode/setup.sh` launches 3 LXD VMs, joins them into one MicroK8s cluster and bootstraps a controller on it. `test_node_loss.py` runs only with `MULTINODE=1` and a `JUJU_CONTROLLER` on that cluster, and loses a node by taking the network of one VM down for a few minutes over SSH, so the test needs no access to the host's LXD or Juju client. CI does not run it.

## Local controller

The supported local setup is a throwaway LXD VM with its own MicroK8s controller, prepared by concierge with the `.github/actions/setup-k8s/concierge.yaml` that CI uses. `CONTRIBUTING.md` has the commands.

## CI

`.github/workflows/ci.yaml` runs lint and unit tests, packs the charm on amd64 and arm64, and runs `tests/integration` except `test_published.py` on a MicroK8s controller that `.github/actions/setup-k8s` bootstraps on each runner. `.kb/releases.md` describes the jobs that publish and verify a release.

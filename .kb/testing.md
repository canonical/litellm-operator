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

Environment variables: `JUJU_CONTROLLER` (required), `CHARM_PATH`, `LITELLM_IMAGE`, `KEEP_MODELS=1`.

## Local controller

The supported local setup is a throwaway LXD VM with its own MicroK8s controller, prepared by concierge with the `.github/actions/setup-k8s/concierge.yaml` that CI uses. `CONTRIBUTING.md` has the commands.

## CI

`.github/workflows/ci.yaml` runs lint and unit tests, packs the charm on amd64 and arm64, and runs `tests/integration` except `test_published.py` on a MicroK8s controller that `.github/actions/setup-k8s` bootstraps on each runner. `.kb/releases.md` describes the jobs that publish and verify a release.

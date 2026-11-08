# Contributing

## Layout

| Path                  | Responsibility                                                       |
| --------------------- | -------------------------------------------------------------------- |
| `src/`                | The charm.                                                           |
| `tests/unit/`         | Fast tests: pure functions, `ops.testing` (Scenario) state transitions and the repository tooling. |
| `tests/integration/`  | Jubilant tests that deploy to a real Kubernetes model.               |
| `requirements/`       | Committed feature requirements, one `NNN-slug/requirements.md` each. |
| `scripts/`            | Repository tooling: requirements adoption and checks.                |
| `AGENTS.md`, `.kb/`   | Knowledge base for coding agents.                                    |
| `.specify/`, `.claude/skills/speckit-*/` | Spec Kit output, regenerated with the Specify CLI. |

## Feature delivery

New features are defined with [Spec Kit](https://github.com/github/spec-kit) and delivered as a stack of pull requests:

1. Run `/speckit-specify`, `/speckit-clarify`, `/speckit-plan` and `/speckit-tasks`. Their output under `specs/` is gitignored.
2. Move the requirements into place with `tox -e adopt-requirements -- NNN-slug`, rewrite them into the format in [`requirements/.kb/requirements.md`](requirements/.kb/requirements.md), and run `tox -e check-requirements`.
3. Open a base pull request against `main` with the requirements and documentation only.
4. Stack one implementation pull request per deliverable on top of it, each with its tests.

Only `requirements/NNN-slug/requirements.md` is committed from a Spec Kit run. The `conventions` workflow fails a pull request that tracks a `spec.md`, anything under `specs/`, or any file besides `requirements.md` in a feature directory. [`.kb/spec-kit.md`](.kb/spec-kit.md) and [`.kb/pull-requests.md`](.kb/pull-requests.md) have the details.

## Unit tests and lint

```bash
tox -e format,lint,unit        # or: uvx --with tox-uv tox -e lint,unit
# without tox:
uv run --group unit pytest tests/unit
uv run --group lint ruff check src tests scripts
```

## Integration tests

The integration tests create and destroy models. They never use the current controller implicitly. Set `JUJU_CONTROLLER` to a controller you bootstrapped for the purpose. JAAS and other shared controllers are refused.

The supported way to run them is a throwaway LXD VM with its own controller:

```bash
charmcraft pack
lxc launch ubuntu:24.04 litellm-it --vm \
  -c limits.cpu=4 -c limits.memory=16GiB -d root,size=60GiB
lxc file push -r . litellm-it/root/litellm-operator
lxc exec litellm-it -- bash -c \
  'snap install concierge --classic && concierge prepare -c /root/litellm-operator/.github/actions/setup-k8s/concierge.yaml'
lxc exec litellm-it --cwd /root/litellm-operator -- env \
  JUJU_CONTROLLER=concierge-microk8s \
  uv run --group integration pytest tests/integration -v -x
lxc delete --force litellm-it   # when done
```

Environment variables: `CHARM_PATH` (packed charm), `LITELLM_IMAGE` (OCI image override), `KEEP_MODELS=1` (keep models for debugging). [`.kb/testing.md`](.kb/testing.md) has the details.

## Publishing

CI does this. Pull requests lint, unit test, build and run the integration tests on native amd64 and arm64 runners. Pushes to `main` then also upload both charms and the image named by `upstream-source`, release them to `latest/edge`, and run `test_published.py` against that release on both architectures. Until the repository has a `charmcraft.yaml`, only lint and unit tests run.

To try a branch before merging, publish it to a Charmhub branch, which expires 30 days after its last release:

```bash
gh workflow run CI --ref <git-branch> -f charmhub-branch=pr-<N>
```

Publishing uses the `CHARMHUB_TOKEN` repository secret, created with `charmcraft login --export` and limited to this charm. Release to other channels by hand with `charmcraft release`. [`.kb/releases.md`](.kb/releases.md) has the details.

## Conventions

- Runtime dependencies go in `pyproject.toml` and are locked in `uv.lock`. Run `uv lock` after changing them.
- Start every new source file with `# Copyright <year> Canonical Ltd.` and `# See LICENSE file for licensing details.`
- Do not commit built `*.charm` files.

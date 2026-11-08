# Preface

How the charm and its image reach Charmhub, and how a release is verified. Read this before changing `.github/workflows/ci.yaml`, `.github/actions/setup-k8s/`, `upstream-source` in `charmcraft.yaml`, or the release process.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - CI publishes edge

Only the `publish` job on a push to `main` uploads and releases to `latest/edge`. Release to other channels by hand with `charmcraft release`.

## §2 - Charms before resource

Charmhub accepts a resource only when an uploaded revision declares it, so the job uploads both charms before the image.

## §3 - Re-runs reuse revisions

A re-run of `publish` uploads the same files again. Charmhub refuses a duplicate and names the existing revision, and the job releases that revision. Keep that behavior when changing the upload step.

## §4 - Names come from charmcraft.yaml

The workflow reads the charm name from `name` and the image from `upstream-source` in `charmcraft.yaml`, and names the OCI image resource `<name>-image`. Declare the resource under that name.

## §5 - Pinned actions

Every third-party action is pinned to a full commit SHA with its version in a comment. Renovate proposes the updates.


# Concepts

## Pipeline

1. `lint-unit` runs `tox -e lint,unit` on every pull request and push.
2. `build` packs the charm on amd64 and arm64 runners.
3. `integration` bootstraps MicroK8s with `.github/actions/setup-k8s` on each architecture and runs `tests/integration` against the packed charm, except `test_published.py`.
4. On a push to `main`, `publish` uploads `<name>_amd64.charm` and `<name>_arm64.charm`, uploads the multi-arch image named by `upstream-source` as one `<name>-image` resource revision, and releases both charm revisions to `latest/edge` with it.
5. `verify-published` deploys the release from Charmhub on both architectures and runs `test_published.py` against the revision `publish` reported.

The `conventions` workflow runs `scripts/check_requirements.py` on every pull request and push (requirements.md§6).

## Before the charm exists

`build`, `integration`, `publish` and `verify-published` run only when `charmcraft.yaml` exists. `lint-unit` reports it as the `charm` output and the later jobs skip without it. The first pull request that adds `charmcraft.yaml` also adds the integration tests and `test_published.py` that these jobs run.

## Charmhub branches

`workflow_dispatch` with `charmhub-branch=pr-<N>` publishes any ref to `latest/edge/pr-<N>`. A Charmhub branch expires 30 days after its last release.

## Credentials

`publish` authenticates with the `CHARMHUB_TOKEN` repository secret, created with `charmcraft login --export` and limited to this charm.

## Dependencies

Renovate (`renovate.json`) proposes dependency updates. Runtime dependencies are declared in `pyproject.toml` and locked in `uv.lock`. Run `uv lock` after changing them.

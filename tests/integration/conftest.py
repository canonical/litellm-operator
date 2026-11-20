# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Fixtures for the LiteLLM integration tests (Jubilant).

Safety: these tests create and destroy models. They NEVER fall back to the
current Juju controller. Set ``JUJU_CONTROLLER`` to a controller you own on a
Kubernetes cloud, such as a MicroK8s controller in an LXD VM. JAAS and other
shared controllers are refused (testing.md§1).

Environment:
    JUJU_CONTROLLER              required. Controller to create temporary models on
    CHARM_PATH                   optional. Packed .charm (default: ./litellm_*.charm)
    LITELLM_IMAGE                optional. OCI image for the litellm-image resource
    KEEP_MODELS                  optional. "1" keeps models after the run
    LITELLM_TEST_UPSTREAM_URL    optional. OpenAI-compatible server, ending in /v1
    LITELLM_TEST_UPSTREAM_MODEL  optional. A model that server serves
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import jubilant
import pytest
import yaml

from defaults import PORT
from helpers import namespace

ROOT = Path(__file__).resolve().parents[2]
METADATA = yaml.safe_load((ROOT / "charmcraft.yaml").read_text())
APP = METADATA["name"]
UPSTREAM_APP = "upstream"
WAIT_TIMEOUT = 20 * 60
# Juju schedules Kubernetes pods with an amd64 node selector unless told otherwise.
ARCH = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
MASTER_KEY = "sk-integration-master"
UPSTREAM_KEY = "sk-integration-upstream"
# The text the stand-in upstream answers with.
MOCK_ANSWER = "hello from the stand-in upstream"


def _assert_safe_controller(name: str) -> None:
    """Refuse shared controllers. JAAS controllers report no cloud."""
    raw = subprocess.run(
        ["juju", "controllers", "--format=json"], check=True, capture_output=True, text=True
    ).stdout
    controllers = json.loads(raw).get("controllers", {})
    if name not in controllers:
        pytest.exit(f"JUJU_CONTROLLER={name!r} is not a known controller", returncode=2)
    details = controllers[name]
    if "jaas" in name.lower() or not details.get("cloud"):
        pytest.exit(
            f"Refusing to run integration tests on {name!r}: it looks like a shared JAAS "
            "controller. Bootstrap a local Kubernetes controller and point JUJU_CONTROLLER at it.",
            returncode=2,
        )


@pytest.fixture(scope="session")
def controller() -> str:
    name = os.environ.get("JUJU_CONTROLLER", "").strip()
    if not name:
        pytest.exit(
            "Set JUJU_CONTROLLER to a local Kubernetes controller you own. "
            "The integration tests never use the current controller implicitly.",
            returncode=2,
        )
    _assert_safe_controller(name)
    return name


@pytest.fixture(scope="module")
def juju(controller: str, request: pytest.FixtureRequest) -> Iterator[jubilant.Juju]:
    keep = os.environ.get("KEEP_MODELS", "") == "1"
    # A short update-status interval keeps unit status fresh between hooks.
    config = {"update-status-hook-interval": "1m"}
    with jubilant.temp_model(keep=keep, controller=controller, config=config) as juju:
        juju.wait_timeout = WAIT_TIMEOUT
        juju.cli("set-model-constraints", f"arch={ARCH}")
        yield juju
        if request.session.testsfailed:
            print(juju.debug_log(limit=300), end="")


@pytest.fixture(scope="session")
def charm() -> Path:
    if path := os.environ.get("CHARM_PATH"):
        return Path(path).resolve()
    packed = sorted(ROOT.glob(f"{APP}_*.charm"))
    if not packed:
        pytest.exit("No packed charm found: run `charmcraft pack` or set CHARM_PATH", returncode=2)
    return packed[-1]


@pytest.fixture(scope="session")
def resources() -> dict[str, str]:
    upstream = METADATA["resources"]["litellm-image"]["upstream-source"]
    return {"litellm-image": os.environ.get("LITELLM_IMAGE", upstream)}


@dataclass(frozen=True)
class Upstream:
    """An OpenAI-compatible server the charm under test routes to."""

    api_base: str
    model: str
    stand_in: bool

    def entry(self, name: str, **extra: object) -> dict:
        """A structured model entry for this upstream, using the "upstream" credential."""
        return {
            "name": name,
            "model": f"openai/{self.model}",
            "api-base": self.api_base,
            "credential": "upstream",
            **extra,
        }

    def models(self, *names: str, **extra: object) -> str:
        """The `models` option for entries with these names."""
        return yaml.safe_dump([self.entry(name, **extra) for name in names])


def credentials(**extra: str) -> dict[str, str]:
    """Content of the credentials secret the tests deploy with."""
    return {"master-key": MASTER_KEY, "upstream": UPSTREAM_KEY, **extra}


@pytest.fixture(scope="module")
def upstream(juju: jubilant.Juju, charm: Path, resources: dict[str, str]) -> Upstream:
    """The local model server when given, or else a stand-in deployed in the model (FR-020).

    The stand-in is a second application of this charm whose raw model entry
    answers with LiteLLM's ``mock_response``, so it needs no network.
    """
    url = os.environ.get("LITELLM_TEST_UPSTREAM_URL", "").strip()
    if url:
        model = os.environ.get("LITELLM_TEST_UPSTREAM_MODEL", "").strip()
        if not model:
            pytest.exit(
                "LITELLM_TEST_UPSTREAM_URL needs LITELLM_TEST_UPSTREAM_MODEL", returncode=2
            )
        return Upstream(api_base=url.rstrip("/"), model=model, stand_in=False)

    secret = juju.add_secret("upstream-credentials", {"master-key": UPSTREAM_KEY})
    model_list = [
        {
            "model_name": "mock",
            "litellm_params": {"model": "openai/mock", "mock_response": MOCK_ANSWER},
        }
    ]
    # On the multi-node cluster the stand-in stays on the VM the tests run on,
    # which the node-loss test never cuts off.
    pin = {"tags": f"kubernetes.io/hostname={os.uname().nodename}"}
    juju.deploy(
        charm,
        UPSTREAM_APP,
        resources=resources,
        config={"credentials-secret": secret, "model-list": yaml.safe_dump(model_list)},
        constraints=pin if os.environ.get("MULTINODE") == "1" else None,
    )
    juju.grant_secret(secret, UPSTREAM_APP)
    juju.wait(lambda status: jubilant.all_active(status, UPSTREAM_APP))
    return Upstream(
        api_base=f"http://{UPSTREAM_APP}.{namespace(juju)}.svc.cluster.local:{PORT}/v1",
        model="mock",
        stand_in=True,
    )

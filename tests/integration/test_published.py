# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Check a Charmhub release: the charm and its image, as a user would deploy them.

Skipped unless ``CHARM_CHANNEL`` is set. ``EXPECTED_CHARM_REVISION`` optionally
pins the revision the channel must serve on this architecture.
"""

from __future__ import annotations

import os

import jubilant
import pytest
import yaml

from conftest import APP, ARCH, MASTER_KEY, METADATA, MOCK_ANSWER
from helpers import answer, chat, service_address

CHANNEL = os.environ.get("CHARM_CHANNEL", "")
EXPECTED_REVISION = os.environ.get("EXPECTED_CHARM_REVISION", "")

pytestmark = pytest.mark.skipif(not CHANNEL, reason="CHARM_CHANNEL is not set")


@pytest.fixture(scope="module", autouse=True)
def deployed(juju: jubilant.Juju):
    secret = juju.add_secret("published-credentials", {"master-key": MASTER_KEY})
    model_list = [
        {
            "model_name": "mock",
            "litellm_params": {"model": "openai/mock", "mock_response": MOCK_ANSWER},
        }
    ]
    # No resources are passed, so the image can only come from Charmhub.
    # Juju can't pick a release from a branch channel (track/risk/branch) by
    # itself: it needs the base and architecture given explicitly.
    juju.deploy(
        APP,
        channel=CHANNEL,
        base=METADATA["base"],
        constraints={"arch": ARCH},
        config={"credentials-secret": secret, "model-list": yaml.safe_dump(model_list)},
    )
    juju.grant_secret(secret, APP)
    juju.wait(lambda status: jubilant.all_active(status, APP))


def test_channel_serves_expected_revision(juju: jubilant.Juju):
    app = juju.status().apps[APP]
    assert app.charm_channel == CHANNEL
    if EXPECTED_REVISION:
        assert app.charm_rev == int(EXPECTED_REVISION)


def test_answers(juju: jubilant.Juju):
    assert answer(chat(service_address(juju, APP), "mock", MASTER_KEY)) == MOCK_ANSWER

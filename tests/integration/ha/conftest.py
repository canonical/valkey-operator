# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from collections.abc import Generator
from typing import Any

import jubilant
import pytest

from literals import Substrate

from .helpers.helpers import deploy_chaos_mesh, destroy_chaos_mesh


@pytest.fixture(scope="module")
def chaos_mesh(juju: jubilant.Juju, substrate: Substrate) -> Generator[None, Any, Any]:
    assert juju.model, "Juju model is not set. Ensure that the test is running with a Juju model."
    if substrate == Substrate.K8S:
        deploy_chaos_mesh(juju.model)
        yield
        destroy_chaos_mesh(juju.model)
    else:
        yield

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

from pytest import Parser

logger = logging.getLogger(__name__)


def pytest_addoption(parser: Parser):
    parser.addoption(
        "--substrate",
        action="store",
        help="Substrate to test, either vm or k8s",
        choices=("vm", "k8s"),
        default="k8s",
    )

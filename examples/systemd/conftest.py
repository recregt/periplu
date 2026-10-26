# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pathlib import Path

import pytest

from periplu import container as periplu


@pytest.fixture(scope="session")
def image():
    return periplu.image(Path(__file__).parent / "Containerfile")


@pytest.fixture
def machine(request, image):
    with periplu.admitted(
        request.node.nodeid, image, run_args=[], systemd=True, variant="fresh"
    ) as name:
        yield periplu.Container(name)

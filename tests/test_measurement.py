# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import subprocess

import pytest

from periplu import container


def _inspected(path: str):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=f"{path}\n", stderr="")

    return run


@pytest.mark.parametrize("path", ["", "/"])
def test_a_container_without_a_cgroup_of_its_own_is_not_measured(monkeypatch, path):
    monkeypatch.setattr(container.subprocess, "run", _inspected(path))
    assert container._cgroup_of("exited") is None


def test_a_running_container_is_measured_in_its_own_cgroup(monkeypatch):
    monkeypatch.setattr(container.subprocess, "run", _inspected("/user.slice/libpod-1.scope"))
    assert container._cgroup_of("running") == container.resources.CGROUP_ROOT / (
        "user.slice/libpod-1.scope"
    )

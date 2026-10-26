# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import json

import pytest

from periplu import resources, settings

CORES = 4
INSTANCES = 100
MEMORY = 1000


@pytest.fixture(autouse=True)
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv(settings.CALIBRATE_ENV, raising=False)
    (tmp_path / "test_suite.py").write_text("def test_one():\n    pass\n")
    settings.configure(
        settings.Settings("suite", tmp_path, tmp_path / "baseline.json", tmp_path / "failures")
    )
    monkeypatch.setattr(resources, "cpu_capacity", lambda: CORES)
    monkeypatch.setattr(resources, "memory_available", lambda: MEMORY)
    monkeypatch.setattr(resources, "inotify_available", lambda: INSTANCES)
    resources._defined.cache_clear()


def _running(*instances: int) -> dict:
    entries = {
        f"t{index}": {"peak_bytes": 0, "cpu_cores": 0.0, "inotify_instances": count}
        for index, count in enumerate(instances)
    }
    return {"running": entries, "memory": MEMORY, "inotify": INSTANCES}


def _demand(instances: int | None) -> resources.Demand:
    return resources.Demand(peak_bytes=0, cpu_cores=0.0, seconds=1.0, inotify_instances=instances)


def test_a_test_fits_while_every_instance_it_needs_is_still_free():
    assert resources._fits(_running(60, 30), _demand(10))
    assert not resources._fits(_running(60, 30), _demand(11))


def test_an_empty_machine_admits_any_test_so_nothing_waits_forever():
    assert resources._fits(_running(), _demand(INSTANCES * 2))


def test_a_test_whose_instances_are_unknown_waits_for_an_empty_machine():
    assert not resources._fits(_running(1), _demand(None))


def test_calibration_admits_a_test_whose_instances_are_unknown(monkeypatch):
    monkeypatch.setenv(settings.CALIBRATE_ENV, "1")
    assert resources._fits(_running(1), _demand(None))


def test_a_test_without_its_own_count_borrows_the_largest_known_one():
    largest = INSTANCES // 2
    known = {"a": _demand(None), "b": _demand(largest - 1), "c": _demand(largest)}
    assert resources.demand_for("a", known).inotify_instances == largest
    assert resources.demand_for("unseen", known).inotify_instances == largest


def test_the_worker_cap_never_exceeds_the_free_instances():
    known = {f"test_suite.py::test_{n}": _demand(INSTANCES // 4) for n in range(10)}
    baseline = settings.current().baseline
    baseline.write_text(json.dumps({test: vars(demand) for test, demand in known.items()}))
    (settings.current().root / "test_suite.py").write_text(
        "".join(f"def test_{n}():\n    pass\n" for n in range(10))
    )
    assert resources.worker_cap() == INSTANCES // (INSTANCES // 4)


def test_a_parametrized_test_keeps_its_measured_history():
    assert resources._defined("test_suite.py::test_one[git]")

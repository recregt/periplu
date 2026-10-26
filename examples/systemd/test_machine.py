# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from periplu.container import wait_any

UNIT = "periplu-example.service"
WORK = "echo started > /run/reached && read line < /run/release"


def test_the_machine_has_finished_booting_when_the_test_starts(machine):
    state = machine.exec("systemctl", "is-system-running").stdout.strip()
    assert state in {"running", "degraded"}


def test_a_unit_is_held_at_a_point_and_released_without_a_clock(machine):
    machine.exec("mkfifo", "/run/reached", "/run/release", check=True)
    reached = machine.start_background("cat", "/run/reached")
    unit = machine.start_background("systemd-run", "--unit", UNIT, "--wait", "sh", "-c", WORK)

    wait_any(reached.ended, unit.ended)

    assert reached.ended.is_set(), unit.wait()
    assert machine.exec("systemctl", "is-active", UNIT).stdout.strip() == "active"
    machine.exec("sh", "-c", "echo go > /run/release", check=True)
    assert unit.wait().returncode == 0

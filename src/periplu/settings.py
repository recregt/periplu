# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import os
from dataclasses import dataclass
from pathlib import Path

SESSION_ENV = "PERIPLU_SESSION"
CONTROLLER_ENV = "PERIPLU_CONTROLLER_PID"
CALIBRATE_ENV = "PERIPLU_CALIBRATE"


@dataclass(frozen=True)
class Settings:
    project: str
    root: Path
    baseline: Path
    failures: Path

    @property
    def cache(self) -> Path:
        home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        return home / "periplu" / self.project


class _State:
    settings: Settings | None = None


def configure(settings: Settings) -> None:
    _State.settings = settings


def current() -> Settings:
    if _State.settings is None:
        raise RuntimeError("periplu is not configured; is the plugin loaded?")
    return _State.settings


def session() -> str:
    return os.environ.get(SESSION_ENV, "")


def calibrating() -> bool:
    return os.environ.get(CALIBRATE_ENV) == "1"

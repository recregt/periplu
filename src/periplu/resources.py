# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import contextlib
import fcntl
import functools
import json
import math
import os
import select
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from periplu import settings

CGROUP_ROOT = Path("/sys/fs/cgroup")
PRESSURE_ROOT = Path("/proc/pressure")
PROC_ROOT = Path("/proc")
INOTIFY_LIMIT = Path("/proc/sys/fs/inotify/max_user_instances")
INOTIFY = "anon_inode:inotify"

KEPT_RUNS = 8


def _history() -> Path:
    return settings.current().cache / "resources.jsonl"


def _ledger() -> Path:
    return settings.current().cache / "admission.json"


def _ledger_lock() -> Path:
    return settings.current().cache / "admission.lock"


def _wakers() -> Path:
    return settings.current().cache / "admission"


@dataclass(frozen=True)
class Demand:
    peak_bytes: int
    cpu_cores: float
    seconds: float
    inotify_instances: int | None


@functools.cache
def _defined(test: str) -> bool:
    if test.startswith("snapshot::"):
        return True
    path, _, name = test.partition("::")
    try:
        source = (settings.current().root / path).read_text()
    except OSError:
        return False
    return f"def {name.partition('[')[0]}(" in source


def _runs_by_test() -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = {}
    with contextlib.suppress(FileNotFoundError):
        for line in _history().read_text().splitlines():
            with contextlib.suppress(ValueError, KeyError):
                run = json.loads(line)
                runs.setdefault(run["test"], []).append(run)
    return {test: kept[-KEPT_RUNS:] for test, kept in runs.items() if _defined(test)}


def _baseline() -> dict[str, dict]:
    with contextlib.suppress(FileNotFoundError):
        return json.loads(settings.current().baseline.read_text())
    return {}


def estimates() -> dict[str, Demand]:
    measured = _runs_by_test()
    known: dict[str, Demand] = {}
    for test, entry in _baseline().items():
        if not _defined(test):
            continue
        known[test] = Demand(
            entry["peak_bytes"],
            entry["cpu_cores"],
            entry["seconds"],
            entry.get("inotify_instances"),
        )
    for test, history in measured.items():
        latest = history[-1].get("variant", "fresh")
        runs = [run for run in history if run.get("variant", "fresh") == latest]
        instances = [
            run["inotify_instances"] for run in runs if run.get("inotify_instances") is not None
        ]
        known[test] = Demand(
            peak_bytes=max(run["peak_bytes"] for run in runs),
            cpu_cores=max(run["cpu_seconds"] / run["seconds"] for run in runs),
            seconds=statistics.median(run["seconds"] for run in runs),
            inotify_instances=max(instances) if instances else None,
        )
    return known


def _snapshot_path(session: str) -> Path:
    return settings.current().cache / f"estimates-{session}.json"


def snapshot(session: str) -> None:
    cache = settings.current().cache
    cache.mkdir(parents=True, exist_ok=True)
    for stale in cache.glob("estimates-*.json"):
        stale.unlink(missing_ok=True)
    _snapshot_path(session).write_text(
        json.dumps({test: vars(demand) for test, demand in estimates().items()})
    )


def snapshotted(session: str) -> dict[str, Demand]:
    with contextlib.suppress(FileNotFoundError, ValueError):
        return {
            test: Demand(**demand)
            for test, demand in json.loads(_snapshot_path(session).read_text()).items()
        }
    return estimates()


def demand_for(test: str, known: dict[str, Demand]) -> Demand | None:
    if test in known:
        return _with_known_instances(known[test], known)
    if not known:
        return None
    instances = _measured_instances(known.values())
    return Demand(
        peak_bytes=max(demand.peak_bytes for demand in known.values()),
        cpu_cores=max(demand.cpu_cores for demand in known.values()),
        seconds=max(demand.seconds for demand in known.values()),
        inotify_instances=max(instances) if instances else None,
    )


def _measured_instances(demands) -> list[int]:
    return [demand.inotify_instances for demand in demands if demand.inotify_instances is not None]


def _with_known_instances(demand: Demand, known: dict[str, Demand]) -> Demand:
    if demand.inotify_instances is not None:
        return demand
    instances = _measured_instances(known.values())
    if not instances:
        return demand
    return Demand(demand.peak_bytes, demand.cpu_cores, demand.seconds, max(instances))


def _own_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return CGROUP_ROOT / line[3:].lstrip("/")
    return CGROUP_ROOT


def _ancestors(cgroup: Path):
    while cgroup != CGROUP_ROOT and CGROUP_ROOT in cgroup.parents:
        yield cgroup
        cgroup = cgroup.parent


def _read(path: Path) -> str | None:
    with contextlib.suppress(OSError):
        return path.read_text().strip()
    return None


def cpu_capacity() -> float:
    capacity = float(len(os.sched_getaffinity(0)))
    for cgroup in _ancestors(_own_cgroup()):
        limit = _read(cgroup / "cpu.max")
        if limit and not limit.startswith("max"):
            quota, period = limit.split()
            capacity = min(capacity, int(quota) / int(period))
    return capacity


def memory_available() -> int:
    available = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            available = int(line.split()[1]) * 1024
    for cgroup in _ancestors(_own_cgroup()):
        limit = _read(cgroup / "memory.max")
        current = _read(cgroup / "memory.current")
        if limit and limit != "max" and current:
            available = min(available, int(limit) - int(current))
    return available


def _inotify_of_process(process: Path) -> int:
    held = 0
    with contextlib.suppress(OSError):
        for descriptor in (process / "fd").iterdir():
            with contextlib.suppress(OSError):
                held += os.readlink(descriptor) == INOTIFY
    return held


def _processes():
    return (process for process in PROC_ROOT.iterdir() if process.name.isdigit())


def inotify_in_use() -> int:
    return sum(_inotify_of_process(process) for process in _processes())


def inotify_of_processes_naming(words: list[str]) -> int:
    held = 0
    for process in _processes():
        with contextlib.suppress(OSError):
            arguments = (process / "cmdline").read_bytes()
            if any(word.encode() in arguments for word in words):
                held += _inotify_of_process(process)
    return held


def inotify_available() -> int:
    return inotify_limit() - inotify_in_use()


def worker_caps() -> dict[str, int]:
    known = estimates()
    if not known:
        return {}
    demands = known.values()
    seconds = sum(demand.seconds for demand in demands)
    longest = max(demand.seconds for demand in demands)
    cores = sum(demand.cpu_cores * demand.seconds for demand in demands) / seconds
    memory = sum(demand.peak_bytes * demand.seconds for demand in demands) / seconds
    caps = {"tests": len(known), "longest test": math.ceil(seconds / longest)}
    if cores:
        caps["cpu"] = math.floor(cpu_capacity() / cores)
    if memory:
        caps["memory"] = math.floor(memory_available() / memory)
    counted = [demand for demand in demands if demand.inotify_instances]
    if counted:
        weight = sum(demand.seconds for demand in counted)
        instances = sum(demand.inotify_instances * demand.seconds for demand in counted) / weight
        caps["inotify"] = math.floor(inotify_available() / instances)
    return caps


def worker_cap() -> int:
    caps = worker_caps()
    return max(1, min(caps.values(), default=1))


def inotify_limit() -> int:
    return int(INOTIFY_LIMIT.read_text())


def _load_ledger() -> dict:
    with contextlib.suppress(FileNotFoundError, ValueError):
        return json.loads(_ledger().read_text())
    return {"running": {}, "waiting": [], "memory": 0, "inotify": 0}


@contextlib.contextmanager
def _locked():
    settings.current().cache.mkdir(parents=True, exist_ok=True)
    with open(_ledger_lock(), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = _load_ledger()
        ledger.setdefault("memory", 0)
        ledger.setdefault("inotify", 0)
        ledger["running"] = {
            name: entry for name, entry in ledger["running"].items() if alive(entry["pid"])
        }
        ledger["waiting"] = [entry for entry in ledger["waiting"] if alive(entry["pid"])]
        yield ledger
        _ledger().write_text(json.dumps(ledger))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fits(ledger: dict, demand: Demand | None) -> bool:
    running = ledger["running"].values()
    if not running:
        return True
    if demand is None:
        return settings.calibrating()
    if demand.inotify_instances is None and not settings.calibrating():
        return False
    reserved = sum(entry["peak_bytes"] for entry in running)
    cores = sum(entry["cpu_cores"] for entry in running)
    instances = sum(entry.get("inotify_instances", 0) for entry in running)
    return (
        reserved + demand.peak_bytes <= ledger["memory"]
        and cores + demand.cpu_cores <= cpu_capacity()
        and instances + (demand.inotify_instances or 0) <= ledger["inotify"]
    )


def _waker(ticket: str) -> Path:
    return _wakers() / ticket


def _wake_waiters(ledger: dict) -> None:
    for entry in ledger["waiting"]:
        with contextlib.suppress(OSError):
            waker = os.open(_waker(entry["ticket"]), os.O_WRONLY | os.O_NONBLOCK)
            with contextlib.suppress(BlockingIOError):
                os.write(waker, b"\n")
            os.close(waker)


def _watch(ledger: dict) -> list[int]:
    """Pidfds of every other process in the ledger, readable once that process ends."""
    watched = []
    pids = {entry["pid"] for entry in [*ledger["running"].values(), *ledger["waiting"]]}
    for pid in pids - {os.getpid()}:
        with contextlib.suppress(ProcessLookupError):
            watched.append(os.pidfd_open(pid))
    return watched


def admit(ticket: str, test: str, demand: Demand | None) -> float:
    """Returns once `test` may start, in arrival order, with the seconds it waited.

    A test is admitted when the peaks of everything running plus its own fit the memory that was
    available when nothing of ours ran, their cores fit the CPUs, and their inotify instances fit
    what the kernel allowed this user when nothing of ours ran. Whether it fits changes
    only when a test starts, ends or dies, so a waiter sleeps until one of those happens: a
    release writes to every waiter's FIFO, and a death closes the pidfd it holds.
    """
    started = time.monotonic()
    _wakers().mkdir(parents=True, exist_ok=True)
    waker = _waker(ticket)
    os.mkfifo(waker)
    wakes = os.open(waker, os.O_RDWR | os.O_NONBLOCK)
    try:
        with _locked() as ledger:
            ledger["waiting"].append({"ticket": ticket, "pid": os.getpid()})
        while True:
            with _locked() as ledger:
                if not ledger["running"]:
                    ledger["memory"] = memory_available()
                    ledger["inotify"] = inotify_available()
                first = ledger["waiting"][0]["ticket"] == ticket
                if first and _fits(ledger, demand):
                    ledger["waiting"].pop(0)
                    ledger["running"][ticket] = {
                        "test": test,
                        "pid": os.getpid(),
                        "peak_bytes": demand.peak_bytes if demand else 0,
                        "cpu_cores": demand.cpu_cores if demand else 0.0,
                        "inotify_instances": (demand.inotify_instances or 0) if demand else 0,
                    }
                    _wake_waiters(ledger)
                    return time.monotonic() - started
                watched = _watch(ledger)
            try:
                select.select([wakes, *watched], [], [])
                with contextlib.suppress(BlockingIOError):
                    os.read(wakes, select.PIPE_BUF)
            finally:
                for pidfd in watched:
                    os.close(pidfd)
    except BaseException:
        release(ticket)
        raise
    finally:
        os.close(wakes)
        waker.unlink(missing_ok=True)


def release(ticket: str) -> None:
    with _locked() as ledger:
        ledger["running"].pop(ticket, None)
        ledger["waiting"] = [entry for entry in ledger["waiting"] if entry["ticket"] != ticket]
        _wake_waiters(ledger)


def _refused_forks(cgroup: Path) -> int:
    refused = 0
    for directory, _, files in os.walk(cgroup):
        if "pids.events" in files:
            for line in (_read(Path(directory) / "pids.events") or "").splitlines():
                if line.startswith("max "):
                    refused += int(line.split()[1])
    return refused


@dataclass(frozen=True)
class Measured:
    seconds: float
    waited: float
    variant: str
    inotify_instances: int | None


def record(test: str, cgroup: Path, measured: Measured) -> None:
    peak = _read(cgroup / "memory.peak")
    tasks = _read(cgroup / "pids.peak")
    refused = _refused_forks(cgroup)
    stat = _read(cgroup / "cpu.stat") or ""
    usage = next(
        (int(line.split()[1]) for line in stat.splitlines() if line.startswith("usage_usec")),
        None,
    )
    if peak is None or usage is None or measured.seconds <= 0:
        return
    run = {
        "test": test,
        "session": settings.session(),
        "peak_bytes": int(peak),
        "peak_tasks": int(tasks) if tasks else None,
        "refused_forks": refused,
        "cpu_seconds": usage / 1_000_000,
        "seconds": measured.seconds,
        "waited_seconds": measured.waited,
        "variant": measured.variant,
        "inotify_instances": measured.inotify_instances,
    }
    settings.current().cache.mkdir(parents=True, exist_ok=True)
    with open(_history(), "a") as history:
        fcntl.flock(history, fcntl.LOCK_EX)
        history.write(json.dumps(run) + "\n")


def trim_history() -> None:
    runs = _runs_by_test()
    lines = [json.dumps(run) for kept in runs.values() for run in kept]
    settings.current().cache.mkdir(parents=True, exist_ok=True)
    with open(_history(), "a+") as history:
        fcntl.flock(history, fcntl.LOCK_EX)
        history.seek(0)
        history.truncate()
        history.write("".join(line + "\n" for line in lines))


def session_runs(session: str) -> list[dict]:
    return [
        run for kept in _runs_by_test().values() for run in kept if run.get("session") == session
    ]


def pressure() -> dict[str, int]:
    totals = {}
    for resource in ("cpu", "memory", "io"):
        text = _read(PRESSURE_ROOT / resource) or ""
        for line in text.splitlines():
            kind, *fields = line.split()
            for field in fields:
                if field.startswith("total="):
                    totals[f"{resource} {kind}"] = int(field[len("total=") :])
    return totals


def write_baseline() -> None:
    known = {test: demand for test, demand in estimates().items() if test in _runs_by_test()}
    settings.current().baseline.write_text(
        json.dumps(
            {
                test: {
                    "peak_bytes": demand.peak_bytes,
                    "cpu_cores": round(demand.cpu_cores, 3),
                    "seconds": round(demand.seconds, 1),
                    "inotify_instances": demand.inotify_instances,
                }
                for test, demand in sorted(known.items())
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    project, root, baseline = sys.argv[1:4]
    settings.configure(settings.Settings(project, Path(root), Path(baseline), Path()))
    write_baseline()

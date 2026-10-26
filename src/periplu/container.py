# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import contextlib
import fcntl
import hashlib
import os
import pathlib
import re
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from periplu import resources, settings

SNAPSHOT_TEST = "snapshot::{key}"
READY = b"READY=1"
PROCESS_TREE = ("ps", "-eo", "pid,ppid,stat,wchan:24,etime,args", "--forest")


def _container_name() -> re.Pattern:
    return re.compile(rf"{re.escape(settings.current().project)}-test-(\d+)-\d+")


def _image_repository() -> str:
    return f"{settings.current().project}-test"


def _snapshot_repository() -> str:
    return f"{settings.current().project}-snapshot"


SNAPSHOT_TAG = re.compile(r"(\d+)-[0-9a-f]+-[\w.-]+")


def wait_any(*events: threading.Event) -> None:
    """Blocks until any of `events` is set."""
    any_set = threading.Event()
    for event in events:
        threading.Thread(target=lambda e=event: (e.wait(), any_set.set()), daemon=True).start()
    any_set.wait()


def until(wanted, what: str, unless=None):
    """Returns `wanted()` once it is truthy; fails as soon as `unless`, a background run or
    process, has ended instead."""
    ended = getattr(unless, "process", unless)
    while True:
        found = wanted()
        if found:
            return found
        if ended is not None and ended.ended.is_set():
            raise AssertionError(f"{what} never happened before {unless.wait()!r} ended")


class BackgroundProcess:
    """A program started in the container that the test waits for, without a deadline."""

    def __init__(self, container: Container, proc: subprocess.Popen, pidfile: str):
        self.container = container
        self.proc = proc
        self.pidfile = pidfile
        self.ended = threading.Event()
        self.stdout = ""
        self.stderr = ""
        threading.Thread(target=self._collect, daemon=True).start()

    def _collect(self) -> None:
        self.stdout, self.stderr = self.proc.communicate()
        self.ended.set()

    def pid(self) -> str:
        """The program's own pid in the container, which it wrote before it started."""
        while not self.ended.is_set():
            pid = self.container.exec("cat", self.pidfile).stdout.strip()
            if pid.isdigit():
                return pid
        raise AssertionError(
            f"{self.proc.args} exited ({self.proc.returncode}) before its pid was read:\n"
            f"{self.stdout}{self.stderr}"
        )

    def signal(self, sig_name: str) -> None:
        pid = self.pid()
        if self.container.exec("kill", f"-{sig_name}", pid).returncode != 0:
            self.ended.wait()
            raise AssertionError(
                f"{self.proc.args} ended before {sig_name} ({self.proc.returncode}):\n"
                f"{self.stdout}{self.stderr}"
            )

    def wait(self) -> subprocess.CompletedProcess:
        self.ended.wait()
        return subprocess.CompletedProcess(
            self.proc.args, self.proc.returncode, self.stdout, self.stderr
        )


@dataclass(frozen=True)
class Recorded:
    command: str
    returncode: int
    stdout: str
    stderr: str


class Container:
    def __init__(self, name: str):
        self.name = name
        self.recorded_runs: list[Recorded] = []
        self.background: list[str] = []

    def record(self, command: str, returncode: int, stdout: str, stderr: str) -> None:
        self.recorded_runs.append(Recorded(command, returncode, stdout, stderr))

    def probes(self) -> list[tuple[str, list[str]]]:
        return [("processes", list(PROCESS_TREE))]

    def snapshot(self) -> str:
        sections = []
        for title, probe in self.probes():
            result = self.exec(*probe)
            sections.append(f"=== {title}\n{result.stdout}{result.stderr}")
        return "\n".join(sections)

    def attach_failure(self, test: str, report) -> None:
        folder = settings.current().failures / re.sub(r"[^\w.-]+", "_", test)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        for index, run in enumerate(self.recorded_runs):
            name = f"run-{index}"
            contents = (
                f"$ {run.command}\nexit {run.returncode}\n"
                f"--- stdout\n{run.stdout}\n--- stderr\n{run.stderr}"
            )
            (folder / f"{name}.txt").write_text(contents)
            report.sections.append((name, contents))
        (folder / "machine.txt").write_text(self.snapshot())
        report.user_properties.append(("failure bundle", str(folder)))

    def exec(self, *args, env=None, check=False, user=None):
        cmd = ["podman", "exec"]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        if user:
            cmd += ["-u", user]
        cmd += [self.name, *args]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise AssertionError(f"{args} failed ({result.returncode}): {result.stderr}")
        return result

    def start_background(self, *args, env=None, user=None) -> BackgroundProcess:
        pidfile = f"/tmp/periplu-background-{len(self.background)}.pid"
        self.background.append(pidfile)
        cmd = ["podman", "exec"]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        if user:
            cmd += ["-u", user]
        cmd += [self.name, "sh", "-c", 'echo $$ > "$0" && exec "$@"', pidfile, *args]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return BackgroundProcess(self, proc, pidfile)

    def process_tree(self) -> str:
        return self.exec(*PROCESS_TREE).stdout

    def path_exists(self, path: str) -> bool:
        return self.exec("test", "-e", path).returncode == 0


def _booted(name: str, notifications: socket.socket) -> None:
    ended = subprocess.Popen(["podman", "wait", name], stdout=subprocess.PIPE, text=True)
    try:
        while True:
            readable, _, _ = select.select([notifications, ended.stdout], [], [])
            if notifications in readable and READY in notifications.recv(4096).split(b"\n"):
                return
            if ended.stdout in readable:
                break
    finally:
        ended.kill()
        ended.wait()
    logs = subprocess.run(["podman", "logs", name], capture_output=True, text=True, check=False)
    subprocess.run(["podman", "rm", "-f", name], capture_output=True, check=False)
    raise RuntimeError(
        f"container {name} ended before systemd finished booting\n"
        f"logs: {logs.stdout.strip()}{logs.stderr.strip()}"
    )


def start(image: str, name: str, run_args: list[str], systemd: bool) -> None:
    command = ["podman", "run", "-d", *run_args, "--name", name, image]
    if not systemd:
        subprocess.run(command, check=True, capture_output=True)
        return
    with tempfile.TemporaryDirectory(prefix="periplu-") as directory:
        address = os.path.join(directory, "notify")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifications:
            notifications.bind(address)
            subprocess.run(
                [*command[:3], "--systemd=always", "--sdnotify=container", *command[3:]],
                check=True,
                capture_output=True,
                env={**os.environ, "NOTIFY_SOCKET": address},
            )
            _booted(name, notifications)


def image(containerfile: pathlib.Path) -> str:
    digest = hashlib.sha256(containerfile.read_bytes()).hexdigest()
    local = f"{_image_repository()}:{digest}"
    cache = settings.current().cache
    cache.mkdir(parents=True, exist_ok=True)
    with open(cache / "container-image.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if subprocess.run(["podman", "image", "exists", local], check=False).returncode != 0:
            subprocess.run(
                [
                    "podman",
                    "build",
                    "-q",
                    "-t",
                    local,
                    "-f",
                    str(containerfile),
                    str(containerfile.parent),
                ],
                check=True,
            )
    return local


def reap_orphans() -> None:
    names = subprocess.run(
        ["podman", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    for name in names:
        match = _container_name().fullmatch(name)
        if match and not resources.alive(int(match.group(1))):
            subprocess.run(["podman", "rm", "-f", name], capture_output=True, check=False)
    tags = subprocess.run(
        [
            "podman",
            "images",
            "--filter",
            f"reference={_snapshot_repository()}",
            "--format",
            "{{.Tag}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    for tag in tags:
        match = SNAPSHOT_TAG.fullmatch(tag)
        if match and not resources.alive(int(match.group(1))):
            subprocess.run(
                ["podman", "rmi", "-f", f"{_snapshot_repository()}:{tag}"],
                capture_output=True,
                check=False,
            )


def _snapshot_prefix() -> str:
    controller = os.environ[settings.CONTROLLER_ENV]
    return f"{controller}-{settings.session()}"


def remove_snapshots() -> None:
    tags = subprocess.run(
        [
            "podman",
            "images",
            "--filter",
            f"reference={_snapshot_repository()}",
            "--format",
            "{{.Tag}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    for tag in tags:
        if tag.startswith(f"{_snapshot_prefix()}-"):
            subprocess.run(
                ["podman", "rmi", "-f", f"{_snapshot_repository()}:{tag}"],
                capture_output=True,
                check=False,
            )


def _cgroup_of(name: str) -> pathlib.Path:
    path = subprocess.run(
        ["podman", "inspect", "-f", "{{.State.CgroupPath}}", name],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return resources.CGROUP_ROOT / path.lstrip("/")


def new_name() -> str:
    return f"{settings.current().project}-test-{os.getpid()}-{time.time_ns()}"


def _known() -> dict[str, resources.Demand]:
    return resources.snapshotted(settings.session())


@contextlib.contextmanager
def admitted(
    test: str, image: str, run_args: list[str], systemd: bool, variant: str
) -> Iterator[str]:
    name = new_name()
    waited = resources.admit(name, test, resources.demand_for(test, _known()))
    started = time.monotonic()
    cgroup = None
    try:
        start(image, name, run_args, systemd)
        cgroup = _cgroup_of(name)
        yield name
    finally:
        if cgroup is not None:
            resources.record(test, cgroup, time.monotonic() - started, waited, variant)
        subprocess.run(["podman", "rm", "-f", name], capture_output=True, check=False)
        resources.release(name)


def snapshot(
    key: str, image: str, run_args: list[str], systemd: bool, build: Callable[[str], None]
) -> str:
    tag = f"{_snapshot_repository()}:{_snapshot_prefix()}-{key}"
    cache = settings.current().cache
    cache.mkdir(parents=True, exist_ok=True)
    with open(cache / f"snapshot-{key}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if subprocess.run(["podman", "image", "exists", tag], check=False).returncode == 0:
            return tag
        with admitted(SNAPSHOT_TEST.format(key=key), image, run_args, systemd, "build") as name:
            build(name)
            subprocess.run(["podman", "stop", name], check=True, capture_output=True)
            subprocess.run(
                ["podman", "commit", "--quiet", name, tag], check=True, capture_output=True
            )
    return tag

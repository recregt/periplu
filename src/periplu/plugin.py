import math
import os
import uuid

import pytest

from periplu import container, resources, settings

PRESSURE_AT_START = pytest.StashKey[dict]()
OPTIONS = {
    "periplu_project": "names the cache, containers and images of this suite",
    "periplu_baseline": "the committed resource estimates, relative to the rootdir",
    "periplu_failures": "where failure bundles are written, relative to the rootdir",
}


def pytest_addoption(parser):
    for name, help in OPTIONS.items():
        parser.addini(name, help)


def _is_controller(config) -> bool:
    return not hasattr(config, "workerinput")


def _required(config, name: str) -> str:
    value = config.getini(name)
    if not value:
        raise pytest.UsageError(f"periplu needs `{name}` in the pytest configuration")
    return value


def pytest_configure(config):
    root = config.rootpath
    settings.configure(
        settings.Settings(
            project=_required(config, "periplu_project"),
            root=root,
            baseline=(root / _required(config, "periplu_baseline")).resolve(),
            failures=(root / _required(config, "periplu_failures")).resolve(),
        )
    )
    if _is_controller(config):
        container.reap_orphans()
        os.environ.setdefault(settings.CONTROLLER_ENV, str(os.getpid()))
        session = os.environ.setdefault(settings.SESSION_ENV, uuid.uuid4().hex)
        resources.snapshot(session)
        config.stash[PRESSURE_AT_START] = resources.pressure()


def pytest_unconfigure(config):
    if _is_controller(config) and settings.CONTROLLER_ENV in os.environ:
        container.remove_snapshots()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    for value in getattr(item, "funcargs", {}).values():
        if isinstance(value, container.Container):
            try:
                value.attach_failure(item.nodeid, report)
            except OSError as error:
                report.sections.append(("failure bundle", f"could not be gathered: {error!r}"))


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    return resources.worker_cap()


def pytest_collection_modifyitems(items):
    known = resources.snapshotted(settings.session())
    items.sort(key=lambda item: -known[item.nodeid].seconds if item.nodeid in known else -math.inf)


def pytest_terminal_summary(terminalreporter, config):
    if not _is_controller(config):
        return
    bundles = sorted(
        {
            (report.nodeid, value)
            for reports in terminalreporter.stats.values()
            for report in reports
            for name, value in getattr(report, "user_properties", ())
            if name == "failure bundle"
        }
    )
    if bundles:
        terminalreporter.write_sep("-", "failure bundles")
        for nodeid, folder in bundles:
            terminalreporter.write_line(f"{nodeid}: {folder}")
    runs = resources.session_runs(settings.session())
    resources.trim_history()
    if not runs:
        return
    start = config.stash.get(PRESSURE_AT_START, {})
    stalls = ", ".join(
        f"{name} {(total - start.get(name, 0)) / 1_000_000:.1f}s"
        for name, total in resources.pressure().items()
    )
    gib = 1024**3
    terminalreporter.write_sep("-", "resources")
    terminalreporter.write_line(
        f"{len(runs)} containers measured; peak memory per test up to "
        f"{max(run['peak_bytes'] for run in runs) / gib:.2f} GiB; "
        f"CPU per test up to {max(run['cpu_seconds'] / run['seconds'] for run in runs):.2f} cores; "
        f"admission waits {sum(run['waited_seconds'] for run in runs):.0f}s in total; "
        f"tasks per test up to {max(run.get('peak_tasks') or 0 for run in runs)}; "
        f"forks refused by a task limit {sum(run.get('refused_forks') or 0 for run in runs)}"
    )
    terminalreporter.write_line(
        f"capacity: {resources.cpu_capacity():g} cores, "
        f"{resources.memory_available() / gib:.1f} GiB available now"
    )
    terminalreporter.write_line(f"pressure stalls during the run: {stalls}")

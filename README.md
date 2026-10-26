<!--
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->

# periplu

A pytest plugin for end-to-end tests in Podman containers. Waits end on events, not on clocks, and parallel tests are admitted by what they measurably use.

- A systemd container counts as booted when systemd itself sends `READY=1`.
- A test waits for a process, a FIFO or a signal, and fails as soon as the thing it waits on has ended. There are no deadlines.
- Tests run in parallel only while their measured memory, CPU and inotify instances fit the machine. A waiting test sleeps until another test starts, ends or dies.
- The longest tests start first.
- A failed test leaves a bundle with every command it recorded and a description of the machine.

## Requirements

- Linux with cgroup v2 and kernel 5.19 or newer, for `memory.peak`.
- Rootless Podman.
- Python 3.14 or newer.

## Install

```toml
[project]
dependencies = ["periplu @ git+https://github.com/recregt/periplu@v0.1.0"]

[tool.pytest.ini_options]
periplu_project = "myproject"
periplu_baseline = "resources.json"
periplu_failures = "failures"
```

All three settings are required.

- `periplu_project` names the cache (`~/.cache/periplu/<project>/`), the containers (`<project>-test-…`) and the images (`<project>-test:…`, `<project>-snapshot:…`).
- `periplu_baseline` is the committed file of resource estimates. It may be missing at first.
- `periplu_failures` is where failure bundles are written.

Both paths are relative to the pytest rootdir.

## A first suite

[`examples/systemd`](examples/systemd) is a complete suite. Run it with:

```sh
cd examples/systemd
uv run pytest -n auto
```

Its fixture is the whole integration:

```python
@pytest.fixture(scope="session")
def image():
    return periplu.image(Path(__file__).parent / "Containerfile")


@pytest.fixture
def machine(request, image):
    with periplu.admitted(
        request.node.nodeid, image, run_args=[], systemd=True, variant="fresh"
    ) as name:
        yield periplu.Container(name)
```

## Containers

From `periplu.container`:

- `image(containerfile)` builds the file once per content and returns the image name.
- `admitted(test, image, *, run_args, systemd, variant)` waits until the machine has room for `test`, starts the container, measures it while the test runs, and removes it afterwards. With `systemd=True` it returns once systemd has booted. `variant` keeps separate estimates for the same test started different ways, for example from a snapshot.
- `snapshot(key, image, *, run_args, systemd, build)` runs `build(name)` in a fresh container and commits the result. It is built once per test session and removed when the session ends.
- `Container(name)` has `exec(*args, env=, user=, check=)`, `start_background(*args)`, `path_exists(path)`, `process_tree()`, and `record(command, returncode, stdout, stderr)` for commands that should appear in a failure bundle. Override `probes()` to add what a failure bundle describes.

## Waiting

- `start_background(...)` returns a `BackgroundProcess`. Its `ended` event is set when the process exits, `wait()` returns the result, `pid()` and `signal(name)` reach the process inside the container.
- `wait_any(*events)` blocks until any of the events is set. Pair what a test waits for with the process it depends on, so the test fails at once instead of hanging. The example test does this with a FIFO.
- `until(wanted, what, unless=)` repeats `wanted()` until it is true and fails when `unless` has ended. It polls, so use it only for a state that has no event to wait on.

## Resources

For each pool, a test starts only if what is in use outside the suite, plus the demand of every running test, plus its own demand, fits the capacity:

| Pool | Capacity | Demand of a test |
|---|---|---|
| Memory | `MemAvailable` when nothing of the suite runs, within the cgroup's `memory.max` | `memory.peak` of its container, a peak the kernel records |
| CPU | CPUs in the affinity mask, within `cpu.max` | average cores over its run |
| inotify instances | `fs.inotify.max_user_instances` minus the instances in use when nothing of the suite runs | instances held by the container's processes, its conmon and exec sessions and its pasta process, counted after boot and at the end |

Rootless containers charge inotify instances to the user who runs Podman, so all containers share one pool. At the kernel default of 128 that is about 16 systemd containers, whatever the number of CPUs. The end of every run shows how many workers each pool allows, so you can see which one holds a run back.

When inotify is the limit, raise it. The value you need is the instances in use outside the suite plus the workers you want times the largest per-test count, both printed in the run summary. For 24 workers at 12 instances each with 10 in use elsewhere, that is 10 + 24 × 12 = 298:

```sh
sudo sysctl fs.inotify.max_user_instances=298
```

This lasts until the next reboot. To keep it, put the line `fs.inotify.max_user_instances = 298` in a file under `/etc/sysctl.d/` and run `sudo sysctl --system`. On NixOS, set `boot.kernel.sysctl."fs.inotify.max_user_instances" = 298;`.

Memory is the only pool with a kernel-recorded peak. CPU only slows tests down when oversubscribed. The inotify count is sampled twice, so an instance a test holds only briefly between the samples is not seen.

A test without an estimate of its own is assumed to need as much as the largest known test. When nothing is known yet, it runs only when nothing else does. To measure a new suite in parallel, set `PERIPLU_CALIBRATE=1` and choose a worker count that fits by hand. Then write the baseline:

```sh
python -m periplu.resources <project> <rootdir> <baseline>
```

`pytest -n auto` picks the number of workers from the estimates: the smallest number any pool allows. A run ends with a line such as `workers allowed by: tests 83, longest test 12, cpu 40, memory 7, inotify 28`, where memory sets the number.

## Failure bundles

When a test that uses a `Container` fails, periplu writes `run-N.txt` for every recorded command and `machine.txt` with the output of `probes()` to `<periplu_failures>/<test id>/`. The paths are listed at the end of the run.

## License

[Mozilla Public License 2.0](LICENSE)

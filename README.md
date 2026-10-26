# periplu

A pytest plugin for end-to-end tests in Podman containers, where every wait ends on an event and never on a clock.

- A systemd container is ready when systemd says so (`READY=1`), not after a delay.
- A test waits for its process, its gate or its signal, and fails as soon as what it waits on has ended.
- Tests are admitted by measured memory and CPU, so a parallel run never oversubscribes the machine. Waiting tests sleep until another test starts, ends or dies.
- The longest tests start first.
- A failed test leaves a bundle with every command it ran and what the machine looked like.

## Use

```toml
[project]
dependencies = ["periplu @ git+https://github.com/recregt/periplu@v0.1.0"]

[tool.pytest.ini_options]
periplu_project = "myproject"
periplu_baseline = "resources.json"
periplu_failures = "../target/e2e-failures"
```

`periplu_project` names the cache, containers and images. `periplu_baseline` holds the committed resource estimates, and `periplu_failures` is where failure bundles go. Both paths are relative to the pytest rootdir.

In a fixture:

```python
from periplu import container as periplu


@pytest.fixture
def machine(request):
    image = periplu.image(Path(__file__).parent / "Containerfile")
    with periplu.admitted(request.node.nodeid, image, [], True, "fresh") as name:
        yield periplu.Container(name)
```

Run with `pytest -n auto`. To refresh the baseline after a run: `python -m periplu.resources <project> <rootdir> <baseline>`.

## License

[Mozilla Public License 2.0](LICENSE)

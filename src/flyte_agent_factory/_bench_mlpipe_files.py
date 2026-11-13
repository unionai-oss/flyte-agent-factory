"""The hidden check of the ml-pipeline task: its `test_hidden_mlpipe.py` source and `holdout.csv`."""

HOLDOUT = """\
tenure_months,monthly_charge,support_calls,churn
68.77,28.59,4,0
8.75,23.58,0,0
21.18,106.53,5,1
64.8,78.59,5,0
36.91,59.69,1,0
11.49,66.83,6,1
11.49,43.72,0,0
7.3,50.78,4,1
65.44,43.58,4,0
2.1,86.38,3,1
18.09,74.28,2,1
27.32,110.73,4,1
69.89,117.69,3,0
67.58,95.52,1,0
67.22,32.14,6,1
69.0,51.87,1,0
70.81,87.24,0,0
42.82,37.52,5,1
1.45,29.56,6,1
16.81,88.78,5,1
69.29,114.44,6,1
10.15,90.8,5,1
22.15,93.09,4,1
43.56,81.02,6,1
36.15,20.31,1,0
39.94,48.32,0,0
31.44,29.93,1,0
51.94,90.81,1,0
58.96,78.57,1,0
49.79,60.54,5,1
28.42,85.57,2,1
21.22,95.82,0,0
28.47,56.59,5,1
51.66,64.99,5,1
28.73,94.23,3,1
63.24,95.69,5,1
64.38,62.19,2,0
58.46,69.61,5,0
58.73,102.27,5,1
65.13,96.41,1,0
71.43,74.87,1,0
19.45,77.19,1,1
12.89,33.88,6,1
62.83,81.33,4,0
53.95,111.32,3,0
1.95,57.87,1,1
3.68,42.48,6,1
67.85,26.05,4,0
43.62,34.21,1,0
9.86,55.65,4,1
49.35,52.76,1,0
7.34,66.9,2,1
35.45,24.72,1,0
63.07,85.38,4,0
57.67,106.13,2,0
23.44,42.39,5,1
70.37,28.04,4,0
25.74,61.24,5,1
3.5,87.76,2,1
44.38,109.29,3,1
6.53,111.29,5,1
30.97,56.25,4,1
13.33,68.21,2,1
21.66,34.82,2,1
43.68,52.27,1,0
35.62,78.57,3,1
64.78,39.44,1,0
24.62,29.83,1,0
17.32,114.99,4,1
54.29,85.09,1,0
62.41,50.35,5,0
22.56,79.3,1,0
11.94,78.25,0,0
30.85,112.23,2,1
45.05,43.33,6,1
23.5,34.34,1,0
46.03,65.51,1,0
18.12,26.39,6,1
24.23,44.53,5,1
11.24,27.69,1,0
61.79,79.68,5,0
27.42,77.38,1,0
68.94,24.67,6,0
3.49,63.15,6,1
11.52,32.66,1,0
7.64,83.18,5,1
64.32,102.05,6,1
44.19,29.03,1,0
56.43,57.08,5,0
38.96,29.77,5,1
13.22,36.91,3,1
22.04,60.84,0,0
11.47,116.05,5,1
57.43,96.33,1,0
64.76,47.45,4,0
8.07,83.42,4,1
47.19,102.24,6,1
18.95,29.43,2,0
39.98,57.21,0,0
23.51,35.56,3,1
67.22,58.49,4,0
19.29,108.06,0,1
69.28,28.62,1,0
5.82,72.01,4,1
9.31,110.61,5,1
52.85,25.49,4,0
52.44,110.12,2,0
2.74,31.51,5,1
48.37,50.67,0,0
29.06,23.69,5,1
27.18,93.63,5,1
16.37,59.63,4,1
34.19,58.11,2,0
42.18,57.05,6,1
49.8,59.52,6,1
39.22,57.4,1,0
28.12,43.91,4,1
3.17,60.33,2,1
34.79,68.86,4,1
30.52,74.08,3,1
41.94,49.23,1,0
46.26,51.41,3,0
52.74,84.28,3,1
55.45,102.17,4,1
13.31,64.69,6,1
28.35,42.87,5,1
69.19,84.6,6,1
44.94,49.12,0,0
22.65,51.69,1,0
17.78,101.19,6,1
67.09,97.08,3,0
39.58,95.48,3,1
71.78,30.88,2,0
37.4,87.55,6,1
3.51,26.9,3,1
20.23,23.21,6,1
57.0,33.8,0,0
21.21,60.49,0,0
55.51,70.07,1,0
62.36,23.53,2,0
15.44,29.85,1,0
31.1,54.64,1,0
7.98,80.02,3,1
53.32,83.8,5,1
43.74,62.39,0,0
30.99,65.39,3,1
19.1,109.85,3,1
15.22,95.38,4,1
16.55,51.54,6,1
64.89,39.04,0,0
54.54,36.2,1,0
67.96,39.99,1,0
18.11,20.05,6,1
33.54,41.55,4,1
47.31,86.12,2,0
60.21,26.09,5,0
20.58,80.89,5,1
1.52,30.7,4,1
52.86,105.72,6,1
49.15,85.58,4,1
11.1,57.62,6,1
25.03,64.65,6,1
56.56,70.58,6,1
16.02,30.61,0,0
45.62,68.44,6,1
33.07,108.52,6,1
52.19,39.76,5,1
57.52,37.05,0,0
33.86,46.19,1,0
19.56,35.91,0,0
33.02,45.12,0,0
29.38,83.8,0,0
14.32,107.41,6,1
69.63,73.31,4,0
2.09,83.65,0,1
63.66,42.48,3,0
27.8,105.2,4,1
35.6,52.07,1,0
26.71,79.37,6,1
34.03,51.18,6,1
65.22,74.03,1,0
44.09,46.48,5,1
30.73,75.1,3,1
58.61,67.77,1,0
42.24,92.88,3,1
28.62,62.35,3,1
32.92,85.39,3,1
66.88,87.25,6,1
58.63,83.37,4,0
70.98,52.31,0,0
50.35,57.17,6,1
35.06,60.57,6,1
68.9,107.4,2,0
19.89,25.35,2,0
34.46,58.36,4,1
60.45,57.98,2,0
31.32,117.75,1,1
44.87,49.85,4,1
51.42,97.87,3,1
52.29,64.65,0,0
15.34,96.23,6,1
36.15,45.87,5,1
66.83,111.21,6,1
15.69,38.41,0,0
27.0,74.37,0,0
3.02,87.64,6,1
68.51,23.52,1,0
61.48,32.3,0,0
3.86,37.13,3,1
44.98,76.52,4,1
7.39,28.04,2,1
32.53,70.23,0,0
59.83,62.85,4,0
48.41,103.72,5,1
62.79,114.43,1,0
57.53,48.23,0,0
41.54,90.28,4,1
58.54,89.45,5,1
38.89,110.73,4,1
14.68,87.79,2,1
30.42,110.12,1,1
12.17,85.72,3,1
37.21,30.92,4,0
35.19,23.08,2,0
4.46,54.43,4,1
38.22,105.49,4,1
44.63,57.79,6,1
1.56,52.33,4,1
59.97,54.23,2,0
14.87,58.79,3,1
67.31,44.53,6,0
48.1,44.68,0,0
28.16,88.89,2,1
4.42,93.81,5,1
17.43,63.68,0,0
59.35,104.24,0,0
61.37,45.04,2,0
13.1,54.86,1,0
4.29,40.56,5,1
40.57,58.58,3,0
67.6,33.38,5,0
12.53,57.82,2,1
43.5,49.57,4,0
2.23,116.29,3,1
52.15,61.64,2,0
59.48,110.48,5,1
42.37,64.4,0,0
67.76,44.11,0,0
50.84,111.7,5,1
12.94,59.8,3,1
56.81,111.87,1,0
60.05,98.36,6,1
10.51,83.22,0,1
19.35,22.73,6,1
35.38,116.17,1,0
65.64,62.58,1,0
11.05,114.35,1,1
61.31,90.26,2,0
4.44,80.6,3,1
63.08,36.81,5,0
39.3,98.84,1,0
3.09,77.8,3,1
38.12,39.86,1,0
59.24,56.76,6,1
55.25,20.98,5,0
10.3,23.26,3,1
10.36,86.61,4,1
40.6,104.22,6,1
6.24,89.08,0,1
70.33,80.87,3,0
63.09,38.18,1,0
36.37,89.17,4,1
10.09,90.37,1,1
46.26,32.35,2,0
6.96,110.71,3,1
7.0,58.9,4,1
56.04,112.56,5,1
24.12,62.01,4,1
20.38,22.59,5,1
6.68,95.2,6,1
43.02,79.51,3,1
33.63,51.21,0,0
29.98,42.49,0,0
12.94,58.64,3,1
65.69,69.88,0,0
58.02,65.36,2,0
16.06,56.28,1,1
7.97,106.49,6,1
66.61,25.43,3,0
45.73,56.4,5,1
7.85,81.7,6,1
43.82,111.16,6,1
65.46,105.91,2,0
33.96,114.69,5,1
51.18,26.0,1,0
25.9,75.23,4,1
67.7,89.61,4,0
57.03,32.76,1,0
49.89,91.05,6,1
37.76,73.15,2,0
19.19,32.89,0,0
34.41,60.5,4,1
64.42,106.72,4,1
32.76,85.2,0,0
31.36,64.13,6,1
38.64,80.22,4,1
42.72,53.04,1,0
15.28,25.9,6,1
66.89,106.19,3,0
8.84,79.89,2,1
3.56,115.79,4,1
20.03,46.54,3,1
63.35,79.52,6,1
62.79,86.52,5,1
56.27,52.76,5,1
66.19,68.88,3,0
38.46,116.92,4,1
64.22,44.35,1,0
50.97,63.38,1,0
12.28,98.8,5,1
29.7,20.26,2,0
58.14,84.46,2,0
50.28,113.04,0,0
69.96,90.68,4,0
19.42,41.62,2,0
69.11,73.48,2,0
30.16,38.24,2,0
10.08,74.75,1,1
15.52,56.36,5,1
37.72,118.52,1,0
3.13,91.63,6,1
66.42,83.84,1,0
13.0,31.55,4,1
42.85,98.45,6,1
49.7,60.39,6,1
61.15,92.53,4,1
37.62,94.31,3,1
27.18,57.22,1,0
13.05,56.75,2,1
71.45,58.2,0,0
8.18,99.42,4,1
63.88,47.44,5,0
45.77,24.72,0,0
63.79,52.42,4,0
67.07,25.72,2,0
20.05,76.91,0,0
2.49,107.45,0,1
14.21,89.35,0,1
34.86,40.81,1,0
30.58,69.79,3,1
52.85,59.38,2,0
64.19,34.07,4,0
49.77,35.28,6,1
58.17,91.4,2,0
59.99,30.14,6,0
35.56,65.82,5,1
13.12,84.32,6,1
23.52,100.96,1,1
48.95,23.28,1,0
6.22,105.89,0,1
63.97,64.59,6,1
33.98,60.52,5,1
5.95,109.72,4,1
12.47,51.18,0,0
15.02,30.05,4,1
10.14,22.65,4,1
59.52,48.48,3,0
60.12,92.19,6,1
40.64,60.49,2,0
41.73,55.48,1,0
18.26,54.36,3,1
70.16,50.67,2,0
1.24,51.96,4,1
20.81,44.67,0,0
56.46,107.76,0,0
25.73,112.5,1,1
43.61,52.77,0,0
46.65,92.63,4,1
32.41,66.27,1,0
22.7,51.15,3,1
52.96,95.11,4,1
59.43,24.62,4,0
13.63,36.7,2,0
68.47,91.88,4,0
6.96,99.72,0,1
22.96,104.92,3,1
25.18,42.72,2,0
44.63,21.02,0,0
65.92,116.92,1,0
6.94,71.02,4,1
42.39,111.07,1,0
29.35,49.79,5,1
51.76,79.24,5,1
71.13,71.86,5,0
17.47,72.93,1,1
42.55,77.35,3,1
12.46,92.6,3,1
49.36,98.26,1,0
2.17,75.17,5,1
36.95,31.56,5,1
38.24,110.5,6,1
34.03,117.78,1,1
18.48,77.87,1,0
57.04,79.74,6,1
11.21,91.89,3,1
53.33,43.9,3,0
52.78,67.15,0,0
59.44,109.41,2,0
23.52,44.23,4,1
63.95,32.76,5,0
50.9,77.46,4,1
71.3,84.55,2,0
19.9,24.64,1,0
65.01,24.16,6,0
47.59,24.17,4,0
29.32,106.52,4,1
70.77,46.64,6,0
23.11,104.19,5,1
37.11,91.14,0,0
27.09,40.54,0,0
52.39,93.71,6,1
19.25,72.64,0,0
66.29,51.05,3,0
71.78,90.64,2,0
71.49,107.69,1,0
6.43,79.79,3,1
28.0,90.69,6,1
51.56,89.98,3,0
65.81,28.56,2,0
9.24,78.72,4,1
62.02,68.81,5,1
9.59,65.92,3,1
17.19,57.85,0,0
66.19,32.0,4,0
71.98,106.25,3,0
67.8,27.26,2,0
26.57,96.81,3,1
36.95,75.42,2,0
3.74,87.89,4,1
7.44,103.13,5,1
36.04,52.61,5,1
32.0,82.13,3,1
6.52,110.96,2,1
5.42,90.38,2,1
56.22,50.54,6,1
56.24,23.51,6,0
58.96,103.47,1,0
20.8,103.83,2,1
43.81,109.66,3,1
21.77,81.66,5,1
31.69,67.6,3,1
17.23,30.3,3,1
58.59,62.27,2,0
1.18,77.65,2,1
33.62,105.8,1,1
4.49,85.15,6,1
58.32,59.25,3,0
1.28,66.28,1,1
6.32,33.2,6,1
70.29,93.67,5,1
12.72,106.87,3,1
63.75,21.72,2,0
64.87,26.22,4,0
5.98,100.42,4,1
51.05,80.6,0,0
13.12,82.7,2,1
8.41,111.85,0,1
7.87,83.25,4,1
23.51,118.36,4,1
11.66,62.04,5,1
44.95,89.94,5,1
61.51,100.79,5,1
9.74,85.38,0,1
12.9,72.91,3,1
17.35,28.48,6,1
44.45,22.14,1,0
53.2,105.47,5,1
23.87,97.0,0,0
46.52,58.11,4,0
53.76,54.14,3,0
47.68,82.41,5,1
63.63,73.51,4,1
23.54,28.67,4,1
40.95,98.77,3,1
53.68,114.23,4,1
23.89,113.94,0,1
27.98,20.81,6,1
1.73,105.03,4,1
2.45,79.36,3,1
68.96,54.44,5,0
1.01,114.25,3,1
6.05,37.49,2,1
66.79,52.84,4,0
27.66,58.02,6,1
44.64,35.56,2,0
5.1,76.97,3,1
11.34,73.36,6,1
5.48,41.78,0,0
8.19,64.3,5,1
15.7,25.62,4,1
15.77,103.53,4,1
60.96,112.97,6,1
42.48,57.04,0,0
63.02,104.17,4,1
6.91,84.31,6,1
21.39,53.86,4,1
55.35,58.29,3,0
23.51,113.28,6,1
23.5,60.36,3,1
41.02,49.06,1,0
12.81,88.54,5,1
21.83,53.86,2,0
"""

HIDDEN = '''\
"""Hidden acceptance for ml-pipeline, run without cluster credentials."""

import contextlib
import csv
import functools
import hashlib
import importlib
import io
import json
import os
import pkgutil
import re
import runpy
import subprocess
import sys
import time
import types
from pathlib import Path

import flyte
from flyte._cache.cache import cache_from_request
from flyte._task import TaskTemplate

PROJECT = Path(os.environ["BENCH_PROJECT"])
HIDDEN = Path(os.environ["BENCH_HIDDEN"])
ORIGINAL_HASHES = {
    "mlpipe/train.csv": "e5877512ed689d0c3618b107722ebe339a08351f4da226ac4e8807d1c9fe28ac",
    "tests/test_data.py": "c4ddf6763aac3b14558c614aab10687c23f5df29cb522edc87aef9cf5c9f5fd9",
}
# One budget for every `python -m mlpipe` run together, measured from when this module is collected.
BUDGET_S = 60
STARTED = time.monotonic()
# Loaded by `python -m mlpipe` through PYTHONPATH. flyte's `forward` is what a direct task call runs and
# `execute` is what a local run uses; both are patched on the one class every `env.task` function becomes.
# The record is a file because the pipeline runs in a child process.
SITECUSTOMIZE = """\\
import json
import os


def _record(kind, name):
    with open(os.environ["BENCH_TRACE"], "a") as stream:
        stream.write(json.dumps({"kind": kind, "name": name}) + chr(10))


def _wrap(owner, attribute, kind, label):
    original = getattr(owner, attribute)

    def wrapper(self, *args, **kwargs):
        _record(kind, label(self))
        return original(self, *args, **kwargs)

    setattr(owner, attribute, wrapper)


try:
    from flyte._task import AsyncFunctionTaskTemplate
    from sklearn.linear_model import LogisticRegression
except ImportError:
    pass
else:
    for _attribute in ("forward", "execute"):
        _wrap(AsyncFunctionTaskTemplate, _attribute, "task", lambda task: task.func.__name__)
    _wrap(LogisticRegression, "fit", "fit", lambda model: "LogisticRegression")
"""


def run_cli(*flags, trace=None):
    """Run `python -m mlpipe` inside what is left of the budget; `trace` is where to record what ran."""
    left = BUDGET_S - (time.monotonic() - STARTED)
    assert left > 0, f"the {BUDGET_S} s budget for the whole check is spent"
    env = dict(os.environ)
    if trace:
        shim = trace.parent / "shim"
        shim.mkdir(exist_ok=True)
        (shim / "sitecustomize.py").write_text(SITECUSTOMIZE)
        env.update(BENCH_TRACE=str(trace), PYTHONPATH=os.pathsep.join([str(shim), str(PROJECT)]))
    return subprocess.run(
        [sys.executable, "-m", "mlpipe", *flags], cwd=PROJECT, env=env,
        capture_output=True, text=True, timeout=left,
    )


def printed_accuracy(done):
    assert done.returncode == 0, done.stderr[-1500:]
    score = re.search(r"^accuracy=([0-9.]+)$", done.stdout, re.M)
    assert score, f"expected accuracy=NUMBER, got {done.stdout[-500:]!r}"
    return float(score[1])


@functools.cache
def discover():
    """`(has_env, {"load"|"train"|"eval": task object or None})` across the package and its modules."""
    package = importlib.import_module("mlpipe")
    modules = [package]
    mains = []
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        if info.name.rsplit(".", 1)[-1] == "__main__":
            mains.append(info.name)
        else:
            modules.append(importlib.import_module(info.name))

    def found():
        tasks = {}
        for name in ("load", "train", "eval"):
            candidates = [getattr(module, name, None) for module in modules]
            tasks[name] = next((task for task in candidates if isinstance(task, TaskTemplate)), None)
        return tasks

    if mains and not all(found().values()):
        # Run `__main__` under another name so its `if __name__ == "__main__":` guard does not fire.
        # Only done when needed: an unguarded `main()` there would train in this process.
        argv, sys.argv = sys.argv, ["mlpipe"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                namespace = runpy.run_module(mains[0], run_name="mlpipe.discovery")
            modules.append(types.SimpleNamespace(**namespace))
        except (Exception, SystemExit):
            pass
        finally:
            sys.argv = argv
    has_env = any(isinstance(getattr(module, "env", None), flyte.TaskEnvironment) for module in modules)
    return has_env, found()


def test_training_data_and_existing_tests_are_unchanged():
    for name, expected in ORIGINAL_HASHES.items():
        path = PROJECT / name
        assert path.is_file(), f"missing original file: {name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, f"changed original file: {name}"


def test_train_and_eval_are_cached_flyte_tasks():
    has_env, tasks = discover()
    assert has_env
    for name, task in tasks.items():
        assert task is not None and cache_from_request(task.cache).behavior == "auto", (
            f"missing real cached Flyte task: {name}"
        )


def test_cli_scores_the_hidden_holdout(tmp_path):
    assert 0.85 <= printed_accuracy(run_cli()) <= 1.0
    trace = tmp_path / "trace.jsonl"
    holdout_path = str(HIDDEN / "holdout.csv")
    printed = printed_accuracy(run_cli("--eval-csv", holdout_path, trace=trace))
    assert 0.85 <= printed <= 1.0, printed
    events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    ran = {event["name"] for event in events if event["kind"] == "task"}
    _, tasks = discover()
    for name in ("train", "eval"):
        assert tasks[name].func.__name__ in ran, f"the {name} task never ran; tasks that ran: {sorted(ran)}"
    assert any(event["kind"] == "fit" for event in events), "LogisticRegression.fit was never called"

    with (HIDDEN / "holdout.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    features = [name for name in rows[0] if name != "churn"]
    # Reversing row order exposes CLIs that silently reuse their default evaluation set.
    for index, holdout in enumerate((rows, rows[::-1])):
        evaluation = tmp_path / f"evaluation-{index}.csv"
        predictions = tmp_path / f"predictions-{index}.txt"
        with evaluation.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=features)
            writer.writeheader()
            writer.writerows({name: row[name] for name in features} for row in holdout)
        done = run_cli("--eval-csv", str(evaluation), "--predictions", str(predictions))
        assert done.returncode == 0, done.stderr[-1500:]
        assert predictions.is_file(), "CLI did not write predictions"
        labels = predictions.read_text().splitlines()
        assert len(labels) == len(holdout), "expected one predicted label per evaluation row"
        assert set(labels) <= {"0", "1"}, "predictions must be binary labels, without a header"
        accuracy = sum(label == row["churn"] for label, row in zip(labels, holdout)) / len(holdout)
        assert accuracy >= 0.85, f"independently measured accuracy={accuracy:.6f}"
        if index == 0:
            assert abs(printed - accuracy) <= 0.02, f"printed accuracy={printed}, predictions earn {accuracy}"
'''

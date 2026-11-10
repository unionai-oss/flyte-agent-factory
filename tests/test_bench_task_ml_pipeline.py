"""The ml-pipeline start, reference, hidden acceptance and shared check image."""

import ast
import csv
import shutil
import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import _bench_check, _bench_suite, _gates, _timeline
from ops import bench_seed

BENCH = Path(__file__).resolve().parent / "fixtures" / "bench"
START, REF = BENCH / "mlpipe_start", BENCH / "mlpipe_ref"


def committed_tree(task_id: str) -> str:
    """The git tree of the task's committed start: what `ops/bench_seed.py` publishes, and the pin."""
    from pathlib import Path

    from ops import bench_seed

    return bench_seed.tree_id(Path(__file__).parent / "fixtures" / "bench" / bench_seed.STARTS[task_id])


@pytest.fixture
def ml_dependencies():
    pytest.importorskip("sklearn", reason="ml-pipeline runtime checks require scikit-learn installed offline")
    pytest.importorskip("pandas", reason="ml-pipeline runtime checks require pandas installed offline")


def copy_case(tmp_path, source=REF):
    project = tmp_path / "case"
    shutil.copytree(source, project, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return project


def check(project):
    return _bench_check.run_hidden_check(_bench_suite.get("ml-pipeline"), project, timeout=60, key_vars=())


def test_the_task_is_registered_and_pinned():
    task = _bench_suite.get("ml-pipeline")
    assert (task.kind, task.cap_usd, task.cap_hours, task.estimate_usd) == ("build", 40.0, 3.0, 15.0)
    assert task.workdir == "bench/mlpipe" and task.start_tree == committed_tree(task.id)
    assert bench_seed.STARTS[task.id] == "mlpipe_start"
    assert "holdout.csv" in task.check_files


def test_the_check_image_pins_the_ml_dependencies():
    tree = ast.parse(Path(_bench_check.__file__).read_text())
    packages = [
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_pip_packages"
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]
    for name in ("scikit-learn", "pandas"):
        assert any(p.startswith(f"{name}==") and p.split("==")[1] for p in packages)


def test_the_training_data_is_committed_and_the_holdout_is_separate():
    train = (START / "mlpipe" / "train.csv").read_text()
    assert train == (REF / "mlpipe" / "train.csv").read_text()
    rows = list(csv.DictReader(train.splitlines()))
    assert len(rows) == 2000
    assert {r["churn"] for r in rows} == {"0", "1"}
    holdout = _bench_suite.get("ml-pipeline").check_files["holdout.csv"]
    assert not set(train.splitlines()[1:]) & set(holdout.splitlines()[1:])


def test_the_reference_passes_the_hidden_check(tmp_path, ml_dependencies):
    result = check(copy_case(tmp_path))
    assert result["exit_code"] == 0, result["summary"]


def test_the_start_fails_the_hidden_check(tmp_path, ml_dependencies):
    result = check(copy_case(tmp_path, START))
    assert result["exit_code"] != 0, result["summary"]
    assert "test_train_and_eval_are_cached_flyte_tasks" in result["summary"]


@pytest.mark.parametrize("variant", ["undecorated", "uncached", "bad_model"])
def test_the_hidden_check_rejects_other_incorrect_solutions(tmp_path, ml_dependencies, variant):
    project = copy_case(tmp_path)
    module = project / "mlpipe" / "train.py"
    text = module.read_text()
    old, new = {
        "undecorated": ('@env.task(cache="auto")\ndef train', "def train"),
        "uncached": ('@env.task(cache="auto")\ndef eval', "@env.task\ndef eval"),
        "bad_model": (
            'model.fit(data.drop(columns="churn"), data["churn"])',
            'model.fit(data.drop(columns="churn"), data.index % 2)',
        ),
    }[variant]
    assert old in text
    module.write_text(text.replace(old, new))
    result = check(project)
    assert result["exit_code"] != 0, result["summary"]
    failed = "test_cli_scores_the_hidden_holdout" if variant == "bad_model" else "test_train_and_eval"
    assert failed in result["summary"], result["summary"]


@pytest.mark.parametrize("variant", ["cache_constant", "shared_decorator", "another_module", "numpy"])
def test_equivalent_pipelines_pass(tmp_path, ml_dependencies, variant):
    project = copy_case(tmp_path)
    module = project / "mlpipe" / "train.py"
    text = module.read_text()
    if variant == "cache_constant":
        text = text.replace('@env.task(cache="auto")', "@env.task(cache=CACHE_MODE)")
        text = text.replace(
            "@env.task(cache=CACHE_MODE)", 'CACHE_MODE = "auto"\n\n\n@env.task(cache=CACHE_MODE)', 1
        )
    elif variant == "shared_decorator":
        text = text.replace('@env.task(cache="auto")', "@cached")
        text = text.replace("@cached", 'cached = env.task(cache="auto")\n\n\n@cached', 1)
    elif variant == "another_module":
        module.rename(project / "mlpipe" / "pipeline.py")
        (project / "mlpipe" / "__main__.py").write_text("from mlpipe.pipeline import main\n\nmain()\n")
        module = project / "mlpipe" / "pipeline.py"
    else:
        text = text.replace('data.drop(columns="churn")', 'data.drop(columns="churn").to_numpy()')
    module.write_text(text)
    result = check(project)
    assert result["exit_code"] == 0, result["summary"]


@pytest.mark.parametrize("variant", ["dummy_print", "ignore_eval", "training_data", "existing_tests"])
def test_qa_attacks_are_rejected(tmp_path, ml_dependencies, variant):
    project = copy_case(tmp_path)
    module = project / "mlpipe" / "train.py"
    if variant == "dummy_print":
        module.write_text(
            """\
if False:
    @env.task(cache="auto")
    def load(): pass
    @env.task(cache="auto")
    def train(): pass
    @env.task(cache="auto")
    def eval(): pass
"""
        )
        (project / "mlpipe" / "__main__.py").write_text('print("accuracy=1.0")\n')
    elif variant == "ignore_eval":
        text = module.read_text().replace(
            "args = parser.parse_args()", "args = parser.parse_args()\n    args.eval_csv = None"
        )
        module.write_text(text)
    elif variant == "training_data":
        path = project / "mlpipe" / "train.csv"
        path.write_text(path.read_text() + "\n")
    else:
        path = project / "tests" / "test_data.py"
        path.write_text(path.read_text() + "\n")
    result = check(project)
    assert result["exit_code"] != 0, result["summary"]


def test_tasks_in_a_guarded_main_module_pass(tmp_path, ml_dependencies):
    # QA round 2: the discovery used to skip `__main__`, so a correct pipeline placed there was invisible.
    project = copy_case(tmp_path)
    module = project / "mlpipe" / "train.py"
    (project / "mlpipe" / "__main__.py").write_text(module.read_text())
    module.unlink()
    result = check(project)
    assert result["exit_code"] == 0, result["summary"]


_KNN = """\
import argparse
import csv
from pathlib import Path


def _rows(path):
    with open(path) as stream:
        return list(csv.DictReader(stream))


def _vector(row):
    return [float(row[name]) for name in ("tenure_months", "monthly_charge", "support_calls")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-csv", type=Path, default=Path(__file__).with_name("train.csv"))
    parser.add_argument("--eval-csv", type=Path)
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    train = [(_vector(r), r["churn"]) for r in _rows(args.train_csv)]
    scale = [max(v[i] for v, _ in train) or 1.0 for i in range(3)]
    holdout = _rows(args.eval_csv or args.train_csv)

    def predict(row):
        point = [x / s for x, s in zip(_vector(row), scale)]
        near = sorted(
            train, key=lambda t: sum((a / s - b) ** 2 for a, s, b in zip(t[0], scale, point))
        )[:15]
        return "1" if sum(label == "1" for _, label in near) * 2 > len(near) else "0"

    labels = [predict(row) for row in holdout]
    if args.predictions:
        args.predictions.write_text("".join(f"{label}\\n" for label in labels))
        return
    print("accuracy=1.0")


main()
"""


@pytest.mark.parametrize("variant", ["trivial_tasks_and_knn", "prints_a_perfect_score", "other_model"])
def test_qa_round_two_attacks_are_rejected(tmp_path, ml_dependencies, variant):
    project = copy_case(tmp_path)
    module = project / "mlpipe" / "train.py"
    if variant == "trivial_tasks_and_knn":
        # Genuine cached Flyte tasks that nothing runs, beside a stdlib k-NN and a fabricated score.
        module.write_text(
            """\
import flyte

env = flyte.TaskEnvironment(name="mlpipe")


@env.task(cache="auto")
def load(path: str) -> str:
    return path


@env.task(cache="auto")
def train(data: str) -> str:
    return data


@env.task(cache="auto")
def eval(model: str, data: str) -> float:
    return 1.0
"""
        )
        (project / "mlpipe" / "__main__.py").write_text(_KNN)
    elif variant == "prints_a_perfect_score":
        # The real pipeline, but the printed accuracy is not the one the predictions earn.
        text = module.read_text()
        old = 'print(f"accuracy={accuracy:.6f}")'
        assert old in text
        module.write_text(text.replace(old, 'print("accuracy=1.000000")'))
    else:
        # Both tasks run, but the model is not logistic regression.
        text = module.read_text()
        old = "model = LogisticRegression(max_iter=1000, random_state=42)"
        assert old in text
        text = text.replace(old, "model = KNeighborsClassifier(n_neighbors=15)")
        knn = "from sklearn.neighbors import KNeighborsClassifier"
        text = text.replace("import pandas as pd", f"{knn}\nimport pandas as pd")
        module.write_text(text.replace("-> LogisticRegression:", "-> KNeighborsClassifier:"))
    result = check(project)
    assert result["exit_code"] != 0, result["summary"]


def test_the_whole_check_is_bounded_to_sixty_seconds():
    text = _bench_suite.get("ml-pipeline").check_files["test_hidden_mlpipe.py"]
    assert "BUDGET_S = 60" in text
    assert "timeout=20" not in text


@pytest.mark.real_factory_gates
def test_the_start_has_no_blocking_gate_failure(tmp_path):
    workspace = tmp_path / "ws"
    shutil.copytree(START, workspace / "app")
    for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "start")):
        subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.org", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
    assert _gates.run(workspace, "app", "", "", "", {}, _timeline._Timeline()) == ""

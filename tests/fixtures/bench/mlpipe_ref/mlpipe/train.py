"""Train a customer-churn classifier from the committed synthetic CSV."""

import argparse
from pathlib import Path

import flyte

# Without pandas-stubs, mypy reports pandas and scikit-learn as untyped when they are installed and as
# missing when they are not; each ignore covers both, so the check passes either way.
import pandas as pd  # type: ignore[import-untyped, import-not-found, unused-ignore]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped, import-not-found, unused-ignore]
from sklearn.metrics import accuracy_score  # type: ignore[import-untyped, import-not-found, unused-ignore]
from sklearn.model_selection import train_test_split  # type: ignore[import-untyped, import-not-found, unused-ignore]

env = flyte.TaskEnvironment(name="mlpipe")


@env.task(cache="auto")
def load(path: str) -> pd.DataFrame:
    """Read a churn dataset with the three features and a churn label."""
    return pd.read_csv(path)


@env.task(cache="auto")
def train(data: pd.DataFrame) -> LogisticRegression:
    """Fit logistic regression using only the training rows."""
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(data.drop(columns="churn"), data["churn"])
    return model


@env.task(cache="auto")
def eval(model: LogisticRegression, data: pd.DataFrame) -> float:
    """Measure classification accuracy on held-out rows."""
    return float(accuracy_score(data["churn"], model.predict(data.drop(columns="churn"))))


def main() -> None:
    """Train on the training CSV, then print the holdout accuracy or write its predictions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-csv", type=Path, default=Path(__file__).with_name("train.csv"))
    parser.add_argument("--eval-csv", type=Path)
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    flyte.init()
    # Direct task calls execute locally in Flyte 2, without a cluster or serialization dependencies.
    data = load(str(args.train_csv))
    if args.eval_csv:
        training, holdout = data, load(str(args.eval_csv))
    else:
        training, holdout = train_test_split(
            data, test_size=0.25, random_state=42, stratify=data["churn"]
        )
    model = train(training)
    if args.predictions:
        features = holdout.drop(columns="churn", errors="ignore")
        labels = model.predict(features)
        args.predictions.write_text("".join(f"{int(label)}\n" for label in labels))
        return
    accuracy = eval(model, holdout)
    print(f"accuracy={accuracy:.6f}")


if __name__ == "__main__":
    main()

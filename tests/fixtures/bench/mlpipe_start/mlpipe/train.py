"""Train a customer-churn classifier from the committed synthetic CSV."""

import argparse
from pathlib import Path

# Without pandas-stubs, mypy reports pandas and scikit-learn as untyped when they are installed and as
# missing when they are not; each ignore covers both, so the check passes either way.
import pandas as pd  # type: ignore[import-untyped, import-not-found, unused-ignore]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped, import-not-found, unused-ignore]
from sklearn.metrics import accuracy_score  # type: ignore[import-untyped, import-not-found, unused-ignore]
from sklearn.model_selection import train_test_split  # type: ignore[import-untyped, import-not-found, unused-ignore]


def main() -> None:
    """Train on the training CSV and print the holdout accuracy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-csv", type=Path, default=Path(__file__).with_name("train.csv"))
    parser.add_argument("--eval-csv", type=Path)
    args = parser.parse_args()
    data = pd.read_csv(args.train_csv)
    if args.eval_csv:
        training, holdout = data, pd.read_csv(args.eval_csv)
    else:
        training, holdout = train_test_split(
            data, test_size=0.25, random_state=42, stratify=data["churn"]
        )
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(training.drop(columns="churn"), training["churn"])
    accuracy = accuracy_score(holdout["churn"], model.predict(holdout.drop(columns="churn")))
    print(f"accuracy={accuracy:.6f}")


if __name__ == "__main__":
    main()

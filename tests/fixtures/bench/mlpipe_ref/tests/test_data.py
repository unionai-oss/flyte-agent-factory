import csv
from pathlib import Path

import mlpipe


def test_the_training_dataset_is_available():
    path = Path(mlpipe.__file__).with_name("train.csv")
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2000
    assert {row["churn"] for row in rows} == {"0", "1"}

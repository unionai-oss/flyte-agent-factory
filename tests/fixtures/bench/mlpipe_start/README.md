# mlpipe

Predict customer churn from tenure in months, monthly charge and support-call count.
`mlpipe/train.csv` contains 2,000 synthetic customers, with binary `churn` labels.
The fixed training seed is 202604; holdout customers use a different seed from the same distribution.
Churn increases with charge and support calls and decreases with tenure, with Gaussian label noise.
This is synthetic benchmark data, not customer records.

Requires Python 3.12, scikit-learn 1.6.1 and pandas 2.2.3.
The pipeline also requires Flyte 2.10.7.

```sh
python -m mlpipe.train
```

With no arguments, use a stratified 25% validation split (seed 42).
`--train-csv PATH` selects training data, and `--eval-csv PATH` selects a separate holdout.
Prints `accuracy=NUMBER`; CSV columns are `tenure_months,monthly_charge,support_calls,churn`.

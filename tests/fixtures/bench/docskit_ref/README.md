# docskit

Text and number helpers with a small command line.

Run these from this directory:

```sh
python -m docskit slug "Hello, World"
python -m docskit summary 1 2 3 4
```

`slug` prints a lowercase URL-safe form with hyphens between words. `summary` prints the mean and median of the numbers.
Run the tests with `python -m pytest -q`.

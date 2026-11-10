# docskit

Text and number helpers with a small command line.

Run these from this directory:

```sh
python -m docskit slug --text "Hello, World"
python -m docskit stats 1 2 3 4
```

`slug` preserves uppercase letters and uses underscores between words. `stats` prints only the sum.
Run the tests with `python -m pytest -q`.

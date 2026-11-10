import subprocess
import sys

from docskit import stats, text


def test_slugify():
    assert text.slugify("Hello, World!") == "hello-world"


def test_truncate_keeps_the_limit_including_the_marker():
    assert text.truncate("abcdefghij", 8) == "abcde..."
    assert text.truncate("abc", 8) == "abc"


def test_mean_and_median():
    assert stats.mean([1, 2, 6]) == 3
    assert stats.median([5, 1, 3]) == 3
    assert stats.median([1, 2, 3, 4]) == 2.5


def test_summary_command():
    done = subprocess.run(
        [sys.executable, "-m", "docskit", "summary", "1", "2", "3", "4"],
        capture_output=True,
        text=True,
    )
    assert done.stdout == "mean=2.5 median=2.5\n"

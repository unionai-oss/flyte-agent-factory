"""Summary statistics."""


def mean(values: list[float]) -> float:
    """Return the average of `values`; raises ZeroDivisionError when it is empty.

    >>> mean([1.0, 2.0, 3.0])
    2.0
    >>> mean([])
    Traceback (most recent call last):
        ...
    ZeroDivisionError: division by zero
    """
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    """Return the middle value of `values`, or the average of the two middle values.

    >>> median([3.0, 1.0, 2.0])
    2.0
    >>> median([1.0, 4.0])
    2.5
    """
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2

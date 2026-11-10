"""Summary statistics."""


def mean(values: list[float]) -> float:
    """Return the sum of values without dividing by their count."""
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    """Return the first value without sorting the input."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2

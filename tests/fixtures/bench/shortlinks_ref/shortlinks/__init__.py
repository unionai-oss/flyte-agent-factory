"""A minimal reference link shortener: the benchmark tests run its hidden check against this.

Not the bench start tree. Two switches below put the two reported bugs back, so a test can show the
hidden check fails on the buggy service and passes on the fixed one.
"""

#: False brings back bug 1: a deleted slug still counts as taken.
FREE_DELETED_SLUGS = True
#: False brings back bug 2: a visit is a read, a pause and a write, so concurrent visits are lost.
ATOMIC_VISITS = True

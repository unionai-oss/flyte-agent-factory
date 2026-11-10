from concurrent.futures import ThreadPoolExecutor

from test_api import call, port  # type: ignore[import-not-found]  # noqa: F401


def test_deleted_slug_is_reusable(port):  # noqa: F811 -- the imported `port` fixture, taken by name
    body = {"slug": "reusable", "url": "https://example.com"}
    assert call(port, "POST", "/links", body)[0] == 201
    assert call(port, "DELETE", "/links/reusable")[0] == 204
    assert call(port, "POST", "/links", body)[0] == 201
    assert call(port, "GET", "/links/reusable")[1]["visits"] == 0


def test_parallel_visits_are_counted(port):  # noqa: F811 -- the imported `port` fixture, taken by name
    assert (
        call(port, "POST", "/links", {"slug": "parallel", "url": "https://example.com"})[0] == 201
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: call(port, "GET", "/parallel")[0], range(50)))
    assert statuses == [302] * 50
    assert call(port, "GET", "/links/parallel")[1]["visits"] == 50

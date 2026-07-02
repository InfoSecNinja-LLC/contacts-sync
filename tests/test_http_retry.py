from contacts_sync.http_retry import request_with_retry


def test_returns_immediately_on_success(requests_mock):
    requests_mock.get("https://example.com/x", status_code=200, text="ok")
    response = request_with_retry("GET", "https://example.com/x")
    assert response.status_code == 200


def test_retries_on_429_then_succeeds(requests_mock):
    requests_mock.get(
        "https://example.com/x",
        [{"status_code": 429, "headers": {"Retry-After": "0"}}, {"status_code": 200, "text": "ok"}],
    )
    sleeps = []
    response = request_with_retry("GET", "https://example.com/x", sleep=sleeps.append)
    assert response.status_code == 200
    assert sleeps == [0.0]


def test_gives_up_after_max_attempts(requests_mock):
    requests_mock.get("https://example.com/x", status_code=503)
    sleeps = []
    response = request_with_retry("GET", "https://example.com/x", max_attempts=3, sleep=sleeps.append)
    assert response.status_code == 503
    assert len(sleeps) == 2

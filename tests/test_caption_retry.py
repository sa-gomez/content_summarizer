"""Caption retry/throttle tests (issue #24). Run:
uv run --with pytest --with "yt-dlp[default]" --with faster-whisper --with "av<17" pytest tests
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from yt_dlp.utils import DownloadError

import transcribe as t


class FakeHTTP(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP Error {status}")
        self.status = status
        self.headers = {"Retry-After": str(retry_after)} if retry_after else {}


def make_fetch(errors, result=("info", [(0, "hi")])):
    calls = []

    def fetch(url, lang, tmp):
        calls.append(url)
        if errors:
            raise errors.pop(0)
        return result

    fetch.calls = calls
    return fetch


def run(fetch, **kw):
    sleeps = []
    out = t.get_captions_with_retry("u", "en", Path("."), fetch=fetch, sleep=sleeps.append, **kw)
    return out, sleeps


def test_transient_429_then_success_no_whisper():
    fetch = make_fetch([DownloadError("ERROR: HTTP Error 429: Too Many Requests")] * 2)
    out, sleeps = run(fetch)
    assert out == ("info", [(0, "hi")])
    assert len(fetch.calls) == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0]  # backoff grows


def test_persistent_429_raises_after_budget():
    fetch = make_fetch([t.CaptionTransientError("HTTP Error 429")] * 10)
    with pytest.raises(t.CaptionTransientError):
        run(fetch)
    assert len(fetch.calls) == t.CAPTION_RETRIES + 1


def test_non_transient_not_retried():
    fetch = make_fetch([DownloadError("ERROR: Video unavailable"), ValueError("x")])
    with pytest.raises(DownloadError):
        run(fetch)
    assert len(fetch.calls) == 1


def test_5xx_retried():
    fetch = make_fetch([FakeHTTP(503)])
    run(fetch)
    assert len(fetch.calls) == 2


def test_retry_after_header_honoured():
    fetch = make_fetch([FakeHTTP(429, retry_after=7)])
    _, sleeps = run(fetch)
    assert 7 <= sleeps[0] <= 7 * 1.25


def test_max_total_wait_caps():
    fetch = make_fetch([FakeHTTP(429, retry_after=100)] * 5)
    with pytest.raises(FakeHTTP):
        run(fetch, max_total_wait=60)
    assert len(fetch.calls) == 1


@pytest.mark.parametrize("msg,expected", [
    ("HTTP Error 429: Too Many Requests", True),
    ("HTTP Error 502: Bad Gateway", True),
    ("Private video", False),
    ("HTTP Error 404: Not Found", False),
])
def test_classify(msg, expected):
    assert t.classify_error(DownloadError(msg))[0] is expected


def test_throttle_spaces_requests_and_backs_off():
    sleeps = []
    th = t.Throttle(3.0, sleep=sleeps.append)
    th.wait()
    assert sleeps == []  # first request is not delayed
    th.wait()
    assert 3.0 <= sleeps[0] <= 4.5
    th.rate_limited()
    th.wait()
    assert sleeps[1] >= 6.0

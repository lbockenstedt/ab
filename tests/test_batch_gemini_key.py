"""The Gemini batch calls must keep the API key out of the URL: requests puts
the URL into HTTPError and connection-error messages, and batch.py logs those
(`batch: submit failed for …: {e}`, `batch: poll failed for …: {e}`)."""
import batch

KEY = "AIzaTESTKEY-not-real"


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def raise_for_status(self):
        """Never fails."""

    def json(self):
        """An operation name for submit; a still-running operation for poll."""
        return {"name": "batches/abc", "done": False}


def test_gemini_submit_and_poll_send_key_in_header(monkeypatch):
    """Submit and poll put the key in x-goog-api-key, never the URL."""
    calls = []

    def fake(url, **kw):
        """Record the call."""
        calls.append((url, kw.get("headers") or {}))
        return _Resp()

    monkeypatch.setattr(batch.requests, "post", fake)
    monkeypatch.setattr(batch.requests, "get", fake)
    item = {"prompt": "p", "system": "", "custom_id": "c1"}
    assert batch._submit_gemini([item], KEY, "gemini-x") == "batches/abc"
    assert batch._poll_gemini("batches/abc", KEY) is None
    assert len(calls) == 2
    for url, headers in calls:
        assert KEY not in url
        assert headers.get("x-goog-api-key") == KEY

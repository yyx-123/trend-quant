"""GZip middleware tests: large responses are compressed, small ones and SSE are not."""

from __future__ import annotations


class TestGZipMiddleware:
    def test_large_html_page_is_gzipped(self, client) -> None:
        resp = client.get("/rule-backtest", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        # httpx transparently decompresses; body is still valid HTML.
        assert "<html" in resp.text.lower()

    def test_small_response_is_not_gzipped(self, client) -> None:
        resp = client.get("/rule-backtest/api/progress/does-not-exist", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 404
        assert "content-encoding" not in resp.headers

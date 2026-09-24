"""Shared pytest fixtures. Network is BLOCKED in every test: all fetches go through
``weather.http.get_bytes`` / ``head_ok`` (modules must call them as module attributes),
and the ``fake_http`` fixture replaces them with an in-memory registry."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from weather import http as _http          # noqa: E402
from weather.config import Config         # noqa: E402


class FakeHttp:
    """Registry of URL fragment -> response. ``add(fragment, body)`` where body is bytes,
    str, or a JSON-serialisable object; ``fail(fragment)`` makes that URL raise HttpError.
    The FIRST registered fragment contained in the requested URL wins. Unregistered URLs
    raise HttpError (so a test never hits the network by accident)."""

    def __init__(self):
        self.routes = []
        self.calls = []
        self.cond_meta = []             # (fragment, etag, last_modified) for fetch_conditional
        self.no_cache = []              # fragments answered with Cache-Control: no-cache

    def add(self, fragment, body, status_ok=True):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.routes.append((fragment, body, status_ok))

    def fail(self, fragment):
        self.routes.append((fragment, None, False))

    def _lookup(self, url):
        for frag, body, ok in self.routes:
            if frag in url:
                return body, ok
        return None, False

    def get_bytes(self, url, cfg, accept=None, timeout=None, retries=None, method="GET"):
        self.calls.append(url)
        body, ok = self._lookup(url)
        if not ok or body is None:
            raise _http.HttpError("fake: no route for %s" % url)
        return body

    def head_ok(self, url, cfg, timeout=15.0):
        self.calls.append("HEAD " + url)
        body, ok = self._lookup(url)
        return bool(ok and body is not None)

    def blocked(self, fragment):
        """Answer ``fetch_cacheable`` for URLs containing ``fragment`` the way OpenStreetMap
        answers a blocked client: HTTP 200 with ``Cache-Control: no-cache``."""
        self.no_cache.append(fragment)

    def fetch_cacheable(self, url, cfg, etag=None, last_modified=None, accept=None,
                        timeout=None):
        """Stand-in for ``http.fetch_cacheable``: 200 + the registered body (the plain URL is
        recorded in ``.calls``, like ``get_bytes``)."""
        self.calls.append(url)
        body, ok = self._lookup(url)
        if not ok or body is None:
            raise _http.HttpError("fake: no route for %s" % url)
        return {"status": 200, "body": body, "etag": None, "last_modified": None,
                "no_cache": any(f in url for f in self.no_cache), "max_age": None}

    def validators(self, fragment, etag=None, last_modified=None):
        """Give the URLs containing ``fragment`` an ETag / Last-Modified for
        ``fetch_conditional`` (a request that sends the same ETag then gets a 304)."""
        self.cond_meta.append((fragment, etag, last_modified))

    def fetch_conditional(self, url, cfg, etag=None, last_modified=None, accept=None,
                          timeout=None):
        """Stand-in for ``http.fetch_conditional``: 200 + the registered body (recorded as
        ``"COND <url>"`` in ``.calls``), or 304 when ``validators()`` gave the URL an ETag
        and the caller sent it back."""
        self.calls.append("COND " + url)
        body, ok = self._lookup(url)
        if not ok or body is None:
            raise _http.HttpError("fake: no route for %s" % url)
        for frag, et, lm in self.cond_meta:
            if frag in url:
                if et is not None and etag == et:
                    return 304, None, et, lm or last_modified
                return 200, body, et, lm
        return 200, body, None, None


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*a, **k):
        raise AssertionError("network access attempted in a test: %r" % (a[:1],))
    monkeypatch.setattr("urllib.request.urlopen", _blocked)


@pytest.fixture
def fake_http(monkeypatch):
    fh = FakeHttp()
    monkeypatch.setattr(_http, "get_bytes", fh.get_bytes)
    monkeypatch.setattr(_http, "head_ok", fh.head_ok)
    monkeypatch.setattr(_http, "fetch_conditional", fh.fetch_conditional)
    monkeypatch.setattr(_http, "fetch_cacheable", fh.fetch_cacheable)
    return fh


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("WEATHER_"):
            monkeypatch.delenv(k, raising=False)
    return Config.from_env(out_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"),
                           map_px=240, tile_zoom=8)


@pytest.fixture
def cache(cfg):
    return _http.Cache(cfg.cache_dir)

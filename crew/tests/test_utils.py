"""
Regression tests for mc_utils._safe_request.

The default browser UA fix is what stopped ESPN's silent 403s. If anyone
later removes that default, this test fails immediately.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mc_utils import _safe_request, _DEFAULT_HEADERS  # noqa: E402


class TestSafeRequestDefaults:

    def test_default_user_agent_is_browser_like(self):
        ua = _DEFAULT_HEADERS["User-Agent"]
        assert "Mozilla/5.0" in ua, (
            "Default UA must look browser-like. ESPN 403s the bare "
            "python-requests UA — see crewai-migration commit for context."
        )

    def test_default_headers_dont_include_accept(self):
        # Adding `Accept: */*` makes ESPN return HTTP 202 with an empty body
        # (anti-bot challenge). Keep the header set minimal.
        assert "Accept" not in _DEFAULT_HEADERS

    def test_caller_headers_override_default(self):
        """When a caller passes their own headers (e.g. Reddit's UA), the
        default browser UA must NOT be applied silently."""
        custom_headers = {"User-Agent": "MikeCast/2.0 (Reddit)"}
        with patch("mc_utils.requests.get") as get_mock:
            get_mock.return_value = MagicMock(status_code=200)
            _safe_request("https://example.com", headers=custom_headers)
        assert get_mock.called
        _, kwargs = get_mock.call_args
        assert kwargs["headers"] == custom_headers

    def test_no_headers_uses_default(self):
        with patch("mc_utils.requests.get") as get_mock:
            get_mock.return_value = MagicMock(status_code=200)
            _safe_request("https://example.com")
        _, kwargs = get_mock.call_args
        assert kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

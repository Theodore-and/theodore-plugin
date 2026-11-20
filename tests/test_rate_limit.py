"""A rate limit from Theodore's service that lasts (an allowance per account and hour) is told apart from a moment of
load: the status says when the next request can go and never says "busy" or "try again in a moment"."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ClientCase  # noqa: E402


class LongRateLimit(ClientCase):
    def test_a_rate_limit_that_lasts_is_an_allowance_used_up_and_is_not_called_busy(self):
        """An hourly limit (Retry-After of many minutes) is not load: the status says when, tells the assistant not to
        wait or repeat, and never says "busy" or "in a moment". A short wait keeps the short form."""
        self.sign_in()
        self.api.fail_with = None
        self.api.path_errors["/v1/guide/deck-design"] = (429, "rate_limited", {"Retry-After": "1500"})
        _f, nxt, tell = self.assertStatus(self.client("guide", "deck-design"), "RETRY", 75)
        self.assertIn("do not wait for it and do not run this command again now", nxt)
        self.assertIn("about 25 minutes", nxt)
        self.assertNotIn("at most three times", nxt)
        self.assertIn("about 25 minutes", tell)
        self.assertNotIn("busy", tell)
        self.assertNotIn("in a moment", tell)
        self.api.path_errors["/v1/guide/deck-design"] = (429, "rate_limited", {"Retry-After": "120"})
        _f, nxt, tell = self.assertStatus(self.client("guide", "deck-design"), "RETRY", 75)
        self.assertIn("wait 120 seconds", nxt)
        self.assertIn("busy", tell)


if __name__ == "__main__":
    unittest.main()

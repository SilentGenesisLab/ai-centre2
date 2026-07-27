from __future__ import annotations

import unittest

from control_plane.api import app


class OpenApiTests(unittest.TestCase):
    def test_public_domain_is_the_primary_server(self) -> None:
        servers = app.openapi()["servers"]

        self.assertEqual(
            servers[0]["url"],
            "http://aicentre2.sligenai.cn:8320",
        )
        self.assertEqual(servers[1]["url"], "http://127.0.0.1:8320")


if __name__ == "__main__":
    unittest.main()

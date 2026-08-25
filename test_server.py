import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class TrackingStatusTests(unittest.TestCase):
    def test_tracking_status_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            server, "TRACKING_STATUSES_FILE", Path(directory) / "tracking_statuses.json"
        ):
            server.save_tracking_status("8114731", "viewing_requested")
            server.save_tracking_status("123", "sold")
            self.assertEqual(
                server.load_tracking_statuses(),
                {"8114731": "viewing_requested", "123": "sold"},
            )

            server.save_tracking_status("8114731", None)
            self.assertEqual(server.load_tracking_statuses(), {"123": "sold"})

    def test_tracking_status_rejects_unknown_value(self):
        with self.assertRaisesRegex(ValueError, "invalid tracking status"):
            server.save_tracking_status("1", "maybe")

    def test_all_expected_tracking_statuses_are_supported(self):
        self.assertEqual(
            server.TRACKING_STATUS_VALUES,
            {
                "viewing_requested",
                "viewing_planned",
                "viewed",
                "bid",
                "sold",
                "bought",
            },
        )

    def test_tracking_http_api_persists_and_returns_status(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            server, "TRACKING_STATUSES_FILE", Path(directory) / "tracking_statuses.json"
        ), patch.object(server, "PASSWORD", None):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            try:
                connection.request(
                    "POST",
                    "/track",
                    body=json.dumps({"id": "8114731", "status": "viewed"}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 204)

                connection.request("GET", "/tracking-statuses.json")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload, {"8114731": "viewed"})

                connection.request(
                    "POST",
                    "/track",
                    body=json.dumps({"id": "8114731", "status": "wishful_thinking"}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 400)
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

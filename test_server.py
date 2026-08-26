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
            server.save_tracking_status("8114731", "call")
            server.save_tracking_status("123", "sold")
            self.assertEqual(
                server.load_tracking_statuses(),
                {"8114731": "call", "123": "sold"},
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
                "call",
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

    def test_listing_row_batch_is_served(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            server.core, "ROOT", Path(directory)
        ), patch.object(server, "PASSWORD", None):
            batch_file = server.core.row_batch_path(1)
            batch_file.parent.mkdir(parents=True)
            batch_file.write_text('<tr data-id="129"></tr>')
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            try:
                connection.request("GET", "/listing-rows/1.html")
                response = connection.getresponse()
                body = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertEqual(body, '<tr data-id="129"></tr>')

                connection.request("GET", "/listing-rows/2.html")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 404)
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)


class ListingAnalysisTests(unittest.TestCase):
    def analysis(self):
        return {
            "market": {
                "estimate_low": 700000,
                "estimate_high": 750000,
                "summary": "Likely deliberately under-asked.",
            },
            "vve": {"risk": "medium", "monthly_eur": 250, "summary": "Check MJOP."},
            "erfpacht": {
                "risk": "low",
                "headline": "Bought out perpetually",
                "summary": "No regular canon.",
            },
            "flags": ["Non-owner-occupancy clause."],
            "questions": ["Any planned special assessment?"],
            "sources": [{"label": "Funda", "url": "https://example.com/listing"}],
        }

    def test_request_and_completed_analysis_round_trip(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            server, "ANALYSES_FILE", Path(directory) / "listing_analyses.json"
        ), patch.object(
            server, "ANALYSIS_REQUESTS_FILE", Path(directory) / "analysis_requests.json"
        ), patch.object(
            server,
            "analysis_request_context",
            return_value={
                "listing_url": "https://www.funda.nl/detail/example",
                "brochure_url": "https://cloud.funda.nl/valentina_media/example.pdf",
            },
        ):
            request = server.request_listing_analysis("8114731")
            self.assertIn("requested_at", request)
            self.assertEqual(
                request["brochure_url"],
                "https://cloud.funda.nl/valentina_media/example.pdf",
            )
            self.assertEqual(server.load_analysis_requests(), {"8114731": request})

            server.save_listing_analysis("8114731", self.analysis())
            saved = server.load_analyses()["8114731"]
            self.assertEqual(saved["market"]["estimate_low"], 700000)
            self.assertIn("updated_at", saved)
            self.assertEqual(saved["brochure_url"], request["brochure_url"])
            self.assertEqual(server.load_analysis_requests(), {})

    def test_analysis_request_context_discovers_missing_brochure(self):
        listing_url = "https://www.funda.nl/detail/koop/amsterdam/example/123/"
        brochure_url = "https://cloud.funda.nl/valentina_media/example.pdf"
        with patch.object(
            server.core,
            "load_listings",
            return_value={"123": {"url": listing_url}},
        ), patch.object(
            server.core, "fetch_brochure_url", return_value=brochure_url
        ) as discover:
            self.assertEqual(
                server.analysis_request_context("123"),
                {"listing_url": listing_url, "brochure_url": brochure_url},
            )
        discover.assert_called_once_with(listing_url)

    def test_analysis_rejects_invalid_risk(self):
        analysis = self.analysis()
        analysis["vve"]["risk"] = "perfect"
        with self.assertRaisesRegex(ValueError, "analysis.vve.risk is invalid"):
            server.validate_listing_analysis(analysis)

    def test_analysis_rejects_unsafe_source_url(self):
        analysis = self.analysis()
        analysis["sources"][0]["url"] = "javascript:alert(1)"
        with self.assertRaisesRegex(ValueError, "URLs must use HTTP"):
            server.validate_listing_analysis(analysis)

    def test_analysis_http_api_queues_and_completes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            server, "ANALYSES_FILE", Path(directory) / "listing_analyses.json"
        ), patch.object(
            server, "ANALYSIS_REQUESTS_FILE", Path(directory) / "analysis_requests.json"
        ), patch.object(
            server, "analysis_request_context", return_value={}
        ), patch.object(server, "PASSWORD", None):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            try:
                connection.request(
                    "POST",
                    "/request-analysis",
                    body=json.dumps({"id": "8114731"}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 202)

                connection.request("GET", "/analysis-state.json")
                response = connection.getresponse()
                state = json.loads(response.read())
                self.assertIn("8114731", state["requests"])

                connection.request(
                    "POST",
                    "/analysis",
                    body=json.dumps({"id": "8114731", "analysis": self.analysis()}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 204)

                connection.request("GET", "/analysis-state.json")
                response = connection.getresponse()
                state = json.loads(response.read())
                self.assertIn("8114731", state["analyses"])
                self.assertNotIn("8114731", state["requests"])
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

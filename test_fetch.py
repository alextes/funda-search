import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import fetch


SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[4.0, 52.0], [5.0, 52.0], [5.0, 53.0], [4.0, 53.0], [4.0, 52.0]]
    ],
}


class HistoryTests(unittest.TestCase):
    def test_legacy_snapshot_is_idempotent(self):
        listings = {
            "1": {
                "price": 500_000,
                "status": "available",
                "first_seen": "2026-07-06",
            }
        }

        self.assertTrue(fetch.ensure_histories(listings))
        self.assertFalse(fetch.ensure_histories(listings))
        self.assertEqual(
            listings["1"]["price_history"],
            [
                {
                    "price": 500_000,
                    "observed_at": "2026-07-06",
                    "source": "legacy_snapshot",
                }
            ],
        )

    def test_only_changes_are_recorded(self):
        listing = {"price_history": [], "status_history": []}
        timestamp = "2026-08-24T08:00:00+00:00"

        self.assertTrue(
            fetch.record_observation(
                listing, price=500_000, status="available", timestamp=timestamp
            )
        )
        self.assertFalse(
            fetch.record_observation(
                listing, price=500_000, status="available", timestamp=timestamp
            )
        )
        self.assertTrue(
            fetch.record_observation(listing, price=475_000, timestamp=timestamp)
        )
        self.assertEqual([event["price"] for event in listing["price_history"]], [500_000, 475_000])
        self.assertEqual(len(listing["status_history"]), 1)


class WebsiteSearchTests(unittest.TestCase):
    CONFIG = {
        "location": "amsterdam",
        "filters": {
            "min_price": 400_000,
            "max_price": 850_000,
            "min_area": 75,
            "min_bedrooms": 2,
        },
        "pages": 3,
        "website_max_pages": 20,
    }

    @staticmethod
    def page(*ids: str) -> str:
        links = "".join(
            f'<a href="/detail/koop/amsterdam/example-{listing_id}/{listing_id}/">x</a>'
            for listing_id in ids
        )
        return f'<script id="__NUXT_DATA__"></script>{links}'

    def test_public_listing_id_accepts_canonical_url_and_query(self):
        self.assertEqual(
            fetch.public_listing_id(
                "https://www.funda.nl/detail/koop/amsterdam/example/80923730/?x=1"
            ),
            "80923730",
        )
        self.assertIsNone(fetch.public_listing_id("https://www.funda.nl/zoeken/koop"))

    def test_website_search_url_matches_filters(self):
        url = fetch.website_search_url(self.CONFIG, 2)
        self.assertIn("selected_area=amsterdam", url)
        self.assertIn("price=400000-850000", url)
        self.assertIn("floor_area=75-", url)
        self.assertIn("bedrooms=2-", url)
        self.assertIn("sort=publish_date_utc_desc", url)
        self.assertIn("page=2", url)

    def test_parser_deduplicates_listing_links(self):
        page = self.page("80923730", "80923730")
        page += '<a href="/makelaar/example">ignore</a>'
        self.assertEqual(
            fetch.parse_listing_urls(page),
            ["https://www.funda.nl/detail/koop/amsterdam/example-80923730/80923730/"],
        )

    def test_cursor_stops_after_first_entirely_known_page(self):
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=200, text=self.page("30", "20")),
            Mock(status_code=200, text=self.page("20", "10")),
        ]
        listings = {
            "a": {"url": "https://www.funda.nl/detail/koop/amsterdam/a/10/"},
            "b": {"url": "https://www.funda.nl/detail/koop/amsterdam/b/20/"},
        }

        with patch.object(fetch, "WEBSITE_PAGE_SIZE", 2):
            urls = fetch.search_website(self.CONFIG, listings, session=session)

        self.assertEqual(len(urls), 3)
        self.assertEqual(fetch.public_listing_id(urls[0]), "30")
        self.assertEqual(session.get.call_count, 2)

    def test_bot_challenge_is_rejected(self):
        session = Mock()
        session.get.return_value = Mock(
            status_code=200,
            text="<html>Je bent bijna op de pagina</html>",
        )
        with self.assertRaisesRegex(RuntimeError, "bot challenge"):
            fetch.search_website(self.CONFIG, {}, session=session)

    def test_fetch_uses_website_urls_when_api_search_is_unauthorized(self):
        url = "https://www.funda.nl/detail/koop/amsterdam/example/80923730/"
        detail = Mock(global_id=8114731, id=17296467, title="Example")
        client = Mock()
        client.listing.return_value = detail
        context = MagicMock()
        context.__enter__.return_value = client
        listings = {}

        with patch.object(fetch, "Funda", return_value=context), patch.object(
            fetch, "search_pages", side_effect=fetch.SearchError("401: no token provided")
        ), patch.object(fetch, "search_website", return_value=[url]), patch.object(
            fetch, "build_record", return_value={"id": 8114731}
        ), patch.object(fetch.time, "sleep"):
            total, new = fetch.fetch(self.CONFIG, listings)

        self.assertEqual((total, new), (1, 1))
        self.assertEqual(listings["8114731"]["url"], url)
        client.listing.assert_called_once_with(url)


class PriceBandTests(unittest.TestCase):
    def test_band_parser_handles_ranges_and_open_upper_bound(self):
        self.assertEqual(
            fetch._parse_price_band({"SELECTIE": 5848, "LABEL": "5848-6683"}),
            {
                "year": 2025,
                "lower": 5848,
                "upper": 6683,
                "raw_label": "5848-6683",
            },
        )
        self.assertEqual(
            fetch._parse_price_band({"SELECTIE": 12532, "LABEL": "> 12532"})["upper"],
            None,
        )

    def test_point_in_polygon_respects_holes(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [
                SQUARE["coordinates"][0],
                [[4.4, 52.4], [4.6, 52.4], [4.6, 52.6], [4.4, 52.6], [4.4, 52.4]],
            ],
        }
        self.assertTrue(fetch._point_in_geometry(4.2, 52.2, geometry))
        self.assertFalse(fetch._point_in_geometry(4.5, 52.5, geometry))
        self.assertFalse(fetch._point_in_geometry(5.2, 52.2, geometry))

    def test_cached_municipality_dataset_loads(self):
        bands = fetch.load_price_bands()
        self.assertGreater(len(bands), 1000)
        self.assertTrue(all(band["year"] == 2025 for band in bands))

    def test_render_includes_band_and_history(self):
        listing = {
            "id": 1,
            "url": "https://example.test/listing",
            "title": "Example 1",
            "wijk": "District",
            "neighbourhood": "Neighbourhood",
            "price": 500_000,
            "living_area": 100,
            "price_per_m2": 5000,
            "rooms": 3,
            "energy_label": "A",
            "publication_date": "2026-08-01",
            "first_seen": "2026-08-01",
            "lat": 52.2,
            "lon": 4.2,
            "distance_km": 1.0,
            "floorplans": [],
            "photo_urls": [],
            "description": "Description",
            "status": "sold",
            "price_history": [
                {"price": 500_000, "observed_at": "2026-08-01", "source": "funda"}
            ],
            "status_history": [
                {"status": "sold", "observed_at": "2026-08-01", "source": "funda"}
            ],
        }
        bands = [
            {
                "year": 2025,
                "lower": 5848,
                "upper": 6683,
                "raw_label": "5848-6683",
                "geometry": SQUARE,
            }
        ]

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "overview.html"
            with patch.object(fetch, "ROOT", Path(directory)), patch.object(
                fetch, "OVERVIEW_FILE", output
            ), patch.object(fetch, "load_price_bands", return_value=bands):
                fetch.render({"location": "amsterdam", "filters": {}}, {"1": listing})
            page = output.read_text()

        self.assertIn("2025 band", page)
        self.assertIn("€ 5.848–6.683/m²", page)
        self.assertIn('class="band below"', page)
        self.assertIn("Observed history", page)
        self.assertIn("cell.colSpan = 14", page)
        self.assertIn('data-market-gone="1"', page)
        self.assertIn('<input type="checkbox" id="hideSold" checked>', page)
        self.assertIn("<th></th><th>Address</th><th>Status</th>", page)
        self.assertIn('<option value="viewing_requested">viewing requested</option>', page)
        self.assertIn('<option value="bought">bought 🎉</option>', page)
        self.assertIn("tracking-statuses.json", page)
        self.assertIn("trackingStatus === 'sold'", page)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            "status": "available",
            "price_history": [
                {"price": 500_000, "observed_at": "2026-08-01", "source": "funda"}
            ],
            "status_history": [
                {"status": "available", "observed_at": "2026-08-01", "source": "funda"}
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
        self.assertIn("cell.colSpan = 13", page)


if __name__ == "__main__":
    unittest.main()

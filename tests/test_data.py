import unittest
import json
from pathlib import Path
import tempfile
from contextlib import redirect_stdout
import io
from datetime import date
from unittest.mock import patch
import build


class DataTests(unittest.TestCase):
    def test_missing_is_not_zero(self):
        rows = build.validate([{"date":"2026-10-09","value":"."},
            {"date":"2026-10-08","value":"0"}], date(2026,10,12))
        self.assertEqual(rows, [{"date":"2026-10-08","value":0.0}])

    def test_reject_bad_values(self):
        for row in [{"date":"2026-10-13","value":1},
                    {"date":"2026-10-09","value":"nan"}]:
            with self.assertRaises(build.DataError):
                build.validate([row], date(2026,10,12))

    def test_spread_uses_common_dates_and_bp(self):
        records = {"a":{"observations":[{"date":"2026-10-08","value":4.5},
            {"date":"2026-10-09","value":4.6}], "fetched_at":"2026-10-12", "max_age_days":7},
            "b":{"observations":[{"date":"2026-10-09","value":3.6}],
            "fetched_at":"2026-10-12", "max_age_days":7}}
        fig, _ = build.make_figure({"title":"Test", "unit":"bp", "source":"Test",
            "spread":["a","b"], "multiplier":100}, records, date(2026,10,12))
        self.assertEqual(list(fig.data[0].x), ["2026-10-09"])
        self.assertAlmostEqual(fig.data[0].y[0], 100)
        self.assertTrue(any(a.text == "Grafik: Mehmet Faruk Bekir" for a in fig.layout.annotations))

    def test_network_error_cannot_leak_key(self):
        client = unittest.mock.Mock()
        client.get.side_effect = build.requests.RequestException("api_key=TOP_SECRET")
        with self.assertRaises(build.DataError) as raised:
            build.get_json(client, "https://example.org")
        self.assertNotIn("TOP_SECRET", str(raised.exception))

    def test_fred_parser(self):
        client = unittest.mock.Mock()
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {"count":2,"observations":[
            {"date":"2026-10-08","value":"3.8"},{"date":"2026-10-09","value":"."}]}
        with patch.dict(build.os.environ, {"FRED_API_KEY":"test-key"}):
            data = build.fetch_fred(client, {"code":"DGS2"}, "2020-01-01", date(2026,10,12))
        self.assertEqual(data, [{"date":"2026-10-08","value":3.8}])

    def test_complete_api_failure_preserves_cache_and_labels_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "state").mkdir()
            cache = {"us2y":{"provider":"fred","code":"DGS2",
                "fetched_at":"2026-10-01T10:00:00+03:00",
                "observations":[{"date":"2026-10-01","value":3.8}]}}
            (root / "state/cache.json").write_text(json.dumps(cache))
            cfg = {"start_date":"2020-01-01","logo_path":"assets/bfrc-logo.png",
                "series":[{"id":"us2y","provider":"fred","code":"DGS2",
                           "enabled":True,"max_age_days":7}],
                "charts":[{"id":"test","title":"Test","unit":"%","source":"FRED",
                           "series":[{"id":"us2y","label":"2Y"}]}]}
            with patch.object(build, "ROOT", root), patch.object(build, "CONFIG", cfg), \
                 patch.object(build, "fetch_fred", side_effect=build.DataError("API kesintisi")), \
                 redirect_stdout(io.StringIO()):
                build.build()
            html = (root / "dist/index.html").read_text()
            status = json.loads((root / "dist/status.json").read_text())
            self.assertIn("SON BAŞARILI VERİ", status["charts"][0]["status"])
            self.assertIn("API kesintisi", html)
            self.assertTrue((root / "dist/all_failed.flag").exists())
            self.assertEqual(json.loads((root / "state/cache.json").read_text()), cache)

    def test_wrong_series_cache_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "state").mkdir()
            (root / "state/cache.json").write_text(json.dumps({"us2y":{
                "provider":"fred","code":"SOME_OTHER_SERIES","observations":[]}}))
            with patch.object(build, "ROOT", root), \
                 patch.object(build, "fetch_fred", side_effect=build.DataError("API kesintisi")):
                with self.assertRaises(build.DataError):
                    build.build()


if __name__ == "__main__":
    unittest.main()

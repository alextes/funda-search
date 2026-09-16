import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import activity
import server


class ActivityTests(unittest.TestCase):
    def test_rotation_restart_redaction_and_bounded_results(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OPENAI_API_KEY': 'secret-value'}):
            path = Path(directory) / 'activity.jsonl'
            activity.configure(path)
            try:
                activity._handler.maxBytes = 350
                for n in range(12):
                    activity.log_print(f'fetch {n} failed: secret-value token=hidden <script>', file=io.StringIO())
                self.assertTrue(Path(str(path) + '.1').exists())
                activity.configure(path)
                events = activity.recent(2)
                self.assertEqual(len(events), 2)
                self.assertIn('fetch 11', events[0]['message'])
                self.assertEqual(events[0]['level'], 'error')
                for file in Path(directory).iterdir():
                    self.assertNotIn('secret-value', file.read_text())
                    self.assertNotIn('token=hidden', file.read_text())
            finally:
                activity._handler.close()
                activity._handler = activity._path = None

    def test_activity_requires_auth_and_returns_events(self):
        with patch.object(server, 'PASSWORD', 'test-password'), patch.object(activity, 'recent', return_value=[{'at':'2026-09-16T00:00:00+00:00','level':'info','message':'done'}]):
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            try:
                for path in ('/activity.json', '/activity.html'):
                    connection.request('GET', path)
                    response = connection.getresponse()
                    self.assertIn(b'type="password"', response.read())
                with patch.object(server.Handler, 'authed', return_value=True):
                    connection.request('GET', '/activity.json')
                    response = connection.getresponse()
                    self.assertEqual(json.loads(response.read())['entries'][0]['message'], 'done')
                    connection.request('GET', '/activity.html')
                    response = connection.getresponse()
                    body = response.read()
                    self.assertIn(b'message.textContent=e.message', body)
                    self.assertIn(b'Pause updates', body)
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join()


class RefreshReportingTests(unittest.TestCase):
    def test_unchanged_refresh_does_not_render_at_checkpoints(self):
        def refresh(listings, checkpoint):
            checkpoint({'checked': 25})
            return 0
        with patch.object(server.core, 'load_config', return_value={}), patch.object(server.core, 'load_listings', return_value={}), patch.object(server.core, 'ensure_histories', return_value=False), patch.object(server.core, 'ensure_districts', return_value=False), patch.object(server.core, 'save_listings') as save, patch.object(server.core, 'render') as render, patch.object(server.core, 'refresh_statuses', side_effect=refresh):
            server.status_refresh_once()
            self.assertEqual(save.call_count, 2)
            render.assert_not_called()

    def test_explicit_website_mode_skips_rejected_api(self):
        import fetch
        from unittest.mock import MagicMock
        with patch.object(fetch, 'Funda', return_value=MagicMock()), patch.object(fetch, 'search_pages') as api, patch.object(fetch, 'search_website', return_value=[]):
            self.assertEqual(fetch.fetch({'discovery_mode':'website'}, {}), (0, 0))
            api.assert_not_called()

"""Routing-contract regressions; synthetic observations, never provider calls."""
import copy
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tarmac import payloads
from tarmac.providers import codex, antigravity, claude
import test_service as service_tests
from test_service import make_reading, store

FUTURE = '2099-01-01T00:00:00Z'

class CodexBuckets(unittest.TestCase):
    def body(self):
        return {'rateLimits': {'limitId': 'codex', 'primary': {
            'usedPercent': 20, 'windowDurationMins': 300, 'resetsAt': 4070908800},
            'credits': {'balance': '12.5', 'unlimited': True}},
            'rateLimitResetCredits': {'availableCount': 3, 'credits': None}}

    def test_multi_bucket_is_authoritative_and_deduplicated(self):
        body = self.body()
        body['rateLimitsByLimitId'] = {'codex': copy.deepcopy(body['rateLimits']),
            'other': {'primary': {'usedPercent': 100, 'windowDurationMins': 10080,
                                  'resetsAt': 4070908800}}}
        body['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = 60
        got = codex.parse_codex_live(body)
        self.assertEqual([(w['id'], w['remaining']) for w in got.windows],
                         [('codex:primary', 40), ('other:primary', 0)])
        self.assertEqual(got.raw['credits']['count'], 3)
        self.assertEqual(got.raw['workspace_credits']['balance'], 12.5)
        self.assertNotIn('unlimited resets', json.dumps(got.raw))

    def test_count_only_and_capped_details(self):
        body = self.body()
        body['rateLimitResetCredits']['credits'] = [
            {'status': 'available', 'id': 'secret-reset-id', 'expiresAt': 4070908800}]
        r = codex.parse_codex_live(body)
        self.assertEqual(r.raw['credits']['count'], 3)
        self.assertIsNone(r.raw['credits']['expires_at'])
        self.assertNotIn('secret-reset-id', json.dumps(r.as_dict()))

    def test_invalid_window_cannot_disappear_behind_healthy_one(self):
        for used in (float('nan'), float('inf'), True, -2, 101):
            body = self.body()
            body['rateLimits']['secondary'] = {'usedPercent': used}
            r = codex.parse_codex_live(body)
            self.assertEqual(len(r.windows), 2)
            self.assertTrue(r.windows[1]['stale'])
            self.assertIsNone(r.windows[1]['remaining'])

class ProjectionRegressions(unittest.TestCase):
    setUp = service_tests.PayloadTests.setUp
    tearDown = service_tests.PayloadTests.tearDown
    _payload = service_tests.PayloadTests._payload
    def test_expired_window_invalidates_scalar_without_rewriting_history(self):
        row = make_reading(self.row['source_id'], remaining=35,
            windows=[{'label': '5h', 'remaining': 35, 'unit': 'percent',
                      'resets_at': '2020-01-01T00:00:00Z'}])
        store.record_reading(self.con, row)
        p = self._payload(with_history=True)
        self.assertEqual(p['status'], 'unknown')
        self.assertIsNone(p['latest']['remaining'])
        self.assertFalse(p['latest']['definitive'])
        self.assertEqual(p['history'][0]['remaining'], 35)

    def test_unknown_reset_cannot_supply_burn(self):
        a = {'definitive': True, 'observed_at': '2026-01-01T00:00:00Z',
             'windows': [{'id': 'x', 'unit': 'percent', 'remaining_percent': 60}]}
        b = copy.deepcopy(a); b['observed_at'] = '2026-01-01T01:00:00Z'
        b['windows'][0]['remaining_percent'] = 50
        self.assertIsNone(payloads.adjacent_burn(b, a))

    def test_non_finite_is_never_public_quota(self):
        for v in (float('inf'), float('nan'), True):
            self.assertIsNone(payloads._public_number(v))

    def test_per_window_burn_retains_identity_and_tolerates_countdown_precision(self):
        a = {'definitive': True, 'observed_at': '2026-01-01T00:00:00Z',
             'windows': [{'id': 'group:5h', 'unit': 'percent', 'remaining_percent': 60,
                         'resets_at': '2026-01-01T05:00:00Z', 'reset_precision_seconds': 60}]}
        b = copy.deepcopy(a); b['observed_at'] = '2026-01-01T01:00:00Z'
        b['windows'][0].update(remaining_percent=50, resets_at='2026-01-01T05:00:30Z')
        rates = payloads.window_burns(b, a)
        self.assertEqual(rates[0]['window_id'], 'group:5h')
        self.assertEqual(rates[0]['percent_per_hour'], 10)
        b['observed_at'] = '2026-01-01T05:00:01Z'
        self.assertEqual(payloads.window_burns(b, a), [])

class CachedMeasurements(unittest.TestCase):
    def test_agy_reuses_measurement_time_and_separates_config(self):
        with patch.object(antigravity, '_SCREEN_LAST', {}), \
             patch.object(antigravity, 'read_screen', return_value=[
                 {'label': '5h · gemini', 'remaining': 50, 'unit': 'percent',
                  'stale': False, 'resets_at': FUTURE}]) as read, \
             patch.object(antigravity, 'now', side_effect=['a', 'b', 'c']):
            a=antigravity.antigravity({'cwd': '/a'})
            b=antigravity.antigravity({'cwd': '/a'})
            c=antigravity.antigravity({'cwd': '/b'})
            self.assertEqual(a.as_of, b.as_of)
            self.assertEqual(c.as_of, 'c')
            self.assertEqual(read.call_count, 2)

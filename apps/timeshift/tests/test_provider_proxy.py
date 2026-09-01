"""Catchup reaches the provider over that account's configured proxy.

Both timeshift upstream paths run with the DB connection closed for
streaming, so the proxy has to arrive without a query: from the caller that
already holds the M3UAccount, or from the Redis pool entry.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.timeshift import views
from apps.timeshift.redis_keys import TimeshiftRedisKeys
from apps.timeshift.tests.test_views import (
    TEST_MEDIA_ID,
    TEST_SESSION_ID,
    _FakeRedis,
    _fake_upstream,
    _seed_pool_session,
)

PROXY = "socks5h://proxy.example:1080"
EXPECTED_PROXIES = {"http": PROXY, "https": PROXY}


class StreamFromProviderProxyTests(TestCase):
    """_stream_from_provider forwards the caller-resolved proxies verbatim."""

    def setUp(self):
        self.kwargs = dict(
            candidate_urls=["http://provider.test/timeshift.php?stream=1"],
            user_agent="test-agent",
            client_user_agent="test-client-agent",
            range_header=None,
            virtual_channel_id="1_2026-05-12-17-00_1",
            client_id="test123",
            client_ip="127.0.0.1",
            user=None,
            channel_display_name="Test",
            timestamp_utc="2026-05-12:17-00",
            channel_logo_id=None,
            m3u_profile_id=None,
            channel_id=1,
            channel_uuid="00000000-0000-0000-0000-000000000001",
            debug=False,
        )

    @patch.object(views, "_open_upstream")
    def test_account_proxies_passed_to_upstream(self, mocked_open):
        mocked_open.return_value = _fake_upstream(404)
        views._stream_from_provider(**self.kwargs, account_proxies=EXPECTED_PROXIES)
        self.assertEqual(mocked_open.call_args.kwargs["proxies"], EXPECTED_PROXIES)

    @patch.object(views, "_open_upstream")
    def test_unproxied_account_sends_none(self, mocked_open):
        mocked_open.return_value = _fake_upstream(404)
        views._stream_from_provider(**self.kwargs)
        self.assertIsNone(mocked_open.call_args.kwargs["proxies"])

    @patch.object(views, "_open_upstream")
    def test_every_candidate_in_the_cascade_uses_the_proxy(self, mocked_open):
        mocked_open.return_value = _fake_upstream(404)
        kwargs = dict(
            self.kwargs,
            candidate_urls=[
                "http://provider.test/a.php",
                "http://provider.test/b.php",
                "http://provider.test/c.ts",
            ],
        )
        views._stream_from_provider(**kwargs, account_proxies=EXPECTED_PROXIES)

        self.assertEqual(mocked_open.call_count, 3)
        for call in mocked_open.call_args_list:
            self.assertEqual(call.kwargs["proxies"], EXPECTED_PROXIES)


class EofProbeProxyTests(TestCase):
    """The near-EOF probe reconnects to the cached CDN through the proxy."""

    def setUp(self):
        self.redis = _FakeRedis()

    def _entry(self, proxy_url):
        archive_total = 870_621_184
        _seed_pool_session(self.redis, session_id=TEST_SESSION_ID)
        pool_key = TimeshiftRedisKeys.pool(TEST_SESSION_ID)
        self.redis.hset(pool_key, mapping={
            "final_url": "http://cdn.example.test/archive.ts",
            "content_length": str(archive_total),
            "presentation_byte_base": "0",
            "presentation_length": str(archive_total),
            "provider_user_agent": "provider-agent",
            "proxy_url": proxy_url,
            "busy": "1",
        })
        return self.redis.hgetall(pool_key), archive_total

    def _probe(self, proxy_url):
        entry, archive_total = self._entry(proxy_url)
        upstream = MagicMock()
        upstream.status_code = 416
        upstream.headers = {}
        upstream.close = MagicMock()

        with patch.object(views, "_open_upstream", return_value=upstream) as open_mock, \
             _patch_account_lookup() as account_get_mock:
            views._try_serve_busy_eof_probe(
                redis_client=self.redis,
                session_id=TEST_SESSION_ID,
                entry=entry,
                range_header=f"bytes={archive_total - 112_800}-",
                probe_length=None,
                debug=False,
            )

        # The probe must not fall back to a DB lookup for the account.
        account_get_mock.assert_not_called()
        return open_mock

    def test_probe_uses_pool_entry_proxy(self):
        open_mock = self._probe(PROXY)
        self.assertEqual(open_mock.call_args.kwargs["proxies"], EXPECTED_PROXIES)

    def test_probe_without_proxy_sends_none(self):
        open_mock = self._probe("")
        self.assertIsNone(open_mock.call_args.kwargs["proxies"])


class PoolSessionProxyPersistenceTests(TestCase):
    def test_create_pool_session_stores_proxy_url(self):
        redis = _FakeRedis()
        views._create_pool_session(
            redis,
            session_id=TEST_SESSION_ID,
            media_id=TEST_MEDIA_ID,
            user_id=5,
            client_ip="1.2.3.4",
            client_user_agent="test-agent",
            account_id=1,
            profile_id=31,
            stream_id="111",
            dispatcharr_stream_id=1,
            provider_timestamp="2026-06-08:19-00",
            proxy_url=PROXY,
        )
        entry = redis.hgetall(TimeshiftRedisKeys.pool(TEST_SESSION_ID))
        self.assertEqual(entry["proxy_url"], PROXY)

    def test_proxy_url_defaults_to_empty(self):
        redis = _FakeRedis()
        _seed_pool_session(redis, session_id=TEST_SESSION_ID)
        entry = redis.hgetall(TimeshiftRedisKeys.pool(TEST_SESSION_ID))
        self.assertEqual(entry["proxy_url"], "")


def _patch_account_lookup():
    return patch.object(views.M3UAccount.objects, "select_related")

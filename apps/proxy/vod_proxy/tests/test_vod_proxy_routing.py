"""VOD persistent connections carry the provider proxy across workers.

``RedisBackedVODConnection.get_stream()`` runs on whichever worker owns the
request and only has the Redis-serialized state to work from, so the account's
proxy has to survive the round trip through Redis.
"""

from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase

from apps.proxy.vod_proxy.multi_worker_connection_manager import (
    RedisBackedVODConnection,
    SerializableConnectionState,
)


def _state(proxy_url=None):
    return SerializableConnectionState(
        session_id="vod_session",
        stream_url="http://provider.example/movie.mkv",
        headers={"User-Agent": "UA/1.0"},
        m3u_profile_id=7,
        proxy_url=proxy_url,
    )


class SerializableConnectionStateProxyTests(SimpleTestCase):
    def test_proxy_url_survives_redis_round_trip(self):
        state = _state("socks5h://proxy.example:1080")
        restored = SerializableConnectionState.from_dict(state.to_dict())
        self.assertEqual(restored.proxy_url, "socks5h://proxy.example:1080")

    def test_absent_proxy_url_round_trips_as_none(self):
        restored = SerializableConnectionState.from_dict(_state().to_dict())
        self.assertIsNone(restored.proxy_url)

    def test_to_dict_never_emits_none(self):
        # _save_connection_state rejects None values before writing to Redis.
        self.assertEqual(_state().to_dict()["proxy_url"], "")

    def test_state_from_legacy_dict_without_proxy_url(self):
        # Sessions created before this feature have no proxy_url field in Redis.
        data = _state().to_dict()
        del data["proxy_url"]
        self.assertIsNone(SerializableConnectionState.from_dict(data).proxy_url)


class VodHeadProbeProxyTests(SimpleTestCase):
    """The HEAD range-probe reaches the provider over the account's proxy."""

    def setUp(self):
        self.factory = RequestFactory()

    def _run_head_vod(self, proxy_url):
        from apps.m3u.models import M3UAccount
        from apps.proxy.vod_proxy.views import head_vod

        movie = MagicMock()
        movie.name = "Test Movie"
        account = M3UAccount(name="Provider", proxy_url=proxy_url)

        provider_response = MagicMock()
        provider_response.status_code = 200
        provider_response.headers = {
            "Content-Length": "1234",
            "Content-Type": "video/mp4",
        }

        request = self.factory.head("/proxy/vod/movie/uuid/", HTTP_USER_AGENT="ua")

        with patch(
            "apps.proxy.vod_proxy.views.network_access_allowed", return_value=True
        ), patch(
            "core.models.CoreSettings.is_default_stream_profile_redirect",
            return_value=False,
        ), patch(
            "apps.proxy.vod_proxy.views._find_idle_vod_session", return_value=None
        ), patch(
            "apps.proxy.vod_proxy.views._select_vod_stream",
            return_value={
                "content_obj": movie,
                "m3u_account": account,
                "m3u_profile": MagicMock(),
                "current_connections": 0,
                "final_stream_url": "http://provider.example/movie.mp4",
            },
        ), patch(
            "apps.proxy.vod_proxy.views.MultiWorkerVODConnectionManager"
        ), patch(
            "apps.proxy.vod_proxy.views.requests.get", return_value=provider_response
        ) as mock_get:
            head_vod(request, content_type="movie", content_id="uuid")

        return mock_get

    def test_probe_uses_account_proxy(self):
        mock_get = self._run_head_vod("socks5h://proxy.example:1080")
        self.assertEqual(
            mock_get.call_args.kwargs["proxies"],
            {
                "http": "socks5h://proxy.example:1080",
                "https": "socks5h://proxy.example:1080",
            },
        )

    def test_probe_without_proxy_stays_direct(self):
        mock_get = self._run_head_vod(None)
        self.assertIsNone(mock_get.call_args.kwargs["proxies"])


class GetStreamProxyTests(SimpleTestCase):
    def _connection(self, state):
        connection = RedisBackedVODConnection("vod_session", redis_client=MagicMock())
        connection._get_connection_state = lambda: state
        connection._save_connection_state = lambda *a, **kw: True
        connection._acquire_lock = lambda *a, **kw: True
        connection._release_lock = lambda: None
        return connection

    def _run_get_stream(self, state):
        connection = self._connection(state)
        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-length": "100", "content-type": "video/mp4"}
        response.url = state.stream_url

        with patch("requests.Session") as session_cls:
            session = MagicMock()
            session.proxies = {}
            session.get.return_value = response
            session_cls.return_value = session
            connection.get_stream()

        return session

    def test_session_uses_state_proxy(self):
        session = self._run_get_stream(_state("socks5h://proxy.example:1080"))
        self.assertEqual(
            session.proxies,
            {
                "http": "socks5h://proxy.example:1080",
                "https": "socks5h://proxy.example:1080",
            },
        )

    def test_session_without_proxy_stays_direct(self):
        session = self._run_get_stream(_state())
        self.assertEqual(session.proxies, {})

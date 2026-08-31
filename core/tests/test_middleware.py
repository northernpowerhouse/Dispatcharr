from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve

from core.middleware import PortAccessControlMiddleware
from dispatcharr.utils import request_from_trusted_proxy


def _view(request):
    return None


class PortAccessControlMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.middleware = PortAccessControlMiddleware(get_response=lambda r: None)
        self.factory = RequestFactory()

    def _request(self, path, remote_addr="127.0.0.1", forwarded_port=None):
        request = self.factory.get(path, REMOTE_ADDR=remote_addr)
        if forwarded_port is not None:
            request.META["HTTP_X_FORWARDED_PORT"] = forwarded_port
        request.resolver_match = resolve(path)
        return request

    @override_settings(DISPATCHARR_CLIENT_PORT=None)
    def test_noop_when_client_port_feature_disabled(self):
        request = self._request("/api/", forwarded_port="9192")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_noop_when_forwarded_port_header_missing(self):
        request = self._request("/api/")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_noop_when_forwarded_port_does_not_match_client_port(self):
        request = self._request("/api/", forwarded_port="9191")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_untrusted_peer_cannot_spoof_client_port_header(self):
        request = self._request("/api/", remote_addr="203.0.113.5", forwarded_port="9192")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_blocks_api_on_client_port(self):
        request = self._request("/api/", forwarded_port="9192")
        response = self.middleware.process_view(request, _view, [], {})
        self.assertEqual(response.status_code, 403)

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_blocks_spa_root_on_client_port(self):
        request = self._request("/", forwarded_port="9192")
        response = self.middleware.process_view(request, _view, [], {})
        self.assertEqual(response.status_code, 403)

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_blocks_spa_catchall_on_client_port(self):
        request = self._request("/some/deep/spa/route", forwarded_port="9192")
        response = self.middleware.process_view(request, _view, [], {})
        self.assertEqual(response.status_code, 403)

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_blocks_django_admin_on_client_port(self):
        request = self._request("/admin/login/", forwarded_port="9192")
        response = self.middleware.process_view(request, _view, [], {})
        self.assertEqual(response.status_code, 403)

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_allows_output_namespace_on_client_port(self):
        request = self._request("/output/m3u", forwarded_port="9192")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_allows_hdhr_namespace_on_client_port(self):
        request = self._request("/hdhr/discover.json", forwarded_port="9192")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_allows_proxy_namespace_on_client_port(self):
        request = self._request("/proxy/stats/", forwarded_port="9192")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_allows_xc_player_api_on_client_port(self):
        request = self._request("/player_api.php", forwarded_port="9192")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_allows_unprefixed_xc_stream_route_on_client_port(self):
        request = self._request("/someuser/somepass/1", forwarded_port="9192")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT="9192")
    def test_allows_xc_username_literally_admin_on_client_port(self):
        # A username of "admin" must still resolve (and be allowed) as the
        # XC stream route, not get caught by anything admin-shaped — this
        # is the ambiguous-route edge case the port split has to get right.
        request = self._request("/admin/somepass/1", forwarded_port="9192")
        match = request.resolver_match
        self.assertEqual(match.view_name, "xc_stream_endpoint")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))

    @override_settings(DISPATCHARR_CLIENT_PORT=None)
    def test_admin_reachable_by_default_when_feature_disabled(self):
        request = self._request("/api/")
        self.assertIsNone(self.middleware.process_view(request, _view, [], {}))


class RequestFromTrustedProxyTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_default_trusts_loopback(self):
        request = self.factory.get("/", REMOTE_ADDR="127.0.0.1")
        self.assertTrue(request_from_trusted_proxy(request))

    def test_default_does_not_trust_public_peer(self):
        request = self.factory.get("/", REMOTE_ADDR="203.0.113.5")
        self.assertFalse(request_from_trusted_proxy(request))

    @patch.dict("os.environ", {"DISPATCHARR_TRUSTED_PROXIES": "203.0.113.0/24"})
    def test_narrowed_trusted_proxies_excludes_default_local_range(self):
        request = self.factory.get("/", REMOTE_ADDR="127.0.0.1")
        self.assertFalse(request_from_trusted_proxy(request))

        request = self.factory.get("/", REMOTE_ADDR="203.0.113.5")
        self.assertTrue(request_from_trusted_proxy(request))

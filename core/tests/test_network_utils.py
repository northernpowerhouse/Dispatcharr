"""Tests for per-provider proxy routing helpers."""

import os
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from apps.channels.models import Stream
from apps.m3u.models import M3UAccount
from core.network_utils import (
    build_account_session,
    get_account_proxies,
    get_stream_proxies,
    get_stream_subprocess_env,
    get_subprocess_env,
)


class GetAccountProxiesTests(SimpleTestCase):
    """proxy_url -> requests proxies dict, for every supported scheme."""

    def _account(self, proxy_url):
        return M3UAccount(name="Provider", proxy_url=proxy_url)

    def test_none_account_returns_none(self):
        self.assertIsNone(get_account_proxies(None))

    def test_unset_proxy_returns_none(self):
        self.assertIsNone(get_account_proxies(self._account(None)))

    def test_blank_proxy_returns_none(self):
        self.assertIsNone(get_account_proxies(self._account("")))

    def test_http_proxy(self):
        account = self._account("http://proxy.example:8080")
        self.assertEqual(
            get_account_proxies(account),
            {"http": "http://proxy.example:8080", "https": "http://proxy.example:8080"},
        )

    def test_http_proxy_with_credentials(self):
        url = "http://user:pass@proxy.example:8080"
        self.assertEqual(
            get_account_proxies(self._account(url)), {"http": url, "https": url}
        )

    def test_socks5_proxy(self):
        url = "socks5://proxy.example:1080"
        self.assertEqual(
            get_account_proxies(self._account(url)), {"http": url, "https": url}
        )

    def test_socks5h_proxy_with_credentials(self):
        # socks5h resolves DNS through the proxy - the recommended form for
        # geo-restricted providers.
        url = "socks5h://user:pass@proxy.example:1080"
        self.assertEqual(
            get_account_proxies(self._account(url)), {"http": url, "https": url}
        )


class ProxyUrlValidationTests(SimpleTestCase):
    def test_supported_schemes_accepted(self):
        for url in (
            "http://proxy.example:8080",
            "https://proxy.example:8443",
            "socks5://proxy.example:1080",
            "socks5h://user:pass@proxy.example:1080",
        ):
            with self.subTest(url=url):
                M3UAccount(name="Provider", proxy_url=url).clean()

    def test_unsupported_scheme_rejected(self):
        for url in ("ftp://proxy.example:21", "proxy.example:8080", "socks4://p:1080"):
            with self.subTest(url=url):
                with self.assertRaises(ValidationError):
                    M3UAccount(name="Provider", proxy_url=url).clean()

    def test_blank_proxy_url_is_valid(self):
        M3UAccount(name="Provider", proxy_url="").clean()
        M3UAccount(name="Provider", proxy_url=None).clean()


class BuildAccountSessionTests(SimpleTestCase):
    def test_session_without_proxy_has_no_proxies(self):
        session = build_account_session(M3UAccount(name="Provider"))
        self.assertEqual(session.proxies, {})

    def test_session_uses_account_proxy(self):
        account = M3UAccount(name="Provider", proxy_url="socks5h://proxy.example:1080")
        session = build_account_session(account)
        self.assertEqual(
            session.proxies,
            {
                "http": "socks5h://proxy.example:1080",
                "https": "socks5h://proxy.example:1080",
            },
        )

    def test_base_headers_are_applied(self):
        session = build_account_session(
            M3UAccount(name="Provider"), base_headers={"User-Agent": "Dispatcharr/1.0"}
        )
        self.assertEqual(session.headers["User-Agent"], "Dispatcharr/1.0")

    def test_none_account_yields_direct_session(self):
        self.assertEqual(build_account_session(None).proxies, {})


class GetSubprocessEnvTests(SimpleTestCase):
    """ffmpeg/streamlink/vlc inherit the account's proxy via env vars."""

    def test_proxy_vars_set_for_configured_account(self):
        account = M3UAccount(name="Provider", proxy_url="http://proxy.example:8080")
        env = get_subprocess_env(account)
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            self.assertEqual(env[key], "http://proxy.example:8080")

    def test_socks5_proxy_vars(self):
        account = M3UAccount(name="Provider", proxy_url="socks5h://proxy.example:1080")
        env = get_subprocess_env(account)
        self.assertEqual(env["http_proxy"], "socks5h://proxy.example:1080")

    def test_inherited_proxy_vars_stripped_when_account_has_no_proxy(self):
        # A proxy in the parent environment must not leak into a provider that
        # is meant to be reached directly.
        with patch.dict(
            os.environ,
            {"http_proxy": "http://inherited:8080", "HTTPS_PROXY": "http://inherited:8080"},
        ):
            env = get_subprocess_env(M3UAccount(name="Provider"))
            for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                self.assertNotIn(key, env)

    def test_other_env_vars_preserved(self):
        with patch.dict(os.environ, {"DISPATCHARR_TEST_MARKER": "kept"}):
            env = get_subprocess_env(M3UAccount(name="Provider"))
            self.assertEqual(env["DISPATCHARR_TEST_MARKER"], "kept")


class StreamProxyResolutionTests(TestCase):
    """Live proxy resolves the account from a Stream PK it already carries."""

    def setUp(self):
        self.proxied_account = M3UAccount.objects.create(
            name="Proxied provider",
            server_url="http://provider-a.example/list.m3u",
            proxy_url="socks5h://proxy-a.example:1080",
        )
        self.direct_account = M3UAccount.objects.create(
            name="Direct provider",
            server_url="http://provider-b.example/list.m3u",
        )
        self.proxied_stream = Stream.objects.create(
            name="Proxied stream",
            m3u_account=self.proxied_account,
            url="http://provider-a.example/live/1.ts",
        )
        self.direct_stream = Stream.objects.create(
            name="Direct stream",
            m3u_account=self.direct_account,
            url="http://provider-b.example/live/2.ts",
        )

    def test_resolves_proxies_for_stream_account(self):
        self.assertEqual(
            get_stream_proxies(self.proxied_stream.id),
            {
                "http": "socks5h://proxy-a.example:1080",
                "https": "socks5h://proxy-a.example:1080",
            },
        )

    def test_stream_on_unproxied_account_returns_none(self):
        self.assertIsNone(get_stream_proxies(self.direct_stream.id))

    def test_missing_stream_returns_none(self):
        self.assertIsNone(get_stream_proxies(None))
        self.assertIsNone(get_stream_proxies(0))
        self.assertIsNone(get_stream_proxies(self.proxied_stream.id + 10000))

    def test_subprocess_env_from_stream_id(self):
        env = get_stream_subprocess_env(self.proxied_stream.id)
        self.assertEqual(env["http_proxy"], "socks5h://proxy-a.example:1080")
        self.assertEqual(env["HTTPS_PROXY"], "socks5h://proxy-a.example:1080")

    def test_subprocess_env_for_unproxied_stream_strips_proxy_vars(self):
        with patch.dict(os.environ, {"http_proxy": "http://inherited:8080"}):
            env = get_stream_subprocess_env(self.direct_stream.id)
            self.assertNotIn("http_proxy", env)

    def test_unresolvable_stream_passes_environment_through(self):
        # Unknown provider is not the same as "configured to connect directly".
        with patch.dict(os.environ, {"http_proxy": "http://inherited:8080"}):
            env = get_stream_subprocess_env(None)
            self.assertEqual(env["http_proxy"], "http://inherited:8080")

    def test_two_accounts_route_independently(self):
        # The core use case: each provider gets its own egress.
        self.assertNotEqual(
            get_stream_proxies(self.proxied_stream.id),
            get_stream_proxies(self.direct_stream.id),
        )


class XtreamClientProxyTests(SimpleTestCase):
    """The XC API client honors the account proxy when one is passed."""

    def test_client_session_uses_account_proxy(self):
        from core.xtream_codes import Client

        account = M3UAccount(name="Provider", proxy_url="socks5h://proxy.example:1080")
        client = Client(
            "http://provider.example", "user", "pass", "UA/1.0", account=account
        )
        self.assertEqual(
            client.session.proxies,
            {
                "http": "socks5h://proxy.example:1080",
                "https": "socks5h://proxy.example:1080",
            },
        )
        self.assertEqual(client.session.headers["User-Agent"], "UA/1.0")

    def test_client_without_account_is_direct(self):
        from core.xtream_codes import Client

        client = Client("http://provider.example", "user", "pass", "UA/1.0")
        self.assertEqual(client.session.proxies, {})

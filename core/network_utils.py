"""Per-provider outbound proxy routing.

Central helpers so every outbound path Dispatcharr uses to reach a provider
(M3U fetch, Xtream API, native live proxy, VOD, and the ffmpeg/streamlink/vlc
subprocess path) honors the HTTP(S)/SOCKS5 proxy configured on the
M3UAccount that owns the request.
"""

import os

import requests

_PROXY_ENV_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


def get_account_proxies(account):
    """Return a requests-style proxies dict for ``account``, or None."""
    if account is None:
        return None
    return account.get_proxies_dict()


def build_account_session(account, base_headers=None):
    """Create a requests.Session routed through ``account``'s configured proxy, if any."""
    session = requests.Session()
    if base_headers:
        session.headers.update(base_headers)

    proxies = get_account_proxies(account)
    if proxies:
        session.proxies.update(proxies)

    return session


def get_subprocess_env(account):
    """Return an environment dict for ffmpeg/streamlink/vlc with proxy vars set from ``account``.

    Explicitly pops the proxy env keys when the account has no proxy configured,
    so a proxy set in the parent process environment is not accidentally
    inherited by a provider that should be reached directly.
    """
    env = dict(os.environ)
    proxy_url = getattr(account, "proxy_url", None) if account is not None else None

    if proxy_url:
        for key in _PROXY_ENV_KEYS:
            env[key] = proxy_url
    else:
        for key in _PROXY_ENV_KEYS:
            env.pop(key, None)

    return env


def _get_stream_account(stream_id):
    if not stream_id:
        return None

    from apps.channels.models import Stream

    stream = (
        Stream.objects.select_related("m3u_account")
        .filter(id=stream_id)
        .first()
    )
    return stream.m3u_account if stream else None


def get_stream_proxies(stream_id):
    """Resolve proxies dict for the M3UAccount that owns ``stream_id``.

    The live-proxy input layer (StreamManager/HTTPStreamReader) only carries a
    Stream primary key rather than a live M3UAccount instance, so it re-resolves
    the account here instead of threading the model instance through every
    constructor.
    """
    return get_account_proxies(_get_stream_account(stream_id))


def get_stream_subprocess_env(stream_id):
    """Same as get_subprocess_env(), but resolving the account from a Stream PK.

    When the account cannot be resolved at all (no stream row, or a stream with
    no account) the inherited environment is passed through untouched: that is
    an unknown provider, not one configured to connect directly.
    """
    account = _get_stream_account(stream_id)
    if account is None:
        return dict(os.environ)
    return get_subprocess_env(account)

import logging

from django.conf import settings
from django.http import HttpResponseForbidden

from dispatcharr.utils import request_from_trusted_proxy

logger = logging.getLogger(__name__)

# Whole-namespace allowlist: any view under these app namespaces is
# client-facing (output/M3U/EPG, HDHR discovery + device management, and the
# TS/VOD/catchup proxy tree).
CLIENT_VIEW_NAMESPACES = {"output", "hdhr", "proxy"}

# Individual view names for the Xtream-Codes compatibility routes and
# timeshift endpoints, which are registered directly on the root urlconf
# with no namespace (see dispatcharr/urls.py). This includes the unprefixed
# <username>/<password>/<channel_id> route, which shares its URL shape with
# the admin SPA's catch-all — resolving on resolver_match.view_name (set
# only after Django's URL resolver has already disambiguated the request)
# sidesteps that ambiguity instead of re-deriving it from the raw path.
CLIENT_VIEW_NAMES = {
    "xc_player_api",
    "xc_panel_api",
    "xc_get",
    "xc_xmltv",
    "xc_live_stream_endpoint",
    "xc_stream_endpoint",
    "timeshift_proxy",
    "timeshift_proxy_query",
    "stream_xc_movie",
    "stream_xc_episode",
}


class PortAccessControlMiddleware:
    """Restricts requests arriving on the client-only port to client-facing views.

    No-op unless DISPATCHARR_CLIENT_PORT is configured. A request is only
    ever treated as "client" role when X-Forwarded-Port matches that setting
    *and* it comes from a trusted reverse proxy (the same trust gate used by
    get_client_ip()) — an untrusted peer can't spoof its way onto the
    restricted allowlist just by sending the header itself. Any request that
    isn't positively identified as client-port traffic fails open to today's
    permissive (admin) behavior, so this middleware is inert for everyone
    until DISPATCHARR_CLIENT_PORT is opted into.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        client_port = settings.DISPATCHARR_CLIENT_PORT
        if not client_port:
            return None

        if not request_from_trusted_proxy(request):
            return None

        if request.META.get("HTTP_X_FORWARDED_PORT") != str(client_port):
            return None

        resolver_match = request.resolver_match
        namespace = resolver_match.namespace if resolver_match else None
        view_name = resolver_match.view_name if resolver_match else None

        if namespace in CLIENT_VIEW_NAMESPACES or view_name in CLIENT_VIEW_NAMES:
            return None

        logger.warning(
            "Blocked admin-only view %r on client port %s from %s",
            view_name,
            client_port,
            request.META.get("REMOTE_ADDR"),
        )
        return HttpResponseForbidden("Not available on this port.")

# Per-Provider Network Routing + Admin/Client Port Split


## Context

Dispatcharr currently routes all outbound provider traffic (M3U fetch, Xtream API,
live stream pulls, VOD) through whatever single network path the container has —
its README documents "centralizing VPN access" by running the whole container
through one VPN (e.g. Gluetun). Users with several M3U/Xtream accounts from
providers that each require a *different* geo-located VPN/egress currently have
no way to satisfy that per-account — everything shares one egress path. This plan
adds per-`M3UAccount` HTTP/SOCKS5 proxy support, applied consistently across
every outbound path Dispatcharr uses to reach a provider: the native Python
`requests` calls (M3U fetch, Xtream API, native live proxy, VOD) and the
ffmpeg/streamlink/vlc subprocess path (via injected proxy env vars).

Separately, Dispatcharr's admin UI, REST API, and the client-facing surfaces used
by IPTV players/HDHR devices/Xtream clients (`/output/`, `/hdhr/`, `/proxy/`,
`player_api.php` etc.) are all served through one nginx port (default 9191) onto
one Django/uWSGI backend. For security, users want to expose only the
client-facing streaming/API surface to less-trusted networks while keeping the
admin UI on a separate, more restrictable port. This plan adds an optional
second nginx listen port for client traffic, with Django-side defense-in-depth
enforcement, while keeping the existing single-port behavior as the default
(fully backward compatible — opt-in via a new env var).

Both features were scoped via deep exploration of the actual Dispatcharr source
(cloned to `/home/joe/projects/dev/dispatcharr`) plus DeepWiki. Confirmed via the
user: proxy support should cover **HTTP(S) + SOCKS5** (new `PySocks`
dependency). Network-interface binding was considered and **deliberately
dropped** from scope: a proxy applies at the `requests.Session`/adapter level
(or via `http_proxy` env vars for the ffmpeg subprocess path), which governs
*every* hop of a redirect chain, not just the entry URL — providers' nested
CDN/edge redirects transit the same proxy identically to the first request, so
proxy-based routing is not weaker than interface-binding against redirect-heavy
providers. Given that equivalence, interface-binding isn't worth its added
complexity and Docker-ergonomics cost (host networking or macvlan networks,
which make container port-tracking painful) — proxy-only covers the stated use
case fully.

---

## Part 1 — Per-Account Proxy Routing

### Model changes

`apps/m3u/models.py`, `M3UAccount` (currently has `server_url`, `username`,
`password`, `user_agent` FK, `custom_properties` JSON, no routing fields today).
Add one new nullable field + a migration:

- `proxy_url` — `CharField(max_length=500, blank=True, null=True)`. Accepts
  `http://[user:pass@]host:port` or `socks5://[user:pass@]host:port` (also
  `socks5h://` for remote DNS resolution through the proxy, which is what you
  want for geo-blocked providers so DNS doesn't leak the wrong location).

Add `M3UAccount.get_proxies_dict()` (returns `{"http": url, "https": url}` or
`None`). No network-interface field — dropped from scope (see Context): a proxy
already governs every hop of a redirect chain since it operates at the
session/adapter (or process-env) level below HTTP redirect-following, so it's
not weaker than interface binding against providers that redirect to CDN edges,
and it avoids the Docker host-networking/macvlan burden that binding would need.

### New dependency

Add `requests[socks]` (pulls in `PySocks`) to `pyproject.toml` next to the
existing `requests==2.34.2` pin.

### Central helper module

New `core/network_utils.py`:

- `get_account_proxies(account) -> dict | None` — thin wrapper around
  `account.get_proxies_dict()`.
- `build_account_session(account, base_headers=None) -> requests.Session` —
  creates a session and sets `.proxies` from `get_account_proxies`.
- `get_subprocess_env(account) -> dict` — returns `dict(os.environ)` with
  `http_proxy`/`https_proxy`/`HTTP_PROXY`/`HTTPS_PROXY` set (or explicitly
  unset via popped keys when not configured) from `account.proxy_url`, for
  injection into the ffmpeg/streamlink/vlc subprocess environment.

### Call-site changes (reuse the helper everywhere `requests` talks to a provider)

Five distinct call sites were traced; each already has (or can cheaply be given)
access to the `M3UAccount`:

1. **Xtream API client** — `core/xtream_codes.py`, `Client.__init__` (session
   created at line ~50). Add an optional `account=None` kwarg; when given, call
   `build_account_session` instead of a bare `requests.Session()`. Update the
   (small number of) call sites that construct `Client(...)` to pass the account.

2. **M3U playlist fetch** — `apps/m3u/tasks.py:205`, `fetch_m3u_lines()`. Already
   has `account` in scope; replace the module-level `requests.get(...)` call with
   `build_account_session(account).get(...)`.

3. **VOD HEAD/range probe** — `apps/proxy/vod_proxy/views.py:983`. `m3u_account`
   is already a local variable; swap `requests.get(...)` for a
   `build_account_session(m3u_account).get(...)` call. Simplest, no refactor.

4. **VOD persistent streaming** — `apps/proxy/vod_proxy/multi_worker_connection_manager.py`.
   `m3u_account` is in scope in `create_connection()` (~line 1020) but **not** in
   `RedisBackedVODConnection.get_stream()` (~line 461/484), which runs on
   whichever worker owns the request and only has the Redis-serialized
   `SerializableConnectionState` (`m3u_profile_id` int only today). Add
   `proxy_url` (or the account id) to
   `SerializableConnectionState.__init__`/`to_dict`/`from_dict`, populate it in
   `create_connection()` from `m3u_profile.m3u_account`, and build the session in
   `get_stream()` from the deserialized state via the same helper.

5. **Live proxy** — the widest change, touching two independent fetch
   mechanisms that both originate from `apps/proxy/live_proxy/url_utils.py:
   generate_stream_url()` (~line 129), the one place `m3u_account` is fully
   resolved before being flattened into plain url/user-agent strings:
   - *Native path*: `generate_stream_url()` → `views.py` →
     `server.py: initialize_channel()` → `StreamManager.__init__()`
     (`apps/proxy/live_proxy/input/manager.py`) → `_establish_http_connection()`
     → `HTTPStreamReader` (`apps/proxy/live_proxy/input/http_streamer.py`,
     `_read_stream`, where the real `requests.Session()` is created). Thread the
     resolved proxies dict (or just the account id to re-resolve) through each
     of these constructors/signatures, ending with `HTTPStreamReader` building
     its session via `build_account_session`. Note `StreamManager._create_session`
     (line 159) is dead code today — do not build on it, replace/remove it.
   - *Transcode (ffmpeg/streamlink/vlc) path*: `_establish_transcode_connection()`
     in `manager.py`, at the `os.posix_spawn(...)` call (~line 842), which today
     passes `_os.environ` unmodified as the child's environment. Change to
     `env = get_subprocess_env(account); ... os.posix_spawn(_executable,
     self.transcode_cmd, env, ...)`. Reuse the same account reference already
     threaded in for the native path above so this doesn't need a second DB
     lookup.

### Frontend

`frontend/src/components/forms/M3U.jsx` — add a `proxy_url` `TextInput` field
(placeholder text showing the `http://`/`socks5://` format), wire into the
form's `initialValues`, the `useEffect` that populates values when editing, and
`prepareSubmitValues` in `frontend/src/utils/forms/M3uUtils.js` so it's
included in create/update payloads. Follows the exact pattern already used for
`user_agent`/`max_streams` in that file.

### Recommended deployment pattern (README / in-app help text)

Point `proxy_url` at a per-provider VPN sidecar's proxy — Gluetun (already
Dispatcharr's documented "centralize VPN access" pattern) exposes both an HTTP
and a SOCKS5 proxy per container, so running one Gluetun instance per
geo-location and pointing each M3U/Xtream account's `proxy_url` at its
container gives full per-provider VPN isolation without any host-networking or
multi-NIC Docker configuration.

---

## Part 2 — Separate Admin Port from Client-Facing Port

### Current architecture (confirmed by reading `docker/nginx.conf`,
`dispatcharr/urls.py`, `docker/uwsgi.ini`, `docker/entrypoint.sh`,
`docker/init/03-init-dispatcharr.sh`)

One nginx `server` block listens on `NGINX_PORT` (templated from
`DISPATCHARR_PORT`, default 9191). Every path — the React admin SPA, `/api/`,
Django admin, `/hdhr/`, `/output/`, `/proxy/`, the unprefixed Xtream-Codes
compatibility routes (`player_api.php`, `get.php`, `xmltv.php`,
`live/<user>/<pass>/<channel>`, and critically the *unprefixed*
`<user>/<pass>/<channel_id>` route) — all proxy to the same `unix:/app/uwsgi.sock`.
`/ws/` proxies separately to Daphne on `127.0.0.1:8001`. There is only one
Django process; nginx is the only layer that currently knows about ports at all.

Key finding: **splitting does not require a second uWSGI/Daphne process.**
Both a new "client" nginx `server` block and the existing "admin" block can
proxy to the exact same uwsgi.sock — the split is enforced by which `location`s
each nginx block forwards, backed up by a Django-side check for defense in
depth (needed because the unprefixed Xtream route makes purely-regex nginx
matching fragile — see below).

### Design

**Opt-in, fully backward compatible.** New env var `DISPATCHARR_CLIENT_PORT`,
unset by default. When unset: behavior is unchanged, everything stays on
`DISPATCHARR_PORT` exactly as today. When set: nginx gains a second listener
that serves *only* client-facing paths; the admin/original port keeps serving
everything (so existing setups, and anyone who wants trusted admins to also
reach client endpoints on the admin port, keep working); the user is expected
to firewall/restrict network access to the admin port at the infra level
(Docker port publishing, host firewall, reverse proxy ACL) while exposing only
the client port externally — same pattern as the existing
`DISPATCHARR_TRUSTED_PROXIES` / network-access-CIDR features.

**nginx** (`docker/nginx.conf` + new `docker/nginx-client.conf.template`):
Add a second template file containing a `server { listen NGINX_CLIENT_PORT; ...}`
block with `location` entries only for `/hdhr`, `/output/`, `/proxy/`, the
Xtream php endpoints, and the live/movie/series/timeshift path patterns
(mirroring the location list already in `dispatcharr/urls.py`) — no `/api/`,
`/admin/`, `/ws/`, `/static/`, `/assets/`, or root `/`. All matched locations
proxy to the same `unix:/app/uwsgi.sock` used today.

**Templating** (`docker/init/03-init-dispatcharr.sh`): after the existing
`DISPATCHARR_PORT` → `NGINX_PORT` sed (lines ~59-64), add: if
`DISPATCHARR_CLIENT_PORT` is set and a valid integer, `sed` it into
`docker/nginx-client.conf.template` and copy the result into
`/etc/nginx/sites-enabled/`; otherwise skip entirely (don't create the file),
so nginx never tries to bind an unconfigured port. Reuse the existing IPv6
`listen [::]:` stripping logic (already regex-based, no changes needed) for the
new file too.

**Django defense-in-depth** — this is the important part, because nginx
`location` regexes would have to duplicate Django's own routing logic for the
ambiguous unprefixed `<user>/<pass>/<channel_id>` pattern (which shares URL
shape with the SPA catch-all `<path:unused_path>`), and getting that duplication
subtly wrong would either leak the admin SPA on the client port or 403
legitimate stream requests. Instead of matching by nginx path regex a second
time, add Dispatcharr's **first custom middleware**:

- New `core/middleware.py`, `PortAccessControlMiddleware`, implemented via
  `process_view` (not `process_request`) so it runs *after* Django's URL
  resolver has already disambiguated the request — it can check
  `resolver_match.view_name`/`resolver_match.func` against an explicit allowlist
  of client-facing views (`output:*`, `hdhr:*`, `proxy:*`, `xc_player_api`,
  `xc_panel_api`, `xc_get`, `xc_xmltv`, `xc_live_stream_endpoint`,
  `xc_stream_endpoint`, `timeshift_proxy`, `timeshift_proxy_query`,
  `stream_xc_movie`, `stream_xc_episode`) rather than re-deriving it from the
  raw path. This sidesteps the ambiguous-route problem entirely since Django has
  already resolved it correctly by this point.
- Role detection: read `request.META.get("HTTP_X_FORWARDED_PORT")` — nginx
  already sets this (`docker/nginx.conf:22`, `proxy_set_header X-Forwarded-Port
  $server_port`) — but only **trust** it when `REMOTE_ADDR` is in the existing
  trusted-proxy set (reuse the trusted-proxy gating already implemented for
  `get_client_ip()` in `dispatcharr/utils.py`, rather than trusting the header
  unconditionally). If the forwarded port equals `settings.DISPATCHARR_CLIENT_PORT`,
  role = `client` and the allowlist above is enforced (403 on anything else,
  e.g. `/api/`, `/admin/`, the SPA). Otherwise (untrusted source, feature
  disabled, or port doesn't match), role = `admin` — i.e. **fail open to
  today's permissive behavior**, so the middleware is a no-op unless a request
  is positively identified as arriving through the new client-only listener.
- Add to `MIDDLEWARE` in `dispatcharr/settings.py` (currently 8 stock
  Django/DRF/CORS entries, no custom middleware exists yet) near the top, after
  `SecurityMiddleware`.
- New setting: `settings.DISPATCHARR_CLIENT_PORT = os.environ.get("DISPATCHARR_CLIENT_PORT")`,
  following the existing raw-`os.environ.get` idiom used throughout
  `settings.py` (no `django-environ`).

**Frontend**: no changes needed. `frontend/src/api.js` makes relative
(same-origin) requests in production; since `/api/` and the SPA stay together
on the admin port by design, this keeps working untouched. `/ws/` also stays
admin-port-only (nginx's client-port block simply never proxies it), which is
correct since it's used for admin-UI real-time updates.

**Docker Compose / entrypoint plumbing**:
- `docker-compose.yml` and `docker-compose.aio.yml`: document the new optional
  `DISPATCHARR_CLIENT_PORT` env var (commented out by default, following the
  existing style used for `DISPATCHARR_TRUSTED_PROXIES`) plus a matching
  optional `ports:` mapping line.
- `docker/entrypoint.sh`: add `DISPATCHARR_CLIENT_PORT` to the *optional*
  env-propagation list (~line 197-199, alongside `DISPATCHARR_TRUSTED_PROXIES`),
  not the mandatory list, since absence means the feature stays off.

### URL generation correctness (verified, no change needed)

`core/utils.py: get_host_and_port()` / `build_absolute_uri_with_port()` —
used by `apps/output/views.py`, `apps/hdhr/api_views.py`, and
`apps/output/epg.py` to build the stream/playlist URLs embedded in M3U/HDHR/XC
responses — already derives host and port from the **incoming request's own**
`X-Forwarded-Host`/`X-Forwarded-Port`. As long as clients are pointed at the
client port for playlist/HDHR/XC generation *and* playback (which they will be,
since that's the only port serving those endpoints once configured), the
generated URLs automatically come out pointing at the client port with no
backend changes.

---

## Verification

**Part 1 (proxy):**
- Unit-test `core/network_utils.py`: `get_account_proxies` parsing for
  `http://`, `socks5://`, `socks5h://` with/without credentials.
- Manually configure two `M3UAccount`s in a dev instance with `proxy_url`
  pointed at two different local HTTP proxies (e.g. two `mitmproxy`/`tinyproxy`
  instances on different ports) and confirm (via the proxy's own logs) that
  M3U refresh, Xtream login, and live stream playback for each account transits
  its assigned proxy — for both a native "Proxy" `StreamProfile` channel and an
  ffmpeg-transcode `StreamProfile` channel, to confirm the env-var injection
  path works for the subprocess case too.
- Confirm a redirect-heavy test stream (302 to a different host) still
  transits the configured proxy for the redirected request, not just the
  initial one.

**Part 2 (port split):**
- `docker compose up` with `DISPATCHARR_CLIENT_PORT` unset: confirm nginx
  config is unchanged (single listener) and all existing behavior/tests pass —
  regression check for backward compatibility.
- Set `DISPATCHARR_CLIENT_PORT=9192`, rebuild/restart: confirm
  `curl http://localhost:9192/hdhr/discover.json`,
  `.../output/m3u`, and an Xtream `player_api.php` request succeed, while
  `curl http://localhost:9192/api/` and `curl http://localhost:9192/` (SPA)
  both return 403; confirm the admin port (9191) still serves everything as
  before.
- Specifically test the ambiguous route: an Xtream account with username
  literally `admin` hitting `/admin/<password>/<channel_id>` on the client
  port should stream correctly (not get caught by any admin-path exclusion).
- Confirm `/ws/` is unreachable on the client port and admin UI real-time
  updates still work when accessed via the admin port.

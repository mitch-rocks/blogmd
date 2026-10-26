"""Small pure-ASGI middlewares (cheap: no request/response object per call)."""
from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, PlainTextResponse


class ProxyHeadersMiddleware:
    """
    Behind N trusted reverse proxies, take the client address and scheme from the Nth-from-right
    X-Forwarded-For / X-Forwarded-Proto entry (same rule as werkzeug's ProxyFix). Optionally
    prefer a header the edge sets itself (e.g. CF-Connecting-IP) for the client address.
    """

    def __init__(self, app, trusted_hops: int = 0, real_ip_header: str | None = None):
        self.app, self.hops = app, trusted_hops
        self.real_ip = real_ip_header.lower().encode() if real_ip_header else None

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket') and (self.hops or self.real_ip):
            headers = dict(scope['headers'])
            client = scope.get('client') or ('', 0)
            if self.hops:
                xff = [p.strip() for p in headers.get(b'x-forwarded-for', b'').decode('latin1').split(',') if p.strip()]
                if len(xff) >= self.hops:
                    client = (xff[-self.hops], 0)
                proto = [p.strip() for p in headers.get(b'x-forwarded-proto', b'').decode('latin1').split(',') if p.strip()]
                if len(proto) >= self.hops and proto[-self.hops] in ('http', 'https'):
                    scope['scheme'] = proto[-self.hops]
            if self.real_ip and headers.get(self.real_ip):
                client = (headers[self.real_ip].decode('latin1').split(',')[0].strip()[:64], 0)
            scope['client'] = client
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Reject request bodies over `max_bytes` (Content-Length check + streamed count)."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        for k, v in scope['headers']:
            if k == b'content-length' and v.isdigit() and int(v) > self.max:
                return await self._too_large(scope, receive, send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg['type'] == 'http.request':
                seen += len(msg.get('body', b''))
                if seen > self.max:
                    raise _TooLarge()
            return msg

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await self._too_large(scope, receive, send)

    async def _too_large(self, scope, receive, send):
        mb = self.max // (1024 * 1024)
        if scope['path'].startswith('/write/api/'):
            resp = JSONResponse({'error': f'Upload is larger than {mb} MB.'}, status_code=413)
        else:
            resp = PlainTextResponse(f'Request body larger than {mb} MB.', status_code=413)
        await resp(scope, receive, send)


class _TooLarge(Exception):
    pass


class SecurityHeadersMiddleware:
    DEFAULTS = [(b'x-content-type-options', b'nosniff'),
                (b'referrer-policy', b'strict-origin-when-cross-origin'),
                (b'x-frame-options', b'SAMEORIGIN')]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                for k, v in self.DEFAULTS:
                    if k.decode() not in headers:
                        headers.append(k.decode(), v.decode())
            await send(message)

        await self.app(scope, receive, send_with_headers)


class LazySessionMiddleware:
    """
    Starlette's SessionMiddleware needs the signing key up front, but ours lives in the
    database (generated on first start), which is only opened in the app lifespan.
    This builds the real middleware on the first HTTP request.
    """

    def __init__(self, app, get_settings):
        self.app, self.get_settings, self.inner = app, get_settings, None

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        if self.inner is None:
            self.inner = SessionMiddleware(self.app, **self.get_settings())
        await self.inner(scope, receive, send)

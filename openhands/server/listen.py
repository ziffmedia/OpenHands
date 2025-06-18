import os

import socketio
from starlette.middleware import Middleware

from openhands.server.app import app as base_app
from openhands.server.listen_socket import sio
from openhands.server.middleware import (
    AttachConversationMiddleware,
    CacheControlMiddleware,
    InMemoryRateLimiter,
    LocalhostCORSMiddleware,
    RateLimitMiddleware,
)
from openhands.server.static import SPAStaticFiles

if os.getenv('SERVE_FRONTEND', 'true').lower() == 'true':
    base_app.mount(
        '/', SPAStaticFiles(directory='./frontend/build', html=True), name='dist'
    )

# Use the middleware stack directly to avoid BaseHTTPMiddleware wrapping
base_app.middleware_stack = base_app.build_middleware_stack()

# Add middleware directly to avoid Starlette's automatic wrapping
base_app.user_middleware = [
    Middleware(LocalhostCORSMiddleware),
    Middleware(CacheControlMiddleware),
    Middleware(RateLimitMiddleware, rate_limiter=InMemoryRateLimiter(requests=10, seconds=1)),
    Middleware(AttachConversationMiddleware),
] + base_app.user_middleware

app = socketio.ASGIApp(sio, other_asgi_app=base_app)

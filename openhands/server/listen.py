import os

import socketio
from fastapi.middleware.cors import CORSMiddleware

from openhands.server.app import app as base_app
from openhands.server.listen_socket import sio
from openhands.server.middleware import (
    AttachConversationMiddleware,
    CacheControlMiddleware,
    InMemoryRateLimiter,
    RateLimitMiddleware,
)
from openhands.server.static import SPAStaticFiles

if os.getenv('SERVE_FRONTEND', 'true').lower() == 'true':
    base_app.mount(
        '/', SPAStaticFiles(directory='./frontend/build', html=True), name='dist'
    )

# Configure CORS origins - allow localhost and any configured origins
allowed_origins = []

# Allow localhost/127.0.0.1 with common development ports
for host in ['localhost', '127.0.0.1']:
    for port in [3000, 3001, 8000, 8080]:
        allowed_origins.extend([
            f'http://{host}:{port}',
            f'https://{host}:{port}',
        ])

# Add any additional configured origins from environment
cors_origins_env = os.getenv('PERMITTED_CORS_ORIGINS')
if cors_origins_env:
    additional_origins = [origin.strip() for origin in cors_origins_env.split(',')]
    allowed_origins.extend(additional_origins)

# Use FastAPI's built-in CORS middleware instead of custom one
# to ensure better compatibility with socketio.ASGIApp
base_app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
)
base_app.add_middleware(CacheControlMiddleware)
base_app.add_middleware(
    RateLimitMiddleware,
    rate_limiter=InMemoryRateLimiter(requests=10, seconds=1),
)
base_app.add_middleware(AttachConversationMiddleware)

app = socketio.ASGIApp(sio, other_asgi_app=base_app)

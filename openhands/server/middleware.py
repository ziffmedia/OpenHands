import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from openhands.server import shared
from openhands.server.types import SessionMiddlewareInterface
from openhands.server.user_auth import get_user_id


class LocalhostCORSMiddleware(CORSMiddleware):
    """
    Custom CORS middleware that allows any request from localhost/127.0.0.1 domains,
    while using standard CORS rules for other origins.
    """

    def __init__(self, app: ASGIApp) -> None:
        allow_origins_str = os.getenv('PERMITTED_CORS_ORIGINS')
        if allow_origins_str:
            allow_origins = tuple(
                origin.strip() for origin in allow_origins_str.split(',')
            )
        else:
            allow_origins = ()
        super().__init__(
            app,
            allow_origins=allow_origins,
            allow_credentials=True,
            allow_methods=['*'],
            allow_headers=['*'],
        )

    def is_allowed_origin(self, origin: str) -> bool:
        if origin and not self.allow_origins and not self.allow_origin_regex:
            parsed = urlparse(origin)
            hostname = parsed.hostname or ''

            # Allow any localhost/127.0.0.1 origin regardless of port
            if hostname in ['localhost', '127.0.0.1']:
                return True

        return super().is_allowed_origin(origin)


class CacheControlMiddleware(BaseHTTPMiddleware):
    """
    Middleware to disable caching for all routes by adding appropriate headers
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith('/assets'):
            # The content of the assets directory has fingerprinted file names so we cache aggressively
            response.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
        else:
            response.headers['Cache-Control'] = (
                'no-cache, no-store, must-revalidate, max-age=0'
            )
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response


class InMemoryRateLimiter:
    def __init__(self, requests: int = 10, seconds: int = 1, sleep_seconds: int = 0):
        self.requests = requests
        self.seconds = seconds
        self.sleep_seconds = sleep_seconds
        self.history: dict[str, list[datetime]] = defaultdict(list)

    def _get_key(self, request: Request) -> str:
        if hasattr(request, 'client') and request.client:
            return request.client.host
        return 'unknown'

    async def __call__(self, request: Request) -> bool:
        key = self._get_key(request)
        now = datetime.now()

        # Remove old entries
        cutoff = now - timedelta(seconds=self.seconds)
        self.history[key] = [
            timestamp for timestamp in self.history[key] if timestamp > cutoff
        ]

        self.history[key].append(now)

        if len(self.history[key]) > self.requests * 2:
            return False
        elif len(self.history[key]) > self.requests:
            if self.sleep_seconds > 0:
                await asyncio.sleep(self.sleep_seconds)
                return True
            else:
                return False

        return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, rate_limiter: InMemoryRateLimiter):
        super().__init__(app)
        self.rate_limiter = rate_limiter

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not self.is_rate_limited_request(request):
            return await call_next(request)
        ok = await self.rate_limiter(request)
        if not ok:
            return JSONResponse(
                status_code=429,
                content={'message': 'Too many requests'},
                headers={'Retry-After': '1'},
            )
        return await call_next(request)

    def is_rate_limited_request(self, request: StarletteRequest) -> bool:
        if request.url.path.startswith('/assets'):
            return False
        # Put Other non rate limited checks here
        return True


class AttachConversationMiddleware(SessionMiddlewareInterface):
    """
    Raw ASGI middleware implementation to avoid ASGI protocol issues
    that can occur with BaseHTTPMiddleware when handling complex response flows.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def _should_attach(self, scope: Scope) -> tuple[bool, str]:
        """
        Determine if the middleware should attach a session for the given request.
        Returns (should_attach, conversation_id)
        """
        if scope["type"] != "http":
            return False, ""

        method = scope.get("method", "")
        if method == "OPTIONS":
            return False, ""

        path = scope.get("path", "")
        conversation_id = ""

        if path.startswith('/api/conversation'):
            path_parts = path.split('/')
            if len(path_parts) > 4:
                conversation_id = path_parts[3]

        if not conversation_id:
            return False, ""

        return True, conversation_id

    async def _attach_conversation(self, scope: Scope, conversation_id: str) -> tuple[bool, dict]:
        """
        Attach the user's session based on the provided authentication token.
        Returns (success, error_response_dict)
        """
        try:
            # Create a minimal request object for user_id extraction
            request = Request(scope)
            user_id = await get_user_id(request)

            conversation = await shared.conversation_manager.attach_to_conversation(
                conversation_id, user_id
            )

            if not conversation:
                return False, {
                    'status_code': status.HTTP_404_NOT_FOUND,
                    'content': {'error': 'Session not found'}
                }

            scope['conversation'] = conversation
            scope['conversation_id'] = conversation_id
            return True, {}

        except Exception as e:
            return False, {
                'status_code': status.HTTP_500_INTERNAL_SERVER_ERROR,
                'content': {'error': f'Failed to attach conversation: {str(e)}'}
            }

    async def _detach_conversation(self, scope: Scope) -> None:
        """
        Detach the user's session.
        """
        try:
            conversation = scope.get('conversation')
            if conversation:
                await shared.conversation_manager.detach_from_conversation(conversation)
        except Exception:
            # Silently ignore detachment errors to avoid secondary failures
            pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        should_attach, conversation_id = self._should_attach(scope)

        if not should_attach:
            await self.app(scope, receive, send)
            return

        success, error_response = await self._attach_conversation(scope, conversation_id)

        if not success:
            # Send error response directly
            response = JSONResponse(**error_response)
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except Exception:
            await self._detach_conversation(scope)
            raise
        finally:
            await self._detach_conversation(scope)

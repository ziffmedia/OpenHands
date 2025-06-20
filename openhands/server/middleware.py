import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from openhands.server import shared
from openhands.server.types import SessionMiddlewareInterface
from openhands.server.user_auth import get_user_id


class LocalhostCORSMiddleware:
    """
    Raw ASGI CORS middleware that allows any request from localhost/127.0.0.1 domains,
    while using standard CORS rules for other origins.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        allow_origins_str = os.getenv('PERMITTED_CORS_ORIGINS')
        if allow_origins_str:
            self.allow_origins = tuple(
                origin.strip() for origin in allow_origins_str.split(',')
            )
        else:
            self.allow_origins = ()

    def is_allowed_origin(self, origin: str | None) -> bool:
        if origin and not self.allow_origins:
            parsed = urlparse(origin)
            hostname = parsed.hostname or ''

            # Allow any localhost/127.0.0.1 origin regardless of port
            if hostname in ['localhost', '127.0.0.1']:
                return True

        if origin and self.allow_origins:
            return origin in self.allow_origins

        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        headers_dict = dict(scope.get("headers", []))
        origin = headers_dict.get(b"origin", b"").decode("latin1")

        # Handle preflight requests
        if method == "OPTIONS":
            if origin and self.is_allowed_origin(origin):
                response_headers = [
                    (b"access-control-allow-origin", origin.encode("latin1")),
                    (b"access-control-allow-credentials", b"true"),
                    (b"access-control-allow-methods", b"*"),
                    (b"access-control-allow-headers", b"*"),
                    (b"access-control-max-age", b"86400"),
                ]
            else:
                response_headers = []

            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": response_headers,
            })
            await send({
                "type": "http.response.body",
                "body": b"",
            })
            return

        # Handle regular requests
        async def send_wrapper(message):
            # Only modify headers for response start messages
            if message["type"] == "http.response.start" and origin and self.is_allowed_origin(origin):
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"access-control-allow-origin", origin.encode("latin1")),
                    (b"access-control-allow-credentials", b"true"),
                ])
                message = {**message, "headers": headers}

            await send(message)

        await self.app(scope, receive, send_wrapper)


class CacheControlMiddleware:
    """
    Raw ASGI middleware to disable caching for all routes by adding appropriate headers
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        async def send_wrapper(message):
            # Only modify headers for response start messages
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))

                if path.startswith('/assets'):
                    # The content of the assets directory has fingerprinted file names so we cache aggressively
                    headers.append((b'cache-control', b'public, max-age=2592000, immutable'))
                else:
                    headers.extend([
                        (b'cache-control', b'no-cache, no-store, must-revalidate, max-age=0'),
                        (b'pragma', b'no-cache'),
                        (b'expires', b'0'),
                    ])

                message = {**message, "headers": headers}

            await send(message)

        await self.app(scope, receive, send_wrapper)


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


class RateLimitMiddleware:
    """
    Raw ASGI middleware for rate limiting
    """

    def __init__(self, app: ASGIApp, rate_limiter: InMemoryRateLimiter):
        self.app = app
        self.rate_limiter = rate_limiter

    def is_rate_limited_request(self, path: str) -> bool:
        if path.startswith('/assets'):
            return False
        # Put Other non rate limited checks here
        return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self.is_rate_limited_request(path):
            await self.app(scope, receive, send)
            return

        # Create a minimal request object for rate limiting
        request = Request(scope, receive)
        ok = await self.rate_limiter(request)

        if not ok:
            response = JSONResponse(
                status_code=429,
                content={'message': 'Too many requests'},
                headers={'Retry-After': '1'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


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

            # For DockerNestedConversationManager and KubernetesNestedConversationManager, 
            # attach_to_conversation is not supported
            # Instead, we need to check if the conversation exists without attaching
            from openhands.server.conversation_manager.docker_nested_conversation_manager import DockerNestedConversationManager
            from openhands.server.conversation_manager.kubernetes_nested_conversation_manager import KubernetesNestedConversationManager

            if isinstance(shared.conversation_manager, (DockerNestedConversationManager, KubernetesNestedConversationManager)):
                # For nested conversation managers, we need to ensure the container/pod is running
                # when a conversation is opened
                from openhands.core.config import OpenHandsConfig

                try:
                    # We need some basic settings to start the agent loop
                    # Create minimal settings to start the conversation
                    from openhands.storage.data_models.settings import Settings
                    config = shared.config if hasattr(shared, 'config') else OpenHandsConfig()
                    settings = Settings(agent=config.default_agent)

                    await shared.conversation_manager.maybe_start_agent_loop(
                        conversation_id, settings, user_id
                    )
                except Exception as e:
                    # If we can't start the agent loop, log but don't fail the request
                    # The user might be trying to access a conversation that doesn't exist yet
                    pass
            else:
                conversation = await shared.conversation_manager.attach_to_conversation(
                    conversation_id, user_id
                )

                if not conversation:
                    return False, {
                        'status_code': status.HTTP_404_NOT_FOUND,
                        'content': {'error': 'Session not found'}
                    }

                # Initialize state if it doesn't exist
                if 'state' not in scope:
                    scope['state'] = {}

                # Store conversation in scope state so it can be accessed via request.state.conversation
                scope['state']['conversation'] = conversation
                scope['state']['conversation_id'] = conversation_id

                # Also store in scope for backwards compatibility
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
            # Try to get conversation from either location
            conversation = scope.get('conversation') or (scope.get('state', {}).get('conversation') if scope.get('state') else None)
            if conversation:
                from openhands.server.conversation_manager.docker_nested_conversation_manager import DockerNestedConversationManager
                from openhands.server.conversation_manager.kubernetes_nested_conversation_manager import KubernetesNestedConversationManager

                if isinstance(shared.conversation_manager, (DockerNestedConversationManager, KubernetesNestedConversationManager)):
                    # Skip detachment for nested conversation managers
                    # The container/pod lifecycle is managed separately
                    pass
                else:
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

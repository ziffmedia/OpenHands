import json
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from openhands.core.logger import openhands_logger as logger
from openhands.utils.http_session import HttpSession
from openhands.utils.tenacity_stop import stop_if_should_exit


class RequestHTTPError(httpx.HTTPStatusError):
    """Exception raised when an error occurs in a request with details."""

    def __init__(self, *args: Any, detail: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.detail = detail

    def __str__(self) -> str:
        s = super().__str__()
        if self.detail is not None:
            s += f'\nDetails: {self.detail}'
        return str(s)


def is_retryable_error(exception: Any) -> bool:
    # Retry on rate limiting (429) and server errors (5xx)
    if isinstance(exception, (httpx.HTTPStatusError, RequestHTTPError)):
        if hasattr(exception, 'response') and exception.response:
            status_code = exception.response.status_code
            is_retryable = status_code == 429 or (500 <= status_code < 600)
            if is_retryable:
                logger.warning(f"Retryable error {status_code} detected, will retry: {exception}")
            return is_retryable
    return False


@retry(
    retry=retry_if_exception(is_retryable_error),
    stop=stop_after_attempt(5) | stop_if_should_exit(),
    wait=wait_exponential(multiplier=1, min=2, max=30),
)
def send_request(
    session: HttpSession,
    method: str,
    url: str,
    timeout: int = 10,
    **kwargs: Any,
) -> httpx.Response:
    response = session.request(method, url, timeout=timeout, **kwargs)
    try:
        response.raise_for_status()
    except httpx.HTTPError as e:
        try:
            _json = response.json()
        except json.decoder.JSONDecodeError:
            _json = None
        finally:
            response.close()
        raise RequestHTTPError(
            e,
            request=e.request,
            response=e.response,
            detail=_json.get('detail') if _json is not None else None,
        ) from e
    return response

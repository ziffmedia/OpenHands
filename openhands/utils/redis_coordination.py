"""Redis-based coordination utilities for OpenHands multi-replica deployments."""

import asyncio
import json
import os
import time
from typing import Any, Optional

from openhands.core.logger import openhands_logger as logger

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    logger.warning("Redis library not available. Multi-replica coordination disabled.")
    REDIS_AVAILABLE = False


class RedisCoordinator:
    """
    Redis-based coordinator for multi-replica deployments.

    Provides distributed locking and state coordination for resources like
    Kubernetes pods that should only be managed by one replica at a time.
    """

    def __init__(self, redis_url: Optional[str] = None, password: Optional[str] = None):
        self.redis_url = redis_url or f"redis://{os.environ.get('REDIS_HOST', 'localhost:6379')}"
        self.password = password or os.environ.get('REDIS_PASSWORD')
        self.redis_client: Optional[redis.Redis] = None
        self.enabled = REDIS_AVAILABLE and bool(os.environ.get('REDIS_HOST'))

        if not self.enabled:
            logger.warning("Redis coordination disabled - Redis not available or REDIS_HOST not set")

    async def connect(self) -> bool:
        """Connect to Redis. Returns True if successful, False otherwise."""
        if not self.enabled:
            return False

        try:
            self.redis_client = redis.from_url(
                self.redis_url,
                password=self.password,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True,
                retry_on_error=[ConnectionError, TimeoutError],
                retry=redis.Retry(redis.backoff.ExponentialBackoff(), 3)
            )

            # Test connection
            await self.redis_client.ping()
            logger.info("Successfully connected to Redis for coordination")
            return True

        except Exception as e:
            logger.warning(f"Failed to connect to Redis: {e}. Coordination disabled.")
            self.enabled = False
            self.redis_client = None
            return False

    async def close(self):
        """Close Redis connection."""
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None

    async def acquire_lock(self, key: str, timeout: int = 300, retry_interval: float = 0.1) -> bool:
        """
        Acquire a distributed lock with timeout.

        Args:
            key: Lock key
            timeout: Lock timeout in seconds
            retry_interval: Time to wait between retry attempts

        Returns:
            True if lock acquired, False otherwise
        """
        if not self.enabled or not self.redis_client:
            logger.warning("Redis not available, skipping lock acquisition")
            return True  # Fallback to allowing operation

        try:
            lock_key = f"openhands:lock:{key}"
            expiry_time = int(time.time()) + timeout

            # Try to acquire lock
            result = await self.redis_client.set(
                lock_key,
                expiry_time,
                nx=True,  # Only set if key doesn't exist
                ex=timeout  # Set expiry
            )

            if result:
                logger.debug(f"Acquired lock for {key}")
                return True
            else:
                logger.debug(f"Failed to acquire lock for {key} - already held")
                return False

        except Exception as e:
            logger.warning(f"Error acquiring lock for {key}: {e}. Allowing operation to proceed.")
            return True  # Fallback to allowing operation

    async def release_lock(self, key: str) -> bool:
        """
        Release a distributed lock.

        Args:
            key: Lock key

        Returns:
            True if lock released, False otherwise
        """
        if not self.enabled or not self.redis_client:
            return True

        try:
            lock_key = f"openhands:lock:{key}"
            result = await self.redis_client.delete(lock_key)
            if result:
                logger.debug(f"Released lock for {key}")
            return bool(result)

        except Exception as e:
            logger.warning(f"Error releasing lock for {key}: {e}")
            return False

    async def set_resource_state(self, key: str, state: dict, ttl: int = 3600) -> bool:
        """
        Set resource state in Redis.

        Args:
            key: Resource key
            state: State dictionary to store
            ttl: Time to live in seconds

        Returns:
            True if successful, False otherwise
        """
        if not self.enabled or not self.redis_client:
            return False

        try:
            resource_key = f"openhands:resource:{key}"
            await self.redis_client.setex(
                resource_key,
                ttl,
                json.dumps(state)
            )
            logger.debug(f"Set state for resource {key}")
            return True

        except Exception as e:
            logger.warning(f"Error setting state for {key}: {e}")
            return False

    async def get_resource_state(self, key: str) -> Optional[dict]:
        """
        Get resource state from Redis.

        Args:
            key: Resource key

        Returns:
            State dictionary if found, None otherwise
        """
        if not self.enabled or not self.redis_client:
            return None

        try:
            resource_key = f"openhands:resource:{key}"
            data = await self.redis_client.get(resource_key)
            if data:
                return json.loads(data)
            return None

        except Exception as e:
            logger.warning(f"Error getting state for {key}: {e}")
            return None

    async def delete_resource_state(self, key: str) -> bool:
        """
        Delete resource state from Redis.

        Args:
            key: Resource key

        Returns:
            True if deleted, False otherwise
        """
        if not self.enabled or not self.redis_client:
            return False

        try:
            resource_key = f"openhands:resource:{key}"
            result = await self.redis_client.delete(resource_key)
            if result:
                logger.debug(f"Deleted state for resource {key}")
            return bool(result)

        except Exception as e:
            logger.warning(f"Error deleting state for {key}: {e}")
            return False

    async def increment_counter(self, key: str, increment: int = 1, ttl: int = 3600) -> int:
        """
        Increment a counter in Redis.

        Args:
            key: Counter key
            increment: Amount to increment by
            ttl: Time to live in seconds for new counters

        Returns:
            New counter value, or 0 if Redis unavailable
        """
        if not self.enabled or not self.redis_client:
            return 0

        try:
            counter_key = f"openhands:counter:{key}"

            # Use pipeline for atomic operations
            pipe = self.redis_client.pipeline()
            pipe.incrby(counter_key, increment)
            pipe.expire(counter_key, ttl)
            results = await pipe.execute()

            return int(results[0]) if results else 0

        except Exception as e:
            logger.warning(f"Error incrementing counter {key}: {e}")
            return 0

    async def wait_for_lock_with_timeout(
        self,
        key: str,
        lock_timeout: int = 300,
        wait_timeout: int = 60,
        retry_interval: float = 0.5
    ) -> bool:
        """
        Wait for a lock to become available with timeout.

        Args:
            key: Lock key
            lock_timeout: How long to hold the lock once acquired
            wait_timeout: How long to wait for lock to become available
            retry_interval: Time between retry attempts

        Returns:
            True if lock acquired, False if timeout
        """
        if not self.enabled:
            return True

        start_time = time.time()

        while time.time() - start_time < wait_timeout:
            if await self.acquire_lock(key, lock_timeout):
                return True

            await asyncio.sleep(retry_interval)

        logger.warning(f"Timeout waiting for lock {key}")
        return False


# Global coordinator instance
_coordinator: Optional[RedisCoordinator] = None


async def get_coordinator() -> RedisCoordinator:
    """Get or create the global Redis coordinator instance."""
    global _coordinator

    if _coordinator is None:
        _coordinator = RedisCoordinator()
        await _coordinator.connect()

    return _coordinator


async def close_coordinator():
    """Close the global coordinator instance."""
    global _coordinator

    if _coordinator:
        await _coordinator.close()
        _coordinator = None

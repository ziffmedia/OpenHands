
import asyncio
import logging

logger = logging.getLogger(__name__)

_shutdown_listeners = {}

def add_shutdown_listener(listener_id: str, callback):
    """
    Adds a callback to be executed during shutdown.
    """
    if listener_id in _shutdown_listeners:
        logger.warning(f"Shutdown listener with ID '{listener_id}' already exists. Overwriting.")
    _shutdown_listeners[listener_id] = callback
    logger.debug(f"Added shutdown listener: {listener_id}")

def remove_shutdown_listener(listener_id: str):
    """
    Removes a shutdown listener.
    """
    if listener_id in _shutdown_listeners:
        del _shutdown_listeners[listener_id]
        logger.debug(f"Removed shutdown listener: {listener_id}")
    else:
        logger.warning(f"Attempted to remove non-existent shutdown listener: {listener_id}")

async def run_shutdown_listeners():
    """
    Executes all registered shutdown listeners.
    """
    logger.info("Running all registered shutdown listeners...")
    for listener_id, callback in list(_shutdown_listeners.items()):
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback()
            else:
                callback()
            logger.debug(f"Executed shutdown listener: {listener_id}")
        except Exception as e:
            logger.error(f"Error executing shutdown listener '{listener_id}': {e}")
        finally:
            # Remove listener after execution, regardless of success or failure
            del _shutdown_listeners[listener_id]
    logger.info("All shutdown listeners executed.")


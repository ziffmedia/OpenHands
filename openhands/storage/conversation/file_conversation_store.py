from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from openhands.core.config.openhands_config import OpenHandsConfig
from openhands.core.logger import openhands_logger as logger
from openhands.storage import get_file_store
from openhands.storage.conversation.conversation_store import ConversationStore
from openhands.storage.data_models.conversation_metadata import ConversationMetadata
from openhands.storage.data_models.conversation_metadata_result_set import (
    ConversationMetadataResultSet,
)
from openhands.storage.files import FileStore
from openhands.storage.locations import (
    CONVERSATION_BASE_DIR,
    get_conversation_metadata_filename,
)
from openhands.utils.async_utils import call_sync_from_async
from openhands.utils.search_utils import offset_to_page_id, page_id_to_offset

conversation_metadata_type_adapter = TypeAdapter(ConversationMetadata)


@dataclass
class FileConversationStore(ConversationStore):
    file_store: FileStore

    async def save_metadata(self, metadata: ConversationMetadata) -> None:
        json_str = conversation_metadata_type_adapter.dump_json(metadata)
        path = self.get_conversation_metadata_filename(metadata.conversation_id)
        await call_sync_from_async(self.file_store.write, path, json_str)

    async def get_metadata(self, conversation_id: str) -> ConversationMetadata:
        path = self.get_conversation_metadata_filename(conversation_id)

        # Retry logic to handle race conditions where file might be empty temporarily
        max_retries = 3
        retry_delay = 0.1  # 100ms

        for attempt in range(max_retries + 1):
            try:
                json_str = await call_sync_from_async(self.file_store.read, path)

                # Check if file is empty (race condition case)
                if not json_str.strip():
                    if attempt < max_retries:
                        logger.warning(f'Empty metadata file detected for conversation {conversation_id}, retrying...')
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        raise FileNotFoundError(f'Metadata file is empty after {max_retries} retries: {path}')

                # Validate the JSON
                try:
                    json_obj = json.loads(json_str)
                except json.JSONDecodeError as e:
                    if attempt < max_retries:
                        logger.warning(f'Invalid JSON in metadata file for conversation {conversation_id}, retrying: {e}')
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        raise FileNotFoundError(f'Invalid JSON in metadata file after {max_retries} retries: {path}') from e

                if 'created_at' not in json_obj:
                    raise FileNotFoundError(path)

                # Remove github_user_id if it exists
                if 'github_user_id' in json_obj:
                    json_obj.pop('github_user_id')

                result = conversation_metadata_type_adapter.validate_python(json_obj)
                return result

            except FileNotFoundError:
                # Don't retry for genuine file not found errors
                raise
            except Exception as e:
                if attempt < max_retries:
                    logger.warning(f'Error reading metadata file for conversation {conversation_id}, retrying: {e}')
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    raise

    async def delete_metadata(self, conversation_id: str) -> None:
        path = str(
            Path(self.get_conversation_metadata_filename(conversation_id)).parent
        )
        await call_sync_from_async(self.file_store.delete, path)

    async def exists(self, conversation_id: str) -> bool:
        path = self.get_conversation_metadata_filename(conversation_id)
        try:
            await call_sync_from_async(self.file_store.read, path)
            return True
        except FileNotFoundError:
            return False

    async def search(
        self,
        page_id: str | None = None,
        limit: int = 20,
    ) -> ConversationMetadataResultSet:
        conversations: list[ConversationMetadata] = []
        metadata_dir = self.get_conversation_metadata_dir()
        try:
            conversation_ids = [
                path.split('/')[-2]
                for path in self.file_store.list(metadata_dir)
                if not path.startswith(f'{metadata_dir}/.')
            ]
        except FileNotFoundError:
            return ConversationMetadataResultSet([])
        num_conversations = len(conversation_ids)
        start = page_id_to_offset(page_id)
        end = min(limit + start, num_conversations)
        conversations = []
        for conversation_id in conversation_ids:
            try:
                conversations.append(await self.get_metadata(conversation_id))
            except Exception:
                logger.warning(
                    f'Could not load conversation metadata: {conversation_id}'
                )
        conversations.sort(key=_sort_key, reverse=True)
        conversations = conversations[start:end]
        next_page_id = offset_to_page_id(end, end < num_conversations)
        return ConversationMetadataResultSet(conversations, next_page_id)

    def get_conversation_metadata_dir(self) -> str:
        return CONVERSATION_BASE_DIR

    def get_conversation_metadata_filename(self, conversation_id: str) -> str:
        return get_conversation_metadata_filename(conversation_id)

    @classmethod
    async def get_instance(
        cls, config: OpenHandsConfig, user_id: str | None
    ) -> FileConversationStore:
        file_store = get_file_store(
            config.file_store,
            config.file_store_path,
            config.file_store_web_hook_url,
            config.file_store_web_hook_headers,
        )
        return FileConversationStore(file_store)


def _sort_key(conversation: ConversationMetadata) -> str:
    created_at = conversation.created_at
    if created_at:
        return created_at.isoformat()  # YYYY-MM-DDTHH:MM:SS for sorting
    return ''

import os
import shutil
import tempfile

from openhands.core.logger import openhands_logger as logger
from openhands.storage.files import FileStore


class LocalFileStore(FileStore):
    root: str

    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    def get_full_path(self, path: str) -> str:
        if path.startswith('/'):
            path = path[1:]
        return os.path.join(self.root, path)

    def write(self, path: str, contents: str | bytes) -> None:
        full_path = self.get_full_path(path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        # Use atomic write: write to temporary file first, then rename
        # This prevents race conditions where readers see empty/partial files
        mode = 'w' if isinstance(contents, str) else 'wb'
        dir_path = os.path.dirname(full_path)

        # Create temporary file in the same directory to ensure atomic rename works
        with tempfile.NamedTemporaryFile(mode=mode, dir=dir_path, delete=False) as temp_file:
            temp_path = temp_file.name
            temp_file.write(contents)
            temp_file.flush()
            os.fsync(temp_file.fileno())  # Force write to disk

        # Atomic rename - this is the key to preventing race conditions
        os.rename(temp_path, full_path)

    def read(self, path: str) -> str:
        full_path = self.get_full_path(path)
        with open(full_path, 'r') as f:
            return f.read()

    def list(self, path: str) -> list[str]:
        full_path = self.get_full_path(path)
        files = [os.path.join(path, f) for f in os.listdir(full_path)]
        files = [f + '/' if os.path.isdir(self.get_full_path(f)) else f for f in files]
        return files

    def delete(self, path: str) -> None:
        try:
            full_path = self.get_full_path(path)
            if not os.path.exists(full_path):
                logger.debug(f'Local path does not exist: {full_path}')
                return
            if os.path.isfile(full_path):
                os.remove(full_path)
                logger.debug(f'Removed local file: {full_path}')
            elif os.path.isdir(full_path):
                shutil.rmtree(full_path)
                logger.debug(f'Removed local directory: {full_path}')
        except Exception as e:
            logger.error(f'Error clearing local file store: {str(e)}')

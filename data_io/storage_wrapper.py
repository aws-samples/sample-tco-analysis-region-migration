import gzip
import datetime
from pathlib import Path
from botocore.exceptions import ClientError


class CacheExpiredError(FileNotFoundError):
    ...


class StorageWrapper:
    def __init__(self, base_dir: str = 'sku_cache'):
        """
        Initialize a very simple storage wrapper
        """
        self._storage_type = 'local'
        self._cache_dir = Path(base_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def read_bytes(self, key: str, cache_ttl: datetime.timedelta | None = None, compressed: bool = False) -> bytes:
        """Read data from cache"""
        if compressed:
            key += '.gz'

        try:
            cache_file = (self._cache_dir / key).resolve()
            if not cache_file.is_relative_to(self._cache_dir.resolve()):
                raise RuntimeError(f'Refusing to read {key} outside the cache directory at {self._cache_dir}')
            if cache_file.exists():
                last_modification = datetime.datetime.fromtimestamp(cache_file.stat(follow_symlinks=False).st_mtime)
                if cache_ttl is None or (last_modification > datetime.datetime.now() - cache_ttl):
                    return gzip.decompress(cache_file.read_bytes()) if compressed else cache_file.read_bytes()
                else:
                    raise CacheExpiredError(f'{cache_file} exists in local cache but has expired')

            raise FileNotFoundError(f'Could not find {cache_file} in local cache')
        except (FileNotFoundError, ClientError) as e:
            raise FileNotFoundError(f'Could not find {key} in {self._storage_type} cache') from e

    def write_bytes(self, key: str, data: bytes, compressed: bool = False) -> None:
        """Write data to cache"""
        if compressed:
            key += '.gz'
            data = gzip.compress(data)

        path = (self._cache_dir / key).resolve()
        if not path.is_relative_to(self._cache_dir.resolve()):
            raise RuntimeError(f'Refusing to write {key} outside the cache directory at {self._cache_dir}')
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(data)
        path.chmod(0o600)

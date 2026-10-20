"""Hot reload of the config file by polling.

Polling (stat + content hash) is used instead of inotify because inotify is
unreliable on network/cluster filesystems (GlusterFS, NFS) and on bind mounts
that are replaced atomically. An invalid new config is logged and ignored; the
running config stays in effect.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

from .config import Config, ConfigError, config_from_bytes

log = logging.getLogger(__name__)


class ConfigWatcher:
    def __init__(self, path: str | Path, on_change: Callable[[Config], None], interval: float = 10.0):
        self.path = Path(path)
        self.on_change = on_change
        self.interval = interval
        self._stat: tuple[float, int] | None = None
        self._digest: str | None = None

    def prime(self) -> None:
        """Record the state of the file that was loaded at startup."""
        try:
            self._stat = self._read_stat()
            self._digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            self._stat, self._digest = None, None

    def _read_stat(self) -> tuple[float, int]:
        st = self.path.stat()
        return st.st_mtime, st.st_size

    def check(self) -> bool:
        """Check once and apply. Returns True if a new config was applied."""
        config = self.poll()
        if config is None:
            return False
        self.on_change(config)
        return True

    def poll(self) -> Config | None:
        """Return a new, valid config if the file changed, else None. Blocking I/O."""
        try:
            stat = self._read_stat()
        except OSError as exc:
            if self._stat is not None:
                log.warning("Config file %s not readable (%s); keeping current config", self.path, exc)
                self._stat = None
            return None
        if stat == self._stat:
            return None
        self._stat = stat

        try:
            data = self.path.read_bytes()
        except OSError as exc:
            log.warning("Cannot read %s (%s); keeping current config", self.path, exc)
            return None
        digest = hashlib.sha256(data).hexdigest()
        if digest == self._digest:
            return None  # touched but unchanged

        try:
            config = config_from_bytes(data)  # parse exactly the bytes that were hashed
        except ConfigError as exc:
            # Remember the digest so a broken file is reported once, not every poll.
            self._digest = digest
            log.error("Config reload failed, keeping current config: %s", exc)
            return None
        except Exception:
            self._digest = digest
            log.exception("Config reload failed, keeping current config")
            return None

        self._digest = digest
        return config

    async def run(self) -> None:
        log.info("Watching %s for changes every %ss", self.path, self.interval)
        while True:
            await asyncio.sleep(self.interval)
            try:
                config = await asyncio.to_thread(self.poll)
                if config is not None:
                    self.on_change(config)
            except Exception:  # never let the watcher die
                log.exception("Config watcher error")

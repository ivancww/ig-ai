from __future__ import annotations

import logging
import signal
from collections.abc import Callable

from .streaming import IGStreamService

log = logging.getLogger(__name__)


class Runtime:
    """Small headless runner boundary; orchestration remains injectable/testable."""

    def __init__(self, stream: IGStreamService, on_update: Callable[[dict], None]):
        self.stream = stream
        self.on_update = on_update

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stream.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stream.stop())

    def run(self) -> None:
        self.install_signal_handlers()
        log.info("market-data runtime started")
        self.stream.run(self.on_update)
        log.info("market-data runtime stopped")

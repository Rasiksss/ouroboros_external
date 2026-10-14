from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from ouroboros.observability.logging_setup import LogContextFilter


@contextmanager
def dropping_handlers_pinned_to_the_test_stream() -> Iterator[None]:
    root = logging.getLogger()
    established = list(root.handlers)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler in established:
                continue
            if any(isinstance(entry, LogContextFilter) for entry in handler.filters):
                root.removeHandler(handler)
                handler.close()


@contextmanager
def logging_configuration_restored() -> Iterator[None]:
    loggers = {
        name: logger for name, logger in logging.Logger.manager.loggerDict.items() if isinstance(logger, logging.Logger)
    }
    levels = {name: logger.level for name, logger in loggers.items()}
    filters = {name: list(logger.filters) for name, logger in loggers.items()}
    root = logging.getLogger()
    root_level, root_handlers, root_filters = root.level, list(root.handlers), list(root.filters)
    try:
        yield
    finally:
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if not isinstance(logger, logging.Logger):
                continue
            if name in levels:
                logger.setLevel(levels[name])
            logger.filters[:] = filters.get(name, [])
        root.setLevel(root_level)
        for handler in root.handlers:
            if handler not in root_handlers:
                handler.close()
        root.handlers[:] = root_handlers
        root.filters[:] = root_filters

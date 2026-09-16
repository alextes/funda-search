"""Bounded, persistent backend activity shared by the server and fetcher."""
import builtins
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
from datetime import datetime, timezone

_lock = threading.RLock()
_handler = None
_path = None


def configure(path: Path) -> None:
    global _handler, _path
    with _lock:
        if _handler:
            _handler.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        _path = path
        _handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding='utf-8')
        _handler.setFormatter(logging.Formatter('%(message)s'))


def redact(message: str) -> str:
    for name in ('FUNDA_SEARCH_PASSWORD', 'OPENAI_API_KEY'):
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, '[redacted]')
    return re.sub(r'(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|(?:api[_-]?key|password|token)\s*[=:]\s*)[^\s&\"\']+', r'\1[redacted]', message)


def log_print(*args, sep=' ', end='\n', file=None, flush=False):
    """Keep console output and retain application messages (never HTTP access logs)."""
    message = redact(sep.join(str(arg) for arg in args)).strip()
    builtins.print(message, end=end, file=file, flush=flush)
    if not message:
        return
    entry = {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
             'level': 'error' if re.search(r'\b(failed|error|exception)\b', message, re.I) else 'info',
             'message': message[:4000]}
    with _lock:
        if _handler:
            try:
                _handler.emit(logging.LogRecord('activity', logging.INFO, '', 0,
                    json.dumps(entry, ensure_ascii=False), (), None))
            except OSError:
                pass  # A log failure must not stop ingestion.


def recent(limit=500):
    with _lock:
        if _path is None:
            return []
        entries = []
        for path in (Path(str(_path) + '.1'), _path):
            if not path.exists():
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    entry = json.loads(line)
                    entry['message'] = redact(entry['message'])
                    entries.append(entry)
                except (ValueError, KeyError, TypeError):
                    continue
        return list(reversed(entries[-limit:]))

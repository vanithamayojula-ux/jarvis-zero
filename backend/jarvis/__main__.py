"""python -m jarvis  →  starts the backend on http://127.0.0.1:8600"""
import logging
import re

import uvicorn

from .server import PORT, app

_SECRET_IN_URL = re.compile(r"((?:token|key|api_key|access_token)=)[^&\s\"']+", re.I)


class RedactSecrets(logging.Filter):
    """The dashboard and spidey pass JARVIS's token in the WebSocket address (browsers can't add headers there).
    Access logs print that address, so the token is masked before any line is written."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            record.args = tuple(_SECRET_IN_URL.sub(r"\1***", a) if isinstance(a, str) else a for a in args)
        if isinstance(record.msg, str):
            record.msg = _SECRET_IN_URL.sub(r"\1***", record.msg)
        return True


def main() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="info")   # sets up uvicorn's log handlers
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(RedactSecrets())
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(RedactSecrets())
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()

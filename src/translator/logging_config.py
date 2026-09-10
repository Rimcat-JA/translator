import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(directory: Path):
    handler = RotatingFileHandler(directory / "runtime.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("translator")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    # HTTP URLs / external exception bodies can contain credentials. Do not enable access logging.
    for name in ("httpx", "httpcore", "websockets", "ngrok"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

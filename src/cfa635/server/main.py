"""cfa635-server entry point."""

from __future__ import annotations

import logging

import uvicorn

from cfa635.server.app import create_app
from cfa635.server.config import Config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config()
    app = create_app(config)
    # Single process, single worker: the serial port has exactly one owner.
    uvicorn.run(app, host=config.http_host, port=config.http_port)


if __name__ == "__main__":
    main()

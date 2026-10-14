"""Entry point: `uv run python -m bgremover.main` (or `uvicorn bgremover.main:app`)."""

import logging
import os

from .app import create_app
from .config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app(Settings.from_env())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("BGR_HOST", "0.0.0.0"), port=int(os.environ.get("BGR_PORT", 8000)))

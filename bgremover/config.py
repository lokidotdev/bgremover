"""Runtime configuration, read once from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Settings:
    model_path: Path = ROOT / "models" / "birefnet-general.onnx"
    model_url: str = (
        "https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-epoch_244.onnx"
    )
    database_path: Path = ROOT / "data" / "bgremover.db"
    admin_password: str = ""
    secret_key: str = field(default_factory=lambda: secrets.token_hex(32))
    max_upload_bytes: int = 25 * 1024 * 1024
    max_pixels: int = 40_000_000
    workers: int = 4
    fp16: bool = True
    use_tensorrt: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        env = os.environ
        defaults = cls()
        return cls(
            model_path=Path(env.get("BGR_MODEL_PATH", defaults.model_path)),
            model_url=env.get("BGR_MODEL_URL", defaults.model_url),
            database_path=Path(env.get("BGR_DATABASE_PATH", defaults.database_path)),
            admin_password=env.get("BGR_ADMIN_PASSWORD", ""),
            secret_key=env.get("BGR_SECRET_KEY") or defaults.secret_key,
            max_upload_bytes=int(env.get("BGR_MAX_UPLOAD_MB", 25)) * 1024 * 1024,
            max_pixels=int(env.get("BGR_MAX_MEGAPIXELS", 40)) * 1_000_000,
            workers=int(env.get("BGR_WORKERS", defaults.workers)),
            fp16=env.get("BGR_FP16", "1") == "1",
            use_tensorrt=env.get("BGR_USE_TENSORRT", "0") == "1",
        )

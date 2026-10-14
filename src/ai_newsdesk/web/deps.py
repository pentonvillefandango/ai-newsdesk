"""Template environment shared by every route module."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
NO_STORE = {"Cache-Control": "no-store"}

templates = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(WEB_DIR / "templates"), autoescape=True)
)

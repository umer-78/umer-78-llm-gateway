"""uvicorn gateway.server:app"""
import logging
import os

from .app import create_app
from .config import load_config

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app(load_config(os.environ.get("GATEWAY_CONFIG", "config.yaml")))

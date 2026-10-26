"""ASGI entry point:  uvicorn mdserver.web.asgi:app  (configuration comes from the environment)."""
import logging

from mdserver import config
from mdserver.web.app import create_app

logging.basicConfig(level=logging.DEBUG if config.DEBUG else logging.INFO,
                    format='%(levelname)s %(name)s: %(message)s')
logging.getLogger('pymongo').setLevel(logging.WARNING)

app = create_app()

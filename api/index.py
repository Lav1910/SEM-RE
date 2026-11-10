"""Vercel entry point: the whole Flask app runs as one Python function (every path is rewritten here in vercel.json)."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from webapp.app import create_app  # noqa: E402

app = create_app()

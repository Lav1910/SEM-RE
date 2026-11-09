#!/usr/bin/env python3
"""Start the web app with Waitress (production WSGI server).

  .venv/bin/python serve.py                 # http://127.0.0.1:8080
  PORT=8080 HOST=0.0.0.0 python serve.py    # on a server, behind HTTPS (Caddy, nginx, or the host's load balancer)
"""
import logging
import os

from waitress import serve

from webapp.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()

if __name__ == "__main__":
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))
    logging.getLogger("hbm").info("HBM AI Visibility Audits on http://%s:%s", host, port)
    kw = {"threads": int(os.environ.get("WEB_THREADS", "8"))}
    if os.environ.get("TRUSTED_PROXY"):  # e.g. 127.0.0.1 when Caddy/nginx terminates HTTPS in front of the app
        kw.update(trusted_proxy=os.environ["TRUSTED_PROXY"], trusted_proxy_count=1,
                  trusted_proxy_headers="x-forwarded-for x-forwarded-proto x-forwarded-host", clear_untrusted_proxy_headers=True)
    serve(app, host=host, port=port, **kw)

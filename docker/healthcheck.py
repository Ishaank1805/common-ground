"""Container health check: the app answers /api/config (over HTTPS when a certificate is configured)."""
import os
import ssl
import sys
import urllib.request

scheme = "https" if "--certfile" in os.environ.get("GUNICORN_CMD_ARGS", "") else "http"
try:
    # Checking itself on localhost, so the certificate's hostname does not need to match.
    urllib.request.urlopen(f"{scheme}://127.0.0.1:5001/api/config", timeout=4, context=ssl._create_unverified_context())
except Exception:
    sys.exit(1)

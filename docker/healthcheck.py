import sys
import urllib.request

try:
    # Check through the public UI proxy, so both child processes must be healthy.
    with urllib.request.urlopen("http://127.0.0.1:3000/api/health", timeout=4) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)

from hmac import compare_digest
import json
import os
import re
import sys
from threading import Lock

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout
from dhl_tracking import track

TOKEN = os.environ.get("DHL_TRACK_TOKEN", "")
TRACK_LOCK = Lock()


def respond(start_response, status, data):
    body = json.dumps(data).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])
    return [body]


def log_browser_error(error):
    # Record the failing operation without logging URLs or shipment data.
    first_line = str(error).split("\n", 1)[0]
    operation = re.match(r"(?:Page|BrowserType|Browser|Locator)\.[a-z_]+(?=:)", first_line)
    network_code = re.search(r"\b(?:net::)?ERR_[A-Z0-9_]+\b", first_line)
    category = network_code.group(0) if network_code else type(error).__name__
    if "Executable doesn't exist" in first_line:
        category = "browser_executable_missing"
    elif "Host system is missing dependencies" in first_line:
        category = "browser_dependencies_missing"
    print(
        f"DHL Playwright: operation={operation.group(0) if operation else 'unknown'} "
        f"cause={category}", file=sys.stderr, flush=True,
    )


def application(environ, start_response):
    def send(status, data):
        return respond(start_response, status, data)

    path, method = environ["PATH_INFO"], environ["REQUEST_METHOD"]
    ready = len(TOKEN) >= 32
    if path == "/health" and method == "GET":
        return send("200 OK" if ready else "503 Service Unavailable", {"ok": ready})
    if path != "/track" or method != "POST":
        return send("404 Not Found", {"error": "not_found"})
    if not ready:
        return send("503 Service Unavailable", {"error": "not_configured"})
    supplied = environ.get("HTTP_AUTHORIZATION", "").encode()
    if not compare_digest(supplied, f"Bearer {TOKEN}".encode()):
        return send("401 Unauthorized", {"error": "unauthorized"})
    if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
        return send("415 Unsupported Media Type", {"error": "expected_json"})
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
        if not 0 < length <= 4096:
            raise ValueError()
        data = json.loads(environ["wsgi.input"].read(length))
        number = data.get("tracking_number") if isinstance(data, dict) else None
        if not isinstance(number, str) or not re.fullmatch(r"[0-9]{10}", number):
            raise ValueError()
    except (ValueError, RecursionError):
        return send("400 Bad Request", {"error": "invalid_tracking_number"})
    if not TRACK_LOCK.acquire(blocking=False):
        return send("429 Too Many Requests", {"error": "busy"})
    try:
        result = track(number)
    except BrowserTimeout as error:
        log_browser_error(error)
        return send("504 Gateway Timeout", {"error": "browser_timeout"})
    except BrowserError as error:
        log_browser_error(error)
        return send("502 Bad Gateway", {"error": "browser_error"})
    finally:
        TRACK_LOCK.release()

    if result["outcome"] == "ok":
        return send("200 OK", result)
    return send("502 Bad Gateway", result)

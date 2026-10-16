import hmac
from functools import wraps

from flask import jsonify, request, session

from mock_gps import config, logger
from mock_gps.api.ratelimit import RateLimiter

# Named once so the header the MacroDroid macros send stays in lockstep with
# the one the server reads.
API_KEY_HEADER = "API-ACCESS-KEY"
api_limiter = RateLimiter()


def mask_secret(value: str | None) -> str:
    if not value:
        return "None"
    return f"{value[:4]}..." if len(value) > 4 else "****"


def secret_equals(given: str, expected: str) -> bool:
    """Constant-time credential check that accepts any input.

    compare_digest() refuses to compare str containing non-ASCII and raises
    TypeError, so passing user input straight in turned a wrong password into a
    500 -- raised before the caller could throttle or log the attempt. Comparing
    the UTF-8 bytes is equally constant-time and never rejects an input.
    """
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def has_valid_api_key() -> bool:
    api_key = request.headers.get(API_KEY_HEADER, "")
    return bool(api_key) and secret_equals(api_key, config.API_ACCESS_KEY)


def has_valid_session() -> bool:
    return bool(session.get("authenticated"))


def require_api_key(allow_session: bool = True):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            address = request.remote_addr or "unknown"
            if allow_session and has_valid_session():
                api_limiter.clear(address)
                return f(*args, **kwargs)
            retry = api_limiter.retry_after(address)
            if retry and request.headers.get(API_KEY_HEADER):
                logger.log_security(f"API credential attempt throttled from {address}", "warning")
                return jsonify({"error": "Too many attempts"}), 429, {"Retry-After": str(retry)}
            if has_valid_api_key():
                api_limiter.clear(address)
                return f(*args, **kwargs)
            if allow_session and has_valid_session():
                # A fresh login must also clear the failures its expired session
                # accumulated, or the IP stays throttled for the whole window.
                api_limiter.clear(address)
                return f(*args, **kwargs)

            logger.log_security(
                # Store only a masked key prefix so security logs remain useful
                # without becoming a source of credential disclosure.
                f"Unauthorized request: {request.method} {request.path} "
                f"from {address}; key={mask_secret(request.headers.get(API_KEY_HEADER))}",
                "warning",
            )
            # Only a supplied-but-wrong key is a guessing attempt worth
            # throttling. A missing key just means the browser session expired,
            # and the dashboard needs a 401 to redirect to /login -- a 429 there
            # would leave the user stuck on an unauthenticated page.
            if request.headers.get(API_KEY_HEADER) and api_limiter.record_failure(address):
                return jsonify({"error": "Too many attempts"}), 429
            return jsonify({"error": "Unauthorized"}), 401

        return decorated_function

    return decorator

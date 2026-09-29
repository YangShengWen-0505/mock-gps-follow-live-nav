import hashlib
import logging

from flask import Flask, request

from mock_gps import config, db, logger

NOISY_GET_PATHS = {
    "/api/system_status",
    "/api/planned_route",
    "/api/navigation_history",
    "/api/movements/current",
    "/health",
    "/favicon.ico",
}


def create_app():
    logger.init_server_logs()
    try:
        config.validate_runtime_config()
    except RuntimeError as exc:
        logger.log_sys(f"Configuration validation failed: {exc}", "error")
        raise
    if config.TIMEZONE_WARNING:
        logger.log_sys(config.TIMEZONE_WARNING, "warning")
    if not config.GOOGLE_MAPS_API_KEY:
        logger.log_sys("GOOGLE_MAPS_API_KEY is not configured; mission planning will fail", "warning")
    elif config.gmaps_client is None:
        logger.log_sys("Google Maps API client initialization failed", "warning")
    db.init_db()
    app = Flask(__name__)
    app.secret_key = hashlib.sha256(config.FLASK_SESSION_SECRET.encode("utf-8")).hexdigest()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        # Waitress serves plain HTTP directly, so a Secure cookie would never be
        # sent back and login would silently fail. Keep the deployment inside
        # Tailscale or a trusted LAN instead.
        SESSION_COOKIE_SECURE=False,
        MAX_CONTENT_LENGTH=64 * 1024,
    )

    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.before_request
    def log_request_info():
        # The dashboard polls these read-only endpoints frequently; keeping
        # them out of all.log makes mission events much easier to audit.
        if request.method == "GET" and (
            request.path in NOISY_GET_PATHS
            or request.path.startswith("/api/history/")
            or request.path.startswith("/static/")
        ):
            return
        content_length = request.content_length or 0
        logger.log_sys(
            f"Request: {request.method} {request.path} "
            f"from {request.remote_addr} content_length={content_length}",
            "info",
        )

    @app.after_request
    def add_security_headers(response):
        # Keep browser-facing defaults strict because this app is normally
        # exposed over Tailscale and authenticated with a shared API key.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # OSM's public tile service requires a valid Referer from web pages.
        # Send only the cross-origin origin, rather than the full dashboard URL.
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if request.path.startswith("/static/vendor/") and response.status_code == 200:
            # Vendored Leaflet/Font Awesome are pinned by filename and never
            # change in place. This must override rather than setdefault:
            # Flask already stamps its own no-cache on static responses, which
            # would otherwise cancel out hosting these assets locally.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif not request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    from mock_gps.api.routes import api_bp
    app.register_blueprint(api_bp)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app

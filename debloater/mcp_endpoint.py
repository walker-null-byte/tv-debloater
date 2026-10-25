"""POST /mcp: the MCP Streamable HTTP endpoint, mounted on the existing Flask app.

TurboMCP speaks the protocol (initialize, tools/list, tools/call); this module is the HTTP
edge around it. That edge matters because whatever can reach /mcp can change the TV:

* Origin: a request carrying an Origin header must be same-origin, so a web page open in the
  user's browser cannot drive the TV through localhost. No CORS headers are ever sent.
* Host: without a token the Host header must be a loopback name (blocks DNS rebinding).
* Token: with TVD_MCP_TOKEN set, `Authorization: Bearer <token>` is required.
* Binding to a non-loopback address without a token mounts no endpoint at all.

Stateless: one JSON response per POST. GET and DELETE answer 405 (no server-initiated
stream, no sessions), which the spec allows.
"""
import hmac
import ipaddress
import os
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, request
from turbomcp import MCPHttpServer

from .mcp_tools import INSTRUCTIONS, build_registry


def is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Server(MCPHttpServer):
    """TurboMCP's JSON-RPC handler plus the `instructions` field in the initialize reply."""

    def handle_message(self, msg: dict):
        resp = super().handle_message(msg)
        if msg.get("method") == "initialize" and isinstance(resp, dict) and "result" in resp:
            resp["result"]["instructions"] = INSTRUCTIONS
        return resp


def _rpc_error(status: int, code: int, message: str, **headers) -> Response:
    resp = jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}})
    resp.status_code = status
    resp.headers.update(headers)
    return resp


def register_mcp(app, app_state, *, bind_host: str, token: str = "",
                 allowed_hosts: frozenset = frozenset(), path: str = "/mcp") -> tuple[bool, str]:
    """Mount the endpoint on `app`. Returns (mounted, one-line note for the startup banner)."""
    if not token and not is_loopback(bind_host):
        return False, (f"MCP endpoint NOT mounted: bound to {bind_host} (not loopback) without "
                       "a token. Set TVD_MCP_TOKEN to enable it.")

    server = _Server(build_registry(app_state))
    bp = Blueprint("mcp", __name__)

    def refuse():
        """None if the request may proceed, else the error response."""
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != request.host.lower():
            return _rpc_error(403, -32600, "Forbidden: cross-origin request")
        if token:
            auth = request.headers.get("Authorization", "")
            supplied = auth[7:] if auth.startswith("Bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return _rpc_error(401, -32001, "Unauthorized: missing or invalid bearer token",
                                  **{"WWW-Authenticate": "Bearer"})
        else:
            hostname = urlsplit("//" + request.host).hostname or ""
            if not (is_loopback(hostname) or hostname in allowed_hosts):
                return _rpc_error(403, -32600, f"Forbidden: host '{hostname}' is not allowed")
        return None

    @bp.post(path)
    def mcp_post():
        denied = refuse()
        if denied:
            return denied
        if not request.is_json:
            return _rpc_error(415, -32700, "Content-Type must be application/json")
        payload = request.get_json(silent=True)
        if payload is None:
            return _rpc_error(400, -32700, "Parse error")
        result = server.handle_batch(payload)
        if result is None:  # only notifications / responses were sent
            return Response(status=202)
        resp = jsonify(result)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    app.register_blueprint(bp)
    return True, ("MCP endpoint " + ("requires a bearer token" if token
                                     else "open to localhost only (set TVD_MCP_TOKEN to require a token)"))


def register_from_env(app, app_state) -> tuple[bool, str]:
    """Wire /mcp using the TVD_* environment variables (see README)."""
    if os.environ.get("TVD_MCP", "1").lower() in ("0", "false", "off", "no"):
        return False, "MCP endpoint disabled (TVD_MCP=0)"
    allowed = frozenset(h.strip().lower() for h in
                        os.environ.get("TVD_MCP_ALLOWED_HOSTS", "").split(",") if h.strip())
    return register_mcp(app, app_state,
                        bind_host=os.environ.get("TVD_HOST", "127.0.0.1"),
                        token=os.environ.get("TVD_MCP_TOKEN", ""),
                        allowed_hosts=allowed)

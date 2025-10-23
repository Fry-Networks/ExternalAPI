from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
import importlib.util
import logging
import os
from pathlib import Path as PathLib
import sys
import re

# Try to load .env automatically when possible (non-fatal)
dotenv_spec = importlib.util.find_spec("dotenv")
if dotenv_spec is not None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        # don't block startup if dotenv cannot be loaded
        pass

# Resolve 1Password references in environment values.
# Supported formats in .env:
#   op/Vault/Item/field
#   op://Vault/Item/field
# These are translated to the 1Password CLI URI format and fetched using `op read`.
import os
import subprocess
import shlex


def _fetch_op_secret(ref: str) -> str:
    """Fetch a value from 1Password CLI. Returns the raw ref on failure.

    Expects ref like 'op/Vault/Item/field' or 'op://Vault/Item/field'.
    """
    if not ref:
        return ref
    norm = ref
    if ref.startswith("op://"):
        norm = ref
    elif ref.startswith("op/"):
        # convert op/Vault/Item/field -> op://Vault/Item/field
        norm = "op://" + ref[len("op/"):]
    else:
        return ref

    try:
        # call `op read 'op://vault/item/field'`
        # ensure we don't invoke a shell; pass the URI as a single arg
        res = subprocess.run(["op", "read", norm], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except FileNotFoundError:
        # op CLI not available
        print(f"[ExternalAPI] 1Password CLI 'op' not found; leaving env var as-is")
        return ref
    except subprocess.CalledProcessError as e:
        print(f"[ExternalAPI] Failed to read 1Password secret {norm}: {e}; leaving env var as-is")
        return ref


# Resolve any env vars that look like op references (non-fatal)
for k, v in list(os.environ.items()):
    if isinstance(v, str) and (v.startswith("op/") or v.startswith("op://")):
        os.environ[k] = _fetch_op_secret(v)

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from contextlib import asynccontextmanager
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from models import (
    ExistsResponse,
    GenericOk,
    HardwareDocument,
    HardwareResponse,
    InstallationHeartbeat,
    LeaseAction,
    LeaseResponse,
    MinerProfileResponse,
    VersionResponse,
    MinerCode,
)
from storage import STORE

# Configure logging with timestamps and file output
def setup_logging():
    """Set up logging with both console and file output with timestamps."""
    # Import colorama for colored console output
    try:
        from colorama import init, Fore, Style
        init(autoreset=True)  # Automatically reset colors after each print
        colors_available = True
        
        # Add custom log level for API access errors
        API_ACCESS_ERROR_LEVEL = 35  # Between WARNING (30) and ERROR (40)
        logging.addLevelName(API_ACCESS_ERROR_LEVEL, "API ACCESS ERROR")
        
        color_codes = {
            'DEBUG': Fore.CYAN,
            'INFO': Fore.GREEN,
            'WARNING': Fore.YELLOW,
            'ERROR': Fore.RED + Style.BRIGHT,
            'CRITICAL': Fore.RED + Style.BRIGHT,
            'API ACCESS ERROR': Fore.RED + Style.BRIGHT,
            # Short auth notes (used in middleware) should be bright red
            'MISSING TOKEN': Fore.RED + Style.BRIGHT,
            'INVALID TOKEN': Fore.RED + Style.BRIGHT,
        }
        reset_code = Style.RESET_ALL
    except ImportError:
        colors_available = False
        color_codes = {}
        reset_code = ""
    
    # Create logs directory if it doesn't exist
    log_dir = PathLib("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Create log filename with current date
    log_filename = log_dir / f"hardware_api_{datetime.now().strftime('%Y%m%d')}.log"
    
    # Create custom colored formatter for console
    class ColoredFormatter(logging.Formatter):
        """Custom formatter that adds colors to console output with HTTP status code coloring."""
        
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.colors = color_codes
            self.reset = reset_code
            # HTTP status code colors - use the outer scope variables
            if colors_available:
                from colorama import Fore, Style
                self.status_colors = {
                    '200': Fore.GREEN,
                    '201': Fore.GREEN,
                    '202': Fore.GREEN,
                    '204': Fore.GREEN,
                    '301': Fore.YELLOW,
                    '302': Fore.YELLOW,
                    '307': Fore.YELLOW,
                    '308': Fore.YELLOW,
                    '400': Fore.RED + Style.BRIGHT,
                    '401': Fore.RED + Style.BRIGHT,
                    '403': Fore.RED + Style.BRIGHT,
                    '404': Fore.RED + Style.BRIGHT,
                    '422': Fore.RED + Style.BRIGHT,
                    '429': Fore.RED + Style.BRIGHT,
                    '500': Fore.RED + Style.BRIGHT,
                    '502': Fore.RED + Style.BRIGHT,
                    '503': Fore.RED + Style.BRIGHT,
                }
            else:
                self.status_colors = {}
        
        def format(self, record):
            if colors_available:
                # Add color to the level name
                # Our middleware builds messages like: "<IP> - [LEVEL] rest..."
                # Color the bracketed level label inside the message for clarity.
                formatted = super().format(record)

                # Color the bracketed level label (e.g., [INFO], [API ACCESS ERROR])
                level_label_pattern = r"\[([A-Z ]+)\]"

                def color_label(m):
                    label = m.group(1)
                    color = self.colors.get(label, None)
                    if color:
                        return f"{color}[{label}]{self.reset}"
                    return m.group(0)

                formatted = re.sub(level_label_pattern, color_label, formatted)

                # Color only the HTTP status code that appears immediately after
                # the bracketed level label (pattern: "] - <STATUS>"). This avoids
                # accidentally coloring IP octets (e.g. "204" in "204.76.203.219").
                status_pattern = r'(?<=\] - )([1-5][0-9]{2})\b'

                def color_status(match):
                    status = match.group(1)
                    if status in self.status_colors:
                        return f"{self.status_colors[status]}{status}{self.reset}"
                    return status

                formatted = re.sub(status_pattern, color_status, formatted)

                return formatted
            
            return super().format(record)
    
    # Configure logging format with timestamp. Message will include IP and level label.
    log_format = '[%(asctime)s] - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    
    # Create handlers
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter(log_format, datefmt=date_format))
    
    file_handler = logging.FileHandler(log_filename, mode='a', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    
    # Set up logging configuration
    logging.basicConfig(
        level=logging.INFO,
        handlers=[console_handler, file_handler]
    )
    
    # Configure uvicorn loggers to use our colored formatter but disable access logging
    # We'll handle access logging in our middleware instead
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers = [console_handler, file_handler]
    uvicorn_logger.propagate = False

    # Disable uvicorn access logger to avoid duplicate logs
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.handlers = []
    uvicorn_access_logger.propagate = False

    # Create two specialized loggers: console-only and file-only so we can
    # selectively include sensitive fragments (like full User-Agent) in file logs
    console_logger = logging.getLogger("ExternalAPI.console")
    console_logger.handlers = [console_handler]
    console_logger.propagate = False
    console_logger.setLevel(logging.INFO)

    file_logger = logging.getLogger("ExternalAPI.file")
    file_logger.handlers = [file_handler]
    file_logger.propagate = False
    file_logger.setLevel(logging.INFO)

    # Also provide a general app logger (keeps previous behavior for other messages)
    logger = logging.getLogger("ExternalAPI")
    logger.handlers = [console_handler, file_handler]
    logger.propagate = False

    logger.info("Logging initialized - console and file output enabled with colors")
    return logger

# Initialize logging
logger = setup_logging()

# Initialize rate limiter
# Rate limit can be configured via FLXTIME_RATE_LIMIT env var (default: 100 requests per minute)
limiter = Limiter(key_func=get_remote_address, default_limits=[])

# Bearer token security schemes for protected endpoints
bearer_scheme = HTTPBearer(auto_error=False)


def verify_bearer_token_flxtime(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    """Validate bearer token against API_BEARER_TOKEN_FLXTIME or API_BEARER_TOKEN environment variables.
    
    Used for FlxTime-specific endpoints like /credentials/{miner_key}/exists.
    Accepts either the FlxTime-specific token OR the general API token.
    Raises HTTPException 401 if token is missing or invalid.
    Returns the token if valid.
    """
    flxtime_token = os.getenv("API_BEARER_TOKEN_FLXTIME")
    general_token = os.getenv("API_BEARER_TOKEN")
    
    if not flxtime_token and not general_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_BEARER_TOKEN_FLXTIME or API_BEARER_TOKEN not configured on server"
        )
    
    if not credentials:
        # attach a short auth note for middleware logging and raise
        request.state.auth_note = "MISSING TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Accept either the FlxTime-specific token OR the general token
    if credentials.credentials != flxtime_token and credentials.credentials != general_token:
        request.state.auth_note = "INVALID TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # mark success briefly for possible middleware use (optional)
    request.state.auth_note = "OK"
    return credentials.credentials


def verify_bearer_token_general(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    """Validate bearer token against API_BEARER_TOKEN environment variable.
    
    Used for general API endpoints (versions, credentials, installations, leases, PoC).
    Raises HTTPException 401 if token is missing or invalid.
    Returns the token if valid.
    """
    expected_token = os.getenv("API_BEARER_TOKEN")
    
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_BEARER_TOKEN not configured on server"
        )
    
    if not credentials:
        request.state.auth_note = "MISSING TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if credentials.credentials != expected_token:
        request.state.auth_note = "INVALID TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.auth_note = "OK"
    return credentials.credentials


def verify_bearer_token_admin(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    """Validate bearer token against API_BEARER_TOKEN_ADMIN environment variable.

    This token is intended for protecting admin-only endpoints (unblock/list operations).
    Falls back to API_BEARER_TOKEN if API_BEARER_TOKEN_ADMIN is not configured (for smooth upgrades).
    """
    admin_token = os.getenv("API_BEARER_TOKEN_ADMIN")
    general_token = os.getenv("API_BEARER_TOKEN")

    # If no admin-specific token is configured, allow the general token only if present.
    if not admin_token and not general_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_BEARER_TOKEN_ADMIN or API_BEARER_TOKEN not configured on server"
        )

    if not credentials:
        request.state.auth_note = "MISSING TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Prefer admin_token if set; otherwise accept general_token
    if admin_token:
        valid = credentials.credentials == admin_token
    else:
        valid = credentials.credentials == general_token

    if not valid:
        request.state.auth_note = "INVALID TOKEN"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.auth_note = "OK"
    return credentials.credentials
@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup: augment generated OpenAPI schema with human-readable
    # descriptions for MinerCode enum values and cache the modified schema.
    try:
        from models import MinerCode

        enum_desc = {
            "BM": "Bandwidth Miner",
            "IDM": "Indoor Decibel Miner",
            "ODM": "Outdoor Decibel Miner",
            "ISM": "Indoor Satellite Miner",
            "OSM": "Outdoor Satellite Miner",
            "RDN": "Reward Decentralization Node",
            "SDN": "Storage Decentralization Node",
            "SVN": "Storage Validator Node",
            "AEM": "AI Edge Miner",
            "IRM": "Indoor Radiation Miner",
        }

        # Generate and cache the OpenAPI schema once. Avoid injecting
        # vendor-specific fields such as `x-enum-descriptions` into the
        # parameter schemas to prevent raw lists from showing in the UI.
        openapi_schema = app.openapi()

        def _cached_openapi():
            return openapi_schema

        app.openapi = _cached_openapi
    except Exception:
        # Non-fatal; continue startup even if we couldn't augment OpenAPI
        pass
    yield


# Define tags metadata to control the order and descriptions in the documentation
tags_metadata = [
    {
        "name": "FlxTime",
        "description": "Special endpoints for FlxTime partner integration. These endpoints require specific authentication tokens and have dedicated rate limiting.",
    },
    {
        "name": "Health",
        "description": "Service health and status endpoints.",
    },
    {
        "name": "Versions",
        "description": "Hardware miner software version management.",
    },
    {
        "name": "Credentials",
        "description": "Credential and profile lookup from the credentials database.",
    },
    {
        "name": "Installations",
        "description": "Installation heartbeats and tracking.",
    },
    {
        "name": "Leases",
        "description": "Lease coordination and management.",
    },
    {
        "name": "PoC",
        "description": "Proof of Connectivity (PoC) document storage and retrieval.",
    },
]

app = FastAPI(
    title="Hardware API",
    version="1.0.0",
    summary="Reference implementation of the FryNetworks hardware miner HTTP contract.",
    description=(
        "This service exposes endpoints for: \n"
        " - Version management (required hardware miner software)\n"
        " - Credential/profile lookup (from creds database)\n"
        " - Installation heartbeats and lease coordination (PoC database)\n"
        " - Hardware/PoC document storage\n\n"
        "Endpoints are grouped by functional area in the UI. Request/response schemas"
        " are documented using Pydantic models in `models.py`."
    ),
    openapi_tags=tags_metadata,
    lifespan=lifespan,
)

# Simple in-process tracker for repeated 404 probes and a temporary blocklist.
# This is intended as a lightweight mitigation for opportunistic scanners.
# Configurable via environment variables:
#   PROBE_404_WINDOW (seconds) - sliding window to count 404s (default 60)
#   PROBE_404_THRESHOLD - number of 404s in window to trigger a block (default 20)
#   PROBE_BLOCK_SECONDS - how long to block an offender (default 600)
from collections import defaultdict, deque
import asyncio

_probe_404_window = int(os.getenv("PROBE_404_WINDOW", "60"))
_probe_404_threshold = int(os.getenv("PROBE_404_THRESHOLD", "20"))
_probe_block_seconds = int(os.getenv("PROBE_BLOCK_SECONDS", "600"))

# maps ip -> deque[timestamps_of_404s]
_probe_404_counters: Dict[str, deque] = defaultdict(lambda: deque())
# maps ip -> blocked_until_timestamp
_probe_blocklist: Dict[str, float] = {}
# lock for concurrency safety
_probe_lock = asyncio.Lock()

# Add middleware to log HTTP responses with appropriate levels
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = datetime.now()
    client_ip = request.client.host if request.client else "unknown"
    # If IP is currently blocked, short-circuit with 403 and log
    now_ts = datetime.now().timestamp()
    blocked_until = _probe_blocklist.get(client_ip)
    if blocked_until and blocked_until > now_ts:
        # Immediate short-circuit response for blocked IPs
        # Emit a fail2ban-friendly marker so external tools see repeated-block events
        try:
            logger.warning(f"FAILED_PROBE_BLOCK ip={client_ip} blocked_until={blocked_until}")
        except Exception:
            logger.warning("FAILED_PROBE_BLOCK ip=%s blocked", client_ip)
        logger.warning(f"{client_ip} - [WARNING] - 403 BLOCKED - IP temporarily blocked due to repeated probes")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Forbidden", status_code=403)
    response = await call_next(request)
    
    # Only log non-static endpoints and important status codes
    url_path = request.url.path
    
    # Skip logging for docs, static files, and health checks
    if url_path in ["/docs", "/openapi.json", "/redoc", "/health"] or url_path.startswith("/static"):
        return response
    
    # Log the request/response with concise format
    duration = (datetime.now() - start_time).total_seconds()
    # client_ip already computed above for early block checks
    method = request.method
    status_code = response.status_code
    # Capture query string and User-Agent for better fingerprinting of probes
    query_string = request.url.query
    user_agent = request.headers.get("user-agent")
    
    # Pull an optional short auth note from the request (set by auth dependencies)
    auth_note = getattr(request.state, "auth_note", None)
    note_suffix = f" - [{auth_note}]" if auth_note and auth_note != "OK" else ""

    # Build the concise message. Format required:
    # [timestamp] - <IP> - [LEVEL_LABEL] <status> <METHOD> <PATH> - <AUTH_NOTE>
    # Build an extra suffix with query-string and a shortened user-agent when available
    qs_suffix = f" - QS: ?{query_string}" if query_string else ""
    # Shorten User-Agent to avoid long clutter in console logs. Configurable via LOG_UA_MAXLEN.
    if user_agent:
        try:
            max_ua = int(os.getenv("LOG_UA_MAXLEN", "40"))
        except Exception:
            max_ua = 40
        ua_clean = re.sub(r"\s+", " ", user_agent).strip()
        short_ua = ua_clean if len(ua_clean) <= max_ua else ua_clean[: max_ua - 3] + "..."
        ua_suffix = f" - UA: {short_ua}"
    else:
        ua_suffix = ""

    # Prepare two message variants: console (no UA) and file (with UA)
    console_msg_base = f"{client_ip} - [{{}}] - {status_code} {method} {url_path}{note_suffix}{qs_suffix}"
    file_msg_base = f"{client_ip} - [{{}}] - {status_code} {method} {url_path}{note_suffix}{qs_suffix}{ua_suffix}"

    if status_code >= 500:
        level_label = 'ERROR'
        console_msg = console_msg_base.format(level_label)
        file_msg = file_msg_base.format(level_label)
        logging.getLogger("ExternalAPI.console").error(console_msg)
        logging.getLogger("ExternalAPI.file").error(file_msg)
    elif status_code >= 400:
        level_label = 'API ACCESS ERROR'
        console_msg = console_msg_base.format(level_label)
        file_msg = file_msg_base.format(level_label)
        logging.getLogger("ExternalAPI.console").log(35, console_msg)
        logging.getLogger("ExternalAPI.file").log(35, file_msg)
        # Track repeated 404/4xx probes and temporarily block if threshold exceeded
        try:
            async with _probe_lock:
                if status_code == 404:
                    dq = _probe_404_counters[client_ip]
                    now_ts = datetime.now().timestamp()
                    # Emit a machine-parseable marker that fail2ban can watch for
                    try:
                        logger.warning(f"FAILED_PROBE_404 ip={client_ip} path={url_path} ua=\"{user_agent or ''}\"")
                    except Exception:
                        # safe fallback if formatting fails
                        logger.warning("FAILED_PROBE_404 ip=%s path=%s", client_ip, url_path)
                    dq.append(now_ts)
                    # Trim old timestamps outside the window
                    while dq and dq[0] < now_ts - _probe_404_window:
                        dq.popleft()
                    if len(dq) >= _probe_404_threshold:
                        # Block the IP for configured duration
                        _probe_blocklist[client_ip] = now_ts + _probe_block_seconds
                        # emit a fail2ban-friendly block marker as well
                        logger.warning(f"FAILED_PROBE_BLOCK ip={client_ip} duration={_probe_block_seconds} reason=404s")
                        logger.warning(f"{client_ip} - [WARNING] - IP blocked for {_probe_block_seconds}s due to {len(dq)} 404s in {_probe_404_window}s")
                        dq.clear()
                else:
                    # For other 4xx codes we might optionally clear counters or ignore
                    pass
        except Exception:
            # Non-fatal: don't let tracking break request handling
            logger.debug("probe tracking failed")
    elif status_code >= 300:
        level_label = 'WARNING'
        # If there's a Location header, include redirect target for clarity
        location = response.headers.get('location') or response.headers.get('Location')
        redirect_suffix = f" - Redirect -> {location}" if location else ""
        console_msg = console_msg_base.format(level_label) + redirect_suffix
        file_msg = file_msg_base.format(level_label) + redirect_suffix
        logging.getLogger("ExternalAPI.console").warning(console_msg)
        logging.getLogger("ExternalAPI.file").warning(file_msg)
    else:
        level_label = 'INFO'
        console_msg = console_msg_base.format(level_label)
        file_msg = file_msg_base.format(level_label)
        logging.getLogger("ExternalAPI.console").info(console_msg)
        logging.getLogger("ExternalAPI.file").info(file_msg)
    
    return response

# Add rate limit exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


# Redirect root to the interactive docs to provide a friendly public entrypoint.
@app.get("/", include_in_schema=False)
def _root_redirect():
    return RedirectResponse(url="/docs")

# Inject human-readable enum descriptions for MinerCode into the OpenAPI schema.
# This uses a vendor extension `x-enum-descriptions` which Swagger UI will render
# in the schema details. We also set an example for endpoints that reference
# MinerCode via the model-level examples already present in `models.py`.
# (OpenAPI augmentation moved into lifespan handler above.)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@app.get("/admin/blocks", tags=["Admin"], summary="List temporarily blocked IPs")
def list_blocked_ips(token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    """Return the current in-memory temporary blocklist.

    Note: this blocklist is in-memory and will reset if the app restarts.
    """
    now_ts = datetime.now().timestamp()
    # return only currently blocked IPs with remaining seconds
    blocked = {ip: max(0, int(until - now_ts)) for ip, until in _probe_blocklist.items() if until > now_ts}
    return {"blocked": blocked}


@app.post("/admin/blocks/unblock", tags=["Admin"], summary="Unblock an IP")
def unblock_ip(payload: Dict[str, str], token: str = Depends(verify_bearer_token_admin)) -> Dict[str, Any]:
    """Remove an IP from the in-memory blocklist. Expects JSON {"ip": "1.2.3.4"}.

    Emits a fail2ban-friendly UNBLOCK marker for external monitoring.
    """
    ip = payload.get("ip")
    if not ip:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing 'ip' in payload")
    removed = False
    if ip in _probe_blocklist:
        del _probe_blocklist[ip]
        removed = True
        try:
            logger.warning(f"FAILED_PROBE_UNBLOCK ip={ip}")
        except Exception:
            logger.warning("FAILED_PROBE_UNBLOCK ip=%s", ip)
    return {"unblocked": removed}


@app.get("/health", tags=["Health"], summary="Health check")
def health() -> Dict[str, Any]:
    """Simple health endpoint used by probes and external tunnels.

    Returns a small JSON payload with readiness and runtime information.
    """
    return {
        "ok": True,
        "pid": os.getpid(),
        "port": int(os.getenv("PORT", "8081")),
        "time": utc_now().isoformat(),
    }



@app.get(
    "/versions/{miner_code}",
    response_model=VersionResponse,
    summary="Get required miner version",
    tags=["Versions"],
)
def get_required_version(
    miner_code: MinerCode = Path(...),
    token: str = Depends(verify_bearer_token_general)
) -> VersionResponse:
    # MinerCode is an enum; use its value (e.g. 'AEM') when querying the store
    version = STORE.get_required_version(miner_code.value)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Miner code not found")
    return VersionResponse(required_version=version)


@app.get(
    "/credentials/{miner_key}",
    response_model=MinerProfileResponse,
    summary="Get miner credentials/profile",
    tags=["Credentials"],
)
def get_miner_profile(
    miner_key: str = Path(..., description="Full miner key"),
    token: str = Depends(verify_bearer_token_general)
) -> MinerProfileResponse:
    profile = STORE.get_miner_profile(miner_key)
    return MinerProfileResponse(**profile)


@app.get(
    "/credentials/{miner_key}/exists",
    response_model=ExistsResponse,
    summary="Check if miner_key exists",
    tags=["FlxTime"],
)
@limiter.limit(os.getenv("FLXTIME_RATE_LIMIT", "100/minute"))
def check_miner_exists(
    request: Request,
    miner_key: str = Path(..., description="Full miner key"),
    token: str = Depends(verify_bearer_token_flxtime)
) -> ExistsResponse:
    """Check whether a miner_key exists in the credentials database.

    This endpoint is specifically designed for FlxTime partner integration.

    Returns {"exists": true} if found, {"exists": false} otherwise.
    """
    profile = STORE.get_miner_profile(miner_key)
    exists = profile.get("exists", False)
    return ExistsResponse(exists=exists)


@app.post(
    "/installations/{miner_key}/installations/{install_id}",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upsert installation heartbeat",
    tags=["Installations"],
)
def upsert_installation(
    miner_key: str,
    install_id: str,
    heartbeat: InstallationHeartbeat,
    token: str = Depends(verify_bearer_token_general)
) -> GenericOk:
    if heartbeat.miner_key != miner_key or heartbeat.install_id != install_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body miner identity mismatch")
    payload = heartbeat.model_dump()
    payload.setdefault("last_seen_at", utc_now().isoformat())
    STORE.upsert_installation(miner_key, install_id, payload)
    return GenericOk()


@app.post(
    "/installations/{miner_key}/leases/{install_id}",
    response_model=LeaseResponse,
    summary="Acquire a mining lease",
    tags=["Leases"],
)
def acquire_installation_lease(
    miner_key: str,
    install_id: str,
    action: LeaseAction = Body(default_factory=LeaseAction),
    token: str = Depends(verify_bearer_token_general)
) -> LeaseResponse:
    granted, record = STORE.acquire_lease(miner_key, install_id, action.lease_seconds)
    # Use the returned LeaseRecord (if provided) to avoid an extra status DB call.
    if record:
        expires_at = getattr(record, "expires_at", None)
        expires_iso = None
        ttl = 0
        try:
            if isinstance(expires_at, str):
                expires_dt = datetime.fromisoformat(expires_at)
            else:
                expires_dt = expires_at
            if isinstance(expires_dt, datetime) and expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if isinstance(expires_dt, datetime):
                expires_iso = expires_dt.isoformat()
                ttl = max(0, int((expires_dt - utc_now()).total_seconds()))
        except Exception:
            expires_iso = None
            ttl = 0
        status_payload = {"active": bool(granted), "holder_install_id": getattr(record, "holder_install_id", None), "expires_at": expires_iso, "ttl_seconds": ttl}
        return LeaseResponse(granted=granted, **status_payload)
    # fallback: ask the store for status
    logger.debug(f"acquire_installation_lease: FALLBACK calling STORE.lease_status for {miner_key}/{install_id}")
    status_payload = STORE.lease_status(miner_key)
    return LeaseResponse(granted=granted, **status_payload)


@app.patch(
    "/installations/{miner_key}/leases/{install_id}",
    response_model=LeaseResponse,
    summary="Renew a mining lease",
    tags=["Leases"],
)
def renew_installation_lease(
    miner_key: str,
    install_id: str,
    action: LeaseAction = Body(default_factory=LeaseAction),
    token: str = Depends(verify_bearer_token_general)
) -> LeaseResponse:
    granted, record = STORE.renew_lease(miner_key, install_id, action.lease_seconds)
    # Use returned LeaseRecord to avoid an extra status call when possible
    if record:
        expires_at = getattr(record, "expires_at", None)
        expires_iso = None
        ttl = 0
        try:
            if isinstance(expires_at, str):
                expires_dt = datetime.fromisoformat(expires_at)
            else:
                expires_dt = expires_at
            if isinstance(expires_dt, datetime) and expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if isinstance(expires_dt, datetime):
                expires_iso = expires_dt.isoformat()
                ttl = max(0, int((expires_dt - utc_now()).total_seconds()))
        except Exception:
            expires_iso = None
            ttl = 0
        status_payload = {"active": bool(granted), "holder_install_id": getattr(record, "holder_install_id", None), "expires_at": expires_iso, "ttl_seconds": ttl}
        return LeaseResponse(granted=granted, **status_payload)
    logger.debug(f"renew_installation_lease: FALLBACK calling STORE.lease_status for {miner_key}/{install_id}")
    status_payload = STORE.lease_status(miner_key)
    return LeaseResponse(granted=granted, **status_payload)


@app.get(
    "/installations/{miner_key}/leases/current",
    response_model=LeaseResponse,
    summary="Get current lease status",
    tags=["Leases"],
)
def lease_status(
    miner_key: str,
    token: str = Depends(verify_bearer_token_general)
) -> LeaseResponse:
    status_payload = STORE.lease_status(miner_key)
    # Expose False if no active lease.
    return LeaseResponse(granted=status_payload.get("active", False), **status_payload)


@app.get(
    "/PoC/{miner_key}/hardware",
    response_model=HardwareResponse,
    summary="Get PoC hardware document",
    tags=["PoC"],
)
def get_hardware_doc(
    miner_key: str,
    token: str = Depends(verify_bearer_token_general)
) -> HardwareResponse:
    doc = STORE.get_hardware_doc(miner_key)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No hardware document")
    return HardwareResponse(document=doc)


@app.put(
    "/PoC/{miner_key}/hardware",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Replace PoC hardware document",
    tags=["PoC"],
)
def put_hardware_doc(
    miner_key: str,
    payload: HardwareDocument,
    token: str = Depends(verify_bearer_token_general)
) -> GenericOk:
    document = dict(payload.document)
    document.setdefault("miner_key", miner_key)
    document.setdefault("lastUpdated", utc_now().isoformat())
    STORE.put_hardware_doc(miner_key, document)
    return GenericOk()


if __name__ == "__main__":
    import os
    import uvicorn

    # Allow runtime configuration via environment variables.
    # PORT - port number (defaults to 8080)
    # HOST - host to bind (defaults to 0.0.0.0 for production)
    # UVICORN_RELOAD - if set to '1' or 'true' (case-insensitive), enable reload (disabled by default for production)
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    reload_env = os.getenv("UVICORN_RELOAD", "false").lower()
    reload_flag = reload_env in ("1", "true", "yes")

    logger.info(f"Starting server on {host}:{port} (reload={reload_flag})")
    
    # Configure uvicorn to use our existing logging configuration
    # We'll disable uvicorn's log_config to preserve our colored formatting
    uvicorn.run(
        "app:app", 
        host=host, 
        port=port, 
        reload=reload_flag,
        log_config=None,  # Use existing logging configuration
        access_log=False,  # Disable uvicorn access logging - we handle it in middleware
        use_colors=False   # Disable uvicorn colors since we handle our own
    )

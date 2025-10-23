from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
import importlib.util

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
        print("[ExternalAPI] 1Password CLI 'op' not found; leaving env var as-is")
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

# Initialize rate limiter
# Rate limit can be configured via FLXTIME_RATE_LIMIT env var (default: 100 requests per minute)
limiter = Limiter(key_func=get_remote_address, default_limits=[])

# Bearer token security schemes for protected endpoints
bearer_scheme = HTTPBearer(auto_error=False)


def verify_bearer_token_flxtime(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Accept either the FlxTime-specific token OR the general token
    if credentials.credentials != flxtime_token and credentials.credentials != general_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return credentials.credentials


def verify_bearer_token_general(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if credentials.credentials != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
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

        # Generate the OpenAPI schema once and attach descriptions
        openapi_schema = app.openapi()
        schemas = openapi_schema.get("components", {}).get("schemas", {})
        for name, schema in schemas.items():
            enum_vals = schema.get("enum", [])
            if enum_vals and set(enum_vals) & set(enum_desc.keys()):
                schema["x-enum-descriptions"] = [enum_desc.get(v, "") for v in enum_vals]

        # Also attach enum descriptions to operation parameters so Swagger UI
        # can show descriptions near the selected value in the parameter UI.
        try:
            paths = openapi_schema.get("paths", {})
            for path, ops in paths.items():
                for op, opobj in ops.items():
                    # each operation can have a 'parameters' list
                    params = opobj.get("parameters") or []
                    for p in params:
                        # Parameter may be inline schema or a $ref
                        schema = p.get("schema") or {}
                        # If schema is a $ref, resolve to the component schema
                        ref = schema.get("$ref")
                        target_schema = None
                        if ref and ref.startswith("#/components/schemas/"):
                            key = ref.split("/")[-1]
                            target_schema = schemas.get(key)
                        else:
                            target_schema = schema

                        if target_schema:
                            enum_vals = target_schema.get("enum", [])
                            if enum_vals and set(enum_vals) & set(enum_desc.keys()):
                                # attach matching descriptions in the same order
                                p["x-enum-descriptions"] = [enum_desc.get(v, "") for v in enum_vals]
        except Exception:
            # Non-fatal; keep going even if parameter-level augmentation fails
            pass

        def _cached_openapi():
            return openapi_schema

        app.openapi = _cached_openapi
    except Exception:
        # Non-fatal; continue startup even if we couldn't augment OpenAPI
        pass
    yield


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
    lifespan=lifespan,
)

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
    tags=["Credentials"],
)
@limiter.limit(os.getenv("FLXTIME_RATE_LIMIT", "100/minute"))
def check_miner_exists(
    request: Request,
    miner_key: str = Path(..., description="Full miner key"),
    token: str = Depends(verify_bearer_token_flxtime)
) -> ExistsResponse:
    """Check whether a miner_key exists.
    
    Requires bearer token authentication via Authorization header.
    Accepts either API_BEARER_TOKEN_FLXTIME or API_BEARER_TOKEN.
    
    Rate limited to 100 requests per minute per IP (configurable via FLXTIME_RATE_LIMIT).
    
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
    print(f"[API] acquire_installation_lease: FALLBACK calling STORE.lease_status for {miner_key}/{install_id}")
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
    print(f"[API] renew_installation_lease: FALLBACK calling STORE.lease_status for {miner_key}/{install_id}")
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

    print(f"[ExternalAPI] Starting server on {host}:{port} (reload={reload_flag})")
    uvicorn.run("app:app", host=host, port=port, reload=reload_flag)

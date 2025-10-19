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

from fastapi import Body, FastAPI, HTTPException, Path, status

from models import (
    GenericOk,
    HardwareDocument,
    HardwareResponse,
    InstallationHeartbeat,
    LeaseAction,
    LeaseHistoryResponse,
    LeaseResponse,
    MinerProfileResponse,
    VersionResponse,
)
from storage import STORE

app = FastAPI(
    title="Miner External API",
    version="0.1.0",
    summary="Reference implementation of the FryNetworks miner HTTP contract.",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)



@app.get("/versions/{miner_code}", response_model=VersionResponse)
def get_required_version(miner_code: str = Path(..., description="Miner family code", min_length=2)) -> VersionResponse:
    version = STORE.get_required_version(miner_code)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Miner code not found")
    return VersionResponse(required_version=version)


@app.get("/miners/{miner_key}", response_model=MinerProfileResponse)
def get_miner_profile(miner_key: str = Path(..., description="Full miner key")) -> MinerProfileResponse:
    profile = STORE.get_miner_profile(miner_key)
    return MinerProfileResponse(**profile)


@app.post(
    "/miners/{miner_key}/installations/{install_id}",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
)
def upsert_installation(
    miner_key: str,
    install_id: str,
    heartbeat: InstallationHeartbeat,
) -> GenericOk:
    if heartbeat.miner_key != miner_key or heartbeat.install_id != install_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body miner identity mismatch")
    payload = heartbeat.model_dump()
    payload.setdefault("last_seen_at", utc_now().isoformat())
    STORE.upsert_installation(miner_key, install_id, payload)
    return GenericOk()


@app.post(
    "/miners/{miner_key}/leases/{install_id}",
    response_model=LeaseResponse,
)
def acquire_installation_lease(
    miner_key: str,
    install_id: str,
    action: LeaseAction = Body(default_factory=LeaseAction),
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
    "/miners/{miner_key}/leases/{install_id}",
    response_model=LeaseResponse,
)
def renew_installation_lease(
    miner_key: str,
    install_id: str,
    action: LeaseAction = Body(default_factory=LeaseAction),
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
    "/miners/{miner_key}/leases/current",
    response_model=LeaseResponse,
)
def lease_status(miner_key: str) -> LeaseResponse:
    status_payload = STORE.lease_status(miner_key)
    # Expose False if no active lease.
    return LeaseResponse(granted=status_payload.get("active", False), **status_payload)


@app.get(
    "/miners/{miner_key}/leases/history",
    response_model=LeaseHistoryResponse,
)
def lease_history(miner_key: str) -> LeaseHistoryResponse:
    history = STORE.lease_history(miner_key)
    return LeaseHistoryResponse(leases=history)


@app.get(
    "/miners/{miner_key}/hardware",
    response_model=HardwareResponse,
)
def get_hardware_doc(miner_key: str) -> HardwareResponse:
    doc = STORE.get_hardware_doc(miner_key)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No hardware document")
    return HardwareResponse(document=doc)


@app.put(
    "/miners/{miner_key}/hardware",
    response_model=GenericOk,
    status_code=status.HTTP_202_ACCEPTED,
)
def put_hardware_doc(miner_key: str, payload: HardwareDocument) -> GenericOk:
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
    # HOST - host to bind (defaults to 0.0.0.0)
    # UVICORN_RELOAD - if set to '1' or 'true' (case-insensitive), enable reload
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    reload_env = os.getenv("UVICORN_RELOAD", "true").lower()
    reload_flag = reload_env in ("1", "true", "yes")

    uvicorn.run("app:app", host=host, port=port, reload=reload_flag)

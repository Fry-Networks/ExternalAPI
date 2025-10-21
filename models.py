from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from enum import Enum

from pydantic import BaseModel, Field


# Determine pydantic major version early so we can set the proper
# Config attribute at class creation time and avoid deprecation warnings.
_pyd_major = 0
try:
    import pydantic as _pyd
    _pyd_ver = getattr(_pyd, "__version__", "0")
    _pyd_major = int(str(_pyd_ver).split(".")[0])
except Exception:
    _pyd_major = 0


# Shared example used in VersionResponse Config
_VERSION_RESPONSE_EXAMPLE = {
    "required_version": "1.2.3",
    "note": "miner_code path param may be one of BM, HM, SM, XM (example list)."
}


# Examples for other key models
_MINER_PROFILE_EXAMPLE = {
    "miner_key": "miner-key-abc",
    "address": "1abcd...",
    "miner_type": "AEM",
    "credentials": {"mac_address": "AA:BB:CC:DD:EE:FF"},
    "credentials_saved_at": "2025-10-21T06:00:00Z",
    "position": {"lat": 35.0, "lng": -120.0, "hexId": "abc123"},
    "position_saved_at": "2025-10-21T06:05:00Z",
}


_INSTALLATION_HEARTBEAT_EXAMPLE = {
    "miner_key": "miner-key-abc",
    "install_id": "550e8400-e29b-41d4-a716-446655440000",
    "minerCode": "AEM",
    "version_installed": "1.2.3",
    "hostname": "miner-01",
    "os": "Debian 11",
    "last_seen_at": "2025-10-21T07:00:00Z",
    "is_installed": True,
    "version_needed": "1.3.0",
    "is_uptodate": False,
    "is_outdated": True,
}


_LEASE_RESPONSE_EXAMPLE = {
    "granted": True,
    "expires_at": "2025-10-21T07:15:00Z",
    "holder_install_id": "550e8400-e29b-41d4-a716-446655440000",
    "ttl_seconds": 900,
}


_LEASE_ACTION_EXAMPLE = {"lease_seconds": 900}


_HARDWARE_DOCUMENT_EXAMPLE = {"document": {"mac": "AA:BB:CC:DD:EE:FF", "serial": "SN123", "software": "1.2.3"}}


_HARDWARE_RESPONSE_EXAMPLE = {"document": {"mac": "AA:BB:CC:DD:EE:FF", "serial": "SN123", "software": "1.2.3"}}


class VersionResponse(BaseModel):
    """Response for the versions endpoint.

    Note: The `miner_code` path parameter identifies a miner family. Update the
    allowed codes below to match your deployment. The list here is illustrative
    and appears in the generated OpenAPI documentation for convenience.
    """
    required_version: Optional[str] = Field(
        default=None,
        description=(
            "Latest required miner version for the requested miner family.\n\n"
            "Allowed miner codes (replace with your actual codes):\n"
                    " - BM: Bandwidth Miner\n"
                    " - IDM: Indoor Decibel Miner\n"
                    " - ODM: Outdoor Decibel Miner\n"
                    " - ISM: Indoor Satellite Miner\n"
                    " - OSM: Outdoor Satellite Miner\n"
                    " - RDN: Reward Decentralization Node\n"
                    " - SDN: Storage Decentralization Node\n"
                    " - SVN: Storage Validator Node\n"
                    " - AEM: AI Edge Miner\n"
                    " - IRM: Indoor Radiation Miner\n\n"
                    "If you need to add or change miner codes, update the documentation here"
                    " to reflect your environment."
        ),
    )

    class Config:
        pass


# Attach the proper schema-example to the nested Config class to avoid
# Pydantic v2 deprecation warnings while keeping static typing happy.
if _pyd_major >= 2:
    setattr(VersionResponse.Config, "json_schema_extra", {"example": _VERSION_RESPONSE_EXAMPLE})
else:
    setattr(VersionResponse.Config, "schema_extra", {"example": _VERSION_RESPONSE_EXAMPLE})


class MinerCode(str, Enum):
    BM = "BM"
    IDM = "IDM"
    ODM = "ODM"
    ISM = "ISM"
    OSM = "OSM"
    RDN = "RDN"
    SDN = "SDN"
    SVN = "SVN"
    AEM = "AEM"
    IRM = "IRM"



class MinerProfileResponse(BaseModel):
    # Full device record fields
    miner_key: Optional[str] = Field(..., description="Full miner key")
    address: Optional[str] = Field(default=None, description="1stParty address string")
    miner_type: Optional[MinerCode] = Field(default=None, description="Miner family code (e.g. 'BM')")
    credentials: Optional[Dict[str, Any]] = Field(default=None, description="Credentials blob, e.g. {'mac_address': '...'}")
    credentials_saved_at: Optional[datetime] = Field(default=None, description="When credentials were saved")
    position: Optional[Dict[str, Any]] = Field(default=None, description="Position object with lat/lng/hexId")
    position_saved_at: Optional[datetime] = Field(default=None, description="When position was recorded")

    class Config:
        # Allow other fields so we can return the full document shape without failing validation
        extra = "allow"
        pass


class InstallationHeartbeat(BaseModel):
    """Payload sent by a miner installation to report heartbeat and status.

    Fields are intentionally permissive to allow forward-compatible additions
    from miner binaries.
    """
    miner_key: str = Field(..., description="Full miner key for this device")
    install_id: str = Field(..., description="Unique install instance id (UUID)")
    minerCode: Optional[MinerCode] = Field(default=None, description="Miner family code (optional)")
    version_installed: Optional[str] = Field(default=None, description="Current installed software version")
    hostname: Optional[str] = Field(default=None, description="Hostname reported by the miner")
    os: Optional[str] = Field(default=None, description="Operating system string reported by the miner")
    last_seen_at: Optional[datetime] = Field(default=None, description="ISO timestamp when heartbeat was sent")
    is_installed: Optional[bool] = Field(default=None, description="Whether the miner reports the software as installed")
    version_needed: Optional[str] = Field(default=None, description="Optional field hinting the version required")
    is_uptodate: Optional[bool] = Field(default=None, description="Whether the miner considers itself up-to-date")
    is_outdated: Optional[bool] = Field(default=None, description="Whether the miner considers itself outdated")
    class Config:
        extra = "allow"
        pass


class LeaseAction(BaseModel):
    lease_seconds: int = Field(default=900, ge=1, description="Lease duration in seconds")
    class Config:
        pass


class LeaseResponse(BaseModel):
    granted: bool = Field(..., description="Whether the lease was granted to the caller")
    expires_at: Optional[datetime] = Field(default=None, description="ISO expiry timestamp for the lease")
    holder_install_id: Optional[str] = Field(default=None, description="Install id currently holding the lease")
    ttl_seconds: Optional[int] = Field(default=None, description="Time-to-live in seconds for the active lease")
    class Config:
        pass


class HardwareDocument(BaseModel):
    """Generic hardware document payload. The document shape is flexible and
    may contain PoC or runtime metrics fields (mac, software, PoC/PoL fields, etc.).
    """
    document: Dict[str, Any] = Field(..., description="Arbitrary hardware document")
    class Config:
        pass


class HardwareResponse(BaseModel):
    document: Dict[str, Any] = Field(..., description="Stored hardware document returned from PoC database")
    class Config:
        pass


# Attach examples to other models' Configs in a Pydantic-version-aware way
_MODEL_EXAMPLES = [
    (LeaseAction, _LEASE_ACTION_EXAMPLE),
    (LeaseResponse, _LEASE_RESPONSE_EXAMPLE),
    (HardwareDocument, _HARDWARE_DOCUMENT_EXAMPLE),
    (HardwareResponse, _HARDWARE_RESPONSE_EXAMPLE),
    (InstallationHeartbeat, _INSTALLATION_HEARTBEAT_EXAMPLE),
    (MinerProfileResponse, _MINER_PROFILE_EXAMPLE),
]

for _model, _example in _MODEL_EXAMPLES:
    if _pyd_major >= 2:
        setattr(_model.Config, "json_schema_extra", {"example": _example})
    else:
        setattr(_model.Config, "schema_extra", {"example": _example})


class GenericOk(BaseModel):
    ok: bool = True

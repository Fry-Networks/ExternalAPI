from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class VersionResponse(BaseModel):
    required_version: Optional[str] = Field(default=None, description="Latest required miner version")


class MinerProfileResponse(BaseModel):
    # Full device record fields
    miner_key: Optional[str] = Field(..., description="Full miner key")
    address: Optional[str] = Field(default=None, description="1stParty address string")
    miner_type: Optional[str] = Field(default=None, description="Miner family code (e.g. 'BM')")
    credentials: Optional[Dict[str, Any]] = Field(default=None, description="Credentials blob, e.g. {'mac_address': '...'}")
    credentials_saved_at: Optional[datetime] = Field(default=None, description="When credentials were saved")
    position: Optional[Dict[str, Any]] = Field(default=None, description="Position object with lat/lng/hexId")
    position_saved_at: Optional[datetime] = Field(default=None, description="When position was recorded")

    class Config:
        # Allow other fields so we can return the full document shape without failing validation
        extra = "allow"


class InstallationHeartbeat(BaseModel):
    miner_key: str
    install_id: str
    minerCode: Optional[str] = None
    version_installed: Optional[str] = None
    hostname: Optional[str] = None
    os: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    is_installed: Optional[bool] = None
    version_needed: Optional[str] = None
    is_uptodate: Optional[bool] = None
    is_outdated: Optional[bool] = None
    class Config:
        extra = "allow"


class LeaseAction(BaseModel):
    lease_seconds: int = Field(default=900, ge=1, description="Lease duration in seconds")


class LeaseResponse(BaseModel):
    granted: bool
    expires_at: Optional[datetime] = None
    holder_install_id: Optional[str] = None
    ttl_seconds: Optional[int] = None


class HardwareDocument(BaseModel):
    document: Dict[str, Any]


class HardwareResponse(BaseModel):
    document: Dict[str, Any]


class GenericOk(BaseModel):
    ok: bool = True

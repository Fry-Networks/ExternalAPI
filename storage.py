from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple
import os
import logging

try:
    from pymongo import MongoClient
    from pymongo import ReturnDocument
    from pymongo.collection import Collection
except Exception:  # pragma: no cover - optional dependency
    MongoClient = None  # type: ignore
    class _RD:  # pragma: no cover - executed when pymongo missing
        AFTER = True

    ReturnDocument = _RD  # type: ignore

@dataclass
class InstallationRecord:
    miner_key: str
    install_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class LeaseRecord:
    miner_key: str
    holder_install_id: str
    expires_at: datetime
    last_seen_at: datetime
    history_payload: Dict[str, Any] = field(default_factory=dict)

    def ttl_seconds(self) -> int:
        return max(0, int((self.expires_at - datetime.now(timezone.utc)).total_seconds()))


class InMemoryStore:
    """Thread-safe in-memory backing store for the external API."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._versions: Dict[str, Dict[str, Optional[str]]] = {}
        self._miner_profiles: Dict[str, Dict[str, Any]] = {}
        self._installations: Dict[Tuple[str, str], InstallationRecord] = {}
        self._leases: Dict[str, LeaseRecord] = {}
        self._hardware_docs: Dict[str, Dict[str, Any]] = {}
        self._measurements: Dict[str, Dict[str, Any]] = {}  # keyed by hex_id
        self._mysterium: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Version metadata
    def get_required_version(self, miner_code: str, platform: str = "windows") -> Optional[Dict[str, Optional[str]]]:
        normalized = (platform or "").strip().lower()
        if normalized not in ("windows", "linux"):
            raise ValueError("Unsupported platform")

        with self._lock:
            data = self._versions.get(miner_code.upper())
            if data is None:
                return None

            return self._select_versions_by_platform(data, normalized)

    def set_required_version(self, miner_code: str, software_version: Optional[str] = None, poc_version: Optional[str] = None) -> None:
        with self._lock:
            self._versions[miner_code.upper()] = {
                "software_version": software_version,
                "poc_version": poc_version
            }

    def list_supported_installers(self, os_name: str) -> List[str]:
        """Return miner codes that have installer metadata for the requested OS."""
        normalized = (os_name or "").strip().lower()
        if not normalized:
            raise ValueError("OS must be provided")

        if normalized == "linux":
            target_fields = ("linux_software_version", "linux_software_version_needed")
        else:
            target_fields = ("software_version", "software_version_needed")

        with self._lock:
            supported: List[str] = []
            for code, metadata in self._versions.items():
                if any(self._has_installer(metadata, field) for field in target_fields):
                    supported.append(code)

        return sorted(supported)

    @staticmethod
    def _has_installer(metadata: Dict[str, Any], field: str) -> bool:
        value = metadata.get(field)
        return value is not None and value != ""

    @staticmethod
    def _select_versions_by_platform(metadata: Dict[str, Any], platform: str) -> Dict[str, Optional[str]]:
        """Select software/poc versions for requested platform. Returns {} when unavailable."""
        if platform == "linux":
            software_fields = ("linux_software_version", "linux_software_version_needed")
            poc_fields = ("linux_poc_version", "linux_poc_version_needed")
        else:
            software_fields = ("software_version", "software_version_needed")
            poc_fields = ("poc_version", "poc_version_needed")

        software = InMemoryStore._first_non_empty(metadata, software_fields)
        poc = InMemoryStore._first_non_empty(metadata, poc_fields)

        # For Linux-capable miners, allow PoC to fall back to general PoC requirement.
        if platform == "linux" and software:
            poc = poc or InMemoryStore._first_non_empty(metadata, ("poc_version", "poc_version_needed"))

        result: Dict[str, Optional[str]] = {}
        if software:
            result["software_version"] = software
        if poc:
            result["poc_version"] = poc
        return result

    @staticmethod
    def _first_non_empty(metadata: Dict[str, Any], fields: Tuple[str, ...]) -> Optional[str]:
        for field in fields:
            value = metadata.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    # ------------------------------------------------------------------
    # Miner credentials
    def get_miner_profile(self, miner_key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._miner_profiles.get(miner_key, {"exists": False}))

    def set_miner_profile(self, miner_key: str, **fields: Any) -> None:
        with self._lock:
            profile = self._miner_profiles.setdefault(miner_key, {"exists": False})
            profile.update(fields)
            profile["exists"] = True

    # ------------------------------------------------------------------
    # Installation heartbeats
    def upsert_installation(self, miner_key: str, install_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            rec = InstallationRecord(miner_key=miner_key, install_id=install_id, payload=dict(payload))
            self._installations[(miner_key, install_id)] = rec

    def delete_installation(self, miner_key: str, install_id: str) -> bool:
        """Delete an installation record. Returns True if deleted, False if not found."""
        with self._lock:
            key = (miner_key, install_id)
            if key in self._installations:
                del self._installations[key]
                # Also clean up any lease for this installation
                if miner_key in self._leases:
                    lease = self._leases[miner_key]
                    if lease.holder_install_id == install_id:
                        del self._leases[miner_key]
                return True
            return False

    # ------------------------------------------------------------------
    # Leases
    def acquire_lease(self, miner_key: str, install_id: str, lease_seconds: int) -> Tuple[bool, LeaseRecord]:
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(seconds=max(lease_seconds, 1))
        with self._lock:
            current = self._leases.get(miner_key)
            if current:
                if current.holder_install_id != install_id and current.expires_at > now:
                    return False, current
            record = LeaseRecord(
                miner_key=miner_key,
                holder_install_id=install_id,
                expires_at=expiry,
                last_seen_at=now,
            )
            self._leases[miner_key] = record
            return True, record

    def renew_lease(self, miner_key: str, install_id: str, lease_seconds: int) -> Tuple[bool, Optional[LeaseRecord]]:
        now = datetime.now(timezone.utc)
        with self._lock:
            current = self._leases.get(miner_key)
            if not current or current.holder_install_id != install_id:
                return False, current
            current.expires_at = now + timedelta(seconds=max(lease_seconds, 1))
            current.last_seen_at = now
            return True, current

    def lease_status(self, miner_key: str) -> Dict[str, Any]:
        with self._lock:
            record = self._leases.get(miner_key)
            if not record:
                return {"active": False, "holder_install_id": None, "expires_at": None, "ttl_seconds": 0}
            return {
                "active": record.expires_at > datetime.now(timezone.utc),
                "holder_install_id": record.holder_install_id,
                "expires_at": record.expires_at.isoformat(),
                "ttl_seconds": record.ttl_seconds(),
            }

    # ------------------------------------------------------------------
    # Hardware aggregates
    def get_hardware_doc(self, miner_key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._hardware_docs.get(miner_key, {}))

    def put_hardware_doc(self, miner_key: str, document: Dict[str, Any]) -> None:
        with self._lock:
            self._hardware_docs[miner_key] = dict(document)

    # ------------------------------------------------------------------
    # Measurements
    def upload_measurement(self, hex_id: str, miner_code: str, install_id: str, timestamp: str, measurement_type: str, value: Dict[str, Any]) -> None:
        """Store a measurement in the hex-centric structure (in-memory version).
        
        Appends to the measurement_type array within the hex document.
        """
        with self._lock:
            # Get or create hex document
            if hex_id not in self._measurements:
                self._measurements[hex_id] = {"hex_id": hex_id}
            
            doc = self._measurements[hex_id]
            
            # Parse timestamp
            ts_dt: Optional[datetime] = None
            try:
                ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except Exception:
                ts_dt = None
            
            # Create measurement entry
            entry = {
                "timestamp": timestamp,
                "timestamp_dt": ts_dt,
                "miner_code": miner_code,
                "install_id": install_id,
                "value": dict(value),
                "uploaded_at": datetime.now(timezone.utc),
            }
            
            # Append to measurement type array
            if measurement_type not in doc:
                doc[measurement_type] = []
            doc[measurement_type].append(entry)

    def get_measurements(self, hex_id: str, start: Optional[datetime], end: Optional[datetime], measurement_types: Optional[List[str]]) -> Dict[str, Any]:
        """Get all measurements for a hex, optionally filtered by date range and measurement types."""
        with self._lock:
            doc = self._measurements.get(hex_id)
            if not doc:
                return {"hex_id": hex_id}
            
            result: Dict[str, Any] = {"hex_id": hex_id}
            
            # Process each measurement type array
            for key, value in doc.items():
                if key == "hex_id":
                    continue
                # Skip if measurement_types filter is set and this type isn't in it
                if measurement_types and key not in measurement_types:
                    continue
                # Filter array by date range if specified
                if isinstance(value, list):
                    filtered = []
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        ts_dt = item.get("timestamp_dt")
                        if start and (not isinstance(ts_dt, datetime) or ts_dt < start):
                            continue
                        if end and (not isinstance(ts_dt, datetime) or ts_dt > end):
                            continue
                        # Remove internal fields for cleaner response
                        clean_item = {k: v for k, v in item.items() if k not in ("timestamp_dt", "uploaded_at")}
                        filtered.append(clean_item)
                    if filtered:
                        result[key] = filtered
            
            return result

    # ------------------------------------------------------------------
    # Mysterium keystores
    def upsert_mysterium_keystore(self, miner_key: str, keystore_b64: str, identity_id: Optional[str]) -> Dict[str, Any]:
        """Store or update a Mysterium keystore blob for a miner key."""
        with self._lock:
            record = {
                "miner_key": miner_key,
                "keystore_b64": keystore_b64,
                "identity_id": identity_id,
                "updated_at": datetime.now(timezone.utc),
            }
            self._mysterium[miner_key] = record
            return dict(record)

    def get_mysterium_keystore(self, miner_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._mysterium.get(miner_key)
            return dict(record) if record else None


class MongoStore:
    """MongoDB-backed store. Uses environment variables:
    - MONGODB_URI (mongodb connection string)
    
    Uses fixed database names: 'PoC' for installations/hardware, 'creds' for credentials.
    Requires MONGODB_URI environment variable and pymongo to be installed.
    """
    def __init__(self) -> None:
        self._lock = RLock()
        uri = os.environ.get("MONGODB_URI")
        
        if MongoClient is None:
            raise ImportError("pymongo is required for MongoStore. Install with: pip install pymongo")
        
        if not uri:
            raise ValueError("MONGODB_URI environment variable is required for MongoStore")
            
        # No fallback - proceed with MongoDB initialization

        self._client = MongoClient(uri, serverSelectionTimeoutMS=5000)

        # Always use PoC database for versions, installations, and hardware
        poc_db = self._client.get_database("PoC")
        self._versions: Collection = poc_db.get_collection("versions")
        self._installations: Collection = poc_db.get_collection("installations")
        self._hardware_docs: Collection = poc_db.get_collection("hardware")
        
        # Measurements: standard collection with hex_id as primary key
        # Each document contains all measurement types for that hex
        self._measurements: Collection = poc_db.get_collection("measurements")
        self._mysterium: Collection = poc_db.get_collection("mysterium")
        
        try:
            # Index for efficient queries by hex_id
            self._measurements.create_index([("hex_id", 1)], unique=True)
            self._mysterium.create_index([("miner_key", 1)], unique=True)
        except Exception:
            # Non-fatal if index creation fails
            pass

        # Always use creds database for miner profiles/credentials
        creds_db = self._client.get_database("creds")
        self._miner_profiles: Collection = creds_db.get_collection("hardware")



    # No fallback mechanism - MongoDB is required

    # Version metadata
    def get_required_version(self, miner_code: str, platform: str = "windows") -> Optional[Dict[str, Optional[str]]]:
        normalized = (platform or "").strip().lower()
        if normalized not in ("windows", "linux"):
            raise ValueError("Unsupported platform")

        doc = self._versions.find_one({"miner_code": miner_code})
        if doc is None:
            return None

        return self._select_versions_by_platform(doc, normalized)

    def set_required_version(self, miner_code: str, software_version: Optional[str] = None, poc_version: Optional[str] = None) -> None:
        update_fields = {"miner_code": miner_code}
        if software_version is not None:
            update_fields["software_version_needed"] = software_version
        if poc_version is not None:
            update_fields["poc_version_needed"] = poc_version
        self._versions.update_one({"miner_code": miner_code}, {"$set": update_fields}, upsert=True)

    def list_supported_installers(self, os_name: str) -> List[str]:
        """Query versions collection for miner codes with installer metadata."""
        normalized = (os_name or "").strip().lower()
        if not normalized:
            raise ValueError("OS must be provided")

        if normalized == "linux":
            target_fields = ["linux_software_version", "linux_software_version_needed"]
        else:
            target_fields = ["software_version", "software_version_needed"]

        or_clauses = [
            {field: {"$exists": True, "$nin": [None, ""]}}
            for field in target_fields
        ]

        query = {"$or": or_clauses} if len(or_clauses) > 1 else or_clauses[0]

        cursor = self._versions.find(query, {"_id": 0, "miner_code": 1})
        codes = {doc.get("miner_code") for doc in cursor if doc.get("miner_code")}

        return sorted(codes)

    @staticmethod
    def _select_versions_by_platform(doc: Dict[str, Any], platform: str) -> Dict[str, Optional[str]]:
        if platform == "linux":
            software_fields = ("linux_software_version", "linux_software_version_needed")
            poc_fields = ("linux_poc_version", "linux_poc_version_needed")
        else:
            software_fields = ("software_version", "software_version_needed")
            poc_fields = ("poc_version", "poc_version_needed")

        software = MongoStore._first_non_empty(doc, software_fields)
        poc = MongoStore._first_non_empty(doc, poc_fields)

        if platform == "linux" and software:
            poc = poc or MongoStore._first_non_empty(doc, ("poc_version", "poc_version_needed"))

        result: Dict[str, Optional[str]] = {}
        if software:
            result["software_version"] = software
        if poc:
            result["poc_version"] = poc
        return result

    @staticmethod
    def _first_non_empty(doc: Dict[str, Any], fields: Tuple[str, ...]) -> Optional[str]:
        for field in fields:
            value = doc.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    # Miner profiles - always use creds.hardware collection
    def get_miner_profile(self, miner_key: str) -> Dict[str, Any]:
        try:
            # First try miner_profiles collection
            doc = self._miner_profiles.find_one({"miner_key": miner_key}) or self._miner_profiles.find_one({"minerKey": miner_key})
            if doc:
                doc = dict(doc)
                doc.pop("_id", None)
                doc.setdefault("exists", True)
                return doc
            
            # If not found, try installations collection
            doc = self._installations.find_one({"miner_key": miner_key})
            if doc:
                doc = dict(doc)
                doc.pop("_id", None)
                doc.setdefault("exists", True)
                return doc
            
            # If not found, try hardware collection
            doc = self._hardware_docs.find_one({"miner_key": miner_key}) or self._hardware_docs.find_one({"minerKey": miner_key})
            if doc:
                doc = dict(doc)
                doc.pop("_id", None)
                doc.setdefault("exists", True)
                return doc
        except Exception:
            pass

        return {"exists": False}

    def set_miner_profile(self, miner_key: str, **fields: Any) -> None:
        payload = dict(fields)
        payload.setdefault("exists", True)
        payload["miner_key"] = miner_key
        self._miner_profiles.update_one({"miner_key": miner_key}, {"$set": payload}, upsert=True)

    # Installations
    def upsert_installation(self, miner_key: str, install_id: str, payload: Dict[str, Any]) -> None:
        key = {"miner_key": miner_key, "install_id": install_id}
        payload_copy = dict(payload)
        now = datetime.now(timezone.utc)
        # Map incoming fields to your existing installation document shape
        set_on_insert = {
            "first_installed_at": now,
            "_lease": False,
        }
        update_doc: Dict[str, Any] = {"$set": {}, "$setOnInsert": set_on_insert}
        
        # Extract version fields directly
        software_version = payload_copy.get("software_version_installed")
        poc_version = payload_copy.get("poc_version_installed")
        software_needed = payload_copy.get("software_version_needed")
        poc_needed = payload_copy.get("poc_version_needed")
        
        # canonical fields
        update_doc["$set"].update({
            "miner_key": miner_key,
            "install_id": install_id,
            "last_seen_at": payload_copy.get("last_seen_at") or now,
            "hostname": payload_copy.get("hostname"),
            "os": payload_copy.get("os"),
            "software_version_installed": software_version,
            "poc_version_installed": poc_version,
            "software_version_needed": software_needed,
            "poc_version_needed": poc_needed,
            "is_installed": payload_copy.get("is_installed", True),
            "minerCode": payload_copy.get("minerCode"),
        })
        # merge any other keys from payload (keep schema flexible)
        for k, v in payload_copy.items():
            if k not in ("miner_key", "install_id"):
                update_doc["$set"][k] = v
        # Prevent overwriting first_installed_at
        if "first_installed_at" in update_doc["$set"]:
            del update_doc["$set"]["first_installed_at"]
        # Remove any keys that would update subpaths of first_installed_at
        to_del = [k for k in list(update_doc["$set"].keys()) if k.startswith("first_installed_at.")]
        for k in to_del:
            del update_doc["$set"][k]

        self._installations.update_one(key, update_doc, upsert=True)

    def delete_installation(self, miner_key: str, install_id: str) -> bool:
        """Delete an installation record. Returns True if deleted, False if not found."""
        result = self._installations.delete_one({"miner_key": miner_key, "install_id": install_id})
        return result.deleted_count > 0

    # Leases
    def acquire_lease(self, miner_key: str, install_id: str, lease_seconds: int) -> Tuple[bool, LeaseRecord]:
        # Single atomic conditional find_one_and_update that grants a lease only when:
        # - no active lease exists (lease_expires_at missing or <= now)
        # - the document is for this install_id
        # This eliminates the race window between check and update.
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(seconds=max(lease_seconds, 1))
        
        # Prepare filter for the atomic operation
        # The $or condition ensures we only update when:
        # 1. This install_id already holds the lease (renew/extend), OR
        # 2. No lease exists or lease is expired
        filter_q = {
            "miner_key": miner_key,
            "install_id": install_id,
            "$or": [
                {"lease_install_id": install_id},  # This install already holds it
                {"lease_expires_at": {"$lte": now}},  # Expired
                {"lease_expires_at": {"$exists": False}}  # No lease
            ]
        }
        
        update_doc = {
            "$set": {
                "lease_expires_at": expiry,
                "lease_install_id": install_id,
                "last_seen_at": now,
                "_lease": True,
            },
            "$setOnInsert": {"first_installed_at": now},
        }
        
        try:
            # Atomic conditional update: only succeeds if no active lease or lease expired
            updated = self._installations.find_one_and_update(
                filter_q, 
                update_doc, 
                upsert=True, 
                return_document=ReturnDocument.AFTER
            )
            
            if updated:
                print(f"[MongoStore] acquire_lease: atomic grant succeeded for {miner_key}/{install_id}")
                
                expires_val = updated.get("lease_expires_at")
                last_seen = updated.get("last_seen_at", now)
                return True, LeaseRecord(miner_key=miner_key, holder_install_id=install_id, expires_at=expires_val, last_seen_at=last_seen)
            
            # If updated is None, it means the condition failed (another active lease exists)
            print(f"[MongoStore] acquire_lease: atomic grant DENIED (active lease exists) for {miner_key}/{install_id}")
            # Find who currently holds the lease
            current_holder = self._installations.find_one(
                {"miner_key": miner_key, "lease_expires_at": {"$gt": now}}
            )
            if current_holder:
                rec = LeaseRecord(
                    miner_key=miner_key, 
                    holder_install_id=current_holder.get("lease_install_id", "unknown"),
                    expires_at=current_holder.get("lease_expires_at"),
                    last_seen_at=current_holder.get("last_seen_at", now)
                )
                return False, rec
            
            # Edge case: no document returned but no active holder found either
            return False, LeaseRecord(miner_key=miner_key, holder_install_id="unknown", expires_at=now, last_seen_at=now)
            
        except Exception as e:
            # On any error, fall back to best-effort non-atomic path
            print(f"[MongoStore] acquire_lease: FALLBACK to non-atomic (error: {e}) for {miner_key}/{install_id}")
            fallback_filter = {"miner_key": miner_key, "install_id": install_id}
            try:
                self._installations.update_one(
                    fallback_filter, 
                    {"$set": {
                        "lease_expires_at": expiry, 
                        "lease_install_id": install_id, 
                        "last_seen_at": now, 
                        "_lease": True
                    }}, 
                    upsert=True
                )
            except Exception:
                pass
            return True, LeaseRecord(miner_key=miner_key, holder_install_id=install_id, expires_at=expiry, last_seen_at=now)

    def renew_lease(self, miner_key: str, install_id: str, lease_seconds: int) -> Tuple[bool, Optional[LeaseRecord]]:
        now = datetime.now(timezone.utc)
        new_expiry = now + timedelta(seconds=max(lease_seconds, 1))
        
        # Atomic conditional renewal: only succeeds if this install_id currently holds the lease
        filter_q = {
            "miner_key": miner_key, 
            "install_id": install_id, 
            "lease_install_id": install_id
        }
        
        try:
            updated = self._installations.find_one_and_update(
                filter_q, 
                {"$set": {"lease_expires_at": new_expiry, "last_seen_at": now}}, 
                return_document=ReturnDocument.AFTER
            )
            
            if updated:
                print(f"[MongoStore] renew_lease: atomic renewal succeeded for {miner_key}/{install_id}")
                return True, LeaseRecord(miner_key=miner_key, holder_install_id=install_id, expires_at=new_expiry, last_seen_at=now)
            
            # Atomic renewal failed - not the current holder or doc missing
            print(f"[MongoStore] renew_lease: atomic renewal DENIED (not holder) for {miner_key}/{install_id}")
            current = self._installations.find_one({"miner_key": miner_key, "install_id": install_id})
            if current and current.get("lease_expires_at"):
                rec = LeaseRecord(
                    miner_key=miner_key, 
                    holder_install_id=current.get("lease_install_id", "unknown"), 
                    expires_at=current.get("lease_expires_at"), 
                    last_seen_at=current.get("last_seen_at", now)
                )
                return False, rec
            return False, None
            
        except Exception as e:
            # Fallback non-atomic path
            print(f"[MongoStore] renew_lease: FALLBACK to non-atomic (error: {e}) for {miner_key}/{install_id}")
            current = self._installations.find_one({"miner_key": miner_key, "install_id": install_id})
            if not current or current.get("lease_install_id") != install_id:
                rec = None
                if current and current.get("lease_expires_at"):
                    rec = LeaseRecord(
                        miner_key=miner_key, 
                        holder_install_id=current.get("lease_install_id", "unknown"), 
                        expires_at=current.get("lease_expires_at"), 
                        last_seen_at=current.get("last_seen_at", now)
                    )
                return False, rec
            self._installations.update_one(
                {"miner_key": miner_key, "install_id": install_id}, 
                {"$set": {"lease_expires_at": new_expiry, "last_seen_at": now}}
            )
            return True, LeaseRecord(miner_key=miner_key, holder_install_id=install_id, expires_at=new_expiry, last_seen_at=now)

    def lease_status(self, miner_key: str) -> Dict[str, Any]:
        # find any installation doc for this miner with a lease that hasn't expired
        now = datetime.now(timezone.utc)
        rec = self._installations.find_one({"miner_key": miner_key, "lease_expires_at": {"$gt": now}})
        if rec:
            # Active lease found
            expires_at = rec.get("lease_expires_at")
            expires_dt = self._normalize_datetime(expires_at)
            ttl = int((expires_dt - now).total_seconds()) if expires_dt else 0
            expires_iso = expires_dt.isoformat() if isinstance(expires_dt, datetime) else None
            return {"active": True, "holder_install_id": rec.get("lease_install_id"), "expires_at": expires_iso, "ttl_seconds": ttl}
        
        # No active lease found - check for the most recent expired lease
        cursor = self._installations.find(
            {"miner_key": miner_key, "lease_expires_at": {"$exists": True}}
        ).sort("lease_expires_at", -1).limit(1)
        
        rec = next(cursor, None)
        if rec:
            # Return info about the expired lease
            expires_at = rec.get("lease_expires_at")
            expires_dt = self._normalize_datetime(expires_at)
            ttl = int((expires_dt - now).total_seconds()) if expires_dt else 0
            expires_iso = expires_dt.isoformat() if isinstance(expires_dt, datetime) else None
            return {"active": False, "holder_install_id": rec.get("lease_install_id"), "expires_at": expires_iso, "ttl_seconds": ttl}
        
        # No lease found at all
        return {"active": False, "holder_install_id": None, "expires_at": None, "ttl_seconds": 0}

    def _normalize_datetime(self, dt_value) -> Optional[datetime]:
        """Normalize datetime value to UTC datetime object."""
        if isinstance(dt_value, str):
            try:
                return datetime.fromisoformat(dt_value.replace('Z', '+00:00'))
            except Exception:
                return None
        elif isinstance(dt_value, datetime):
            dt = dt_value
        else:
            return None
        
        # If naive, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    # Hardware aggregates
    def get_hardware_doc(self, miner_key: str) -> Dict[str, Any]:
        # Read from PoC.hardware (runtime data with mac, software, PoC, PoL fields).
        # This is the collection that write_status updates via replace_one.
        doc = None
        try:
            doc = self._hardware_docs.find_one({"miner_key": miner_key}) or self._hardware_docs.find_one({"minerKey": miner_key})
        except Exception:
            doc = None
        if not doc:
            return {}
        doc = dict(doc)
        doc.pop("_id", None)
        return doc

    def put_hardware_doc(self, miner_key: str, document: Dict[str, Any]) -> None:
        doc = dict(document)
        doc["miner_key"] = miner_key
        self._hardware_docs.update_one({"miner_key": miner_key}, {"$set": doc}, upsert=True)

    # Measurements
    def upload_measurement(self, hex_id: str, miner_code: str, install_id: str, timestamp: str, measurement_type: str, value: Dict[str, Any]) -> None:
        """Append measurement to hex document, organized by measurement_type.
        
        Each hex document contains arrays of measurements grouped by type:
        {
          "hex_id": "...",
          "bandwidth": [{timestamp, miner_code, install_id, value}, ...],
          "satellite": [{timestamp, miner_code, install_id, value}, ...],
          ...
        }
        """
        now = datetime.now(timezone.utc)
        # Parse timestamp to datetime for sorting/filtering
        ts_dt = None
        try:
            ts_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except Exception:
            ts_dt = now
        
        # Prepare the measurement entry
        entry = {
            "timestamp": timestamp,
            "timestamp_dt": ts_dt,
            "miner_code": miner_code,
            "install_id": install_id,
            "value": dict(value),
            "uploaded_at": now,
        }
        
        # Use $push to append to the measurement_type array
        self._measurements.update_one(
            {"hex_id": hex_id},
            {
                "$set": {"hex_id": hex_id},
                "$push": {measurement_type: entry}
            },
            upsert=True
        )

    def get_measurements(self, hex_id: str, start: Optional[datetime], end: Optional[datetime], measurement_types: Optional[List[str]]) -> Dict[str, Any]:
        """Get all measurements for a hex, optionally filtered by date range and measurement types.
        
        Returns a document with hex_id and arrays of measurements by type.
        If date filtering is requested, filters each array by timestamp_dt.
        """
        doc = self._measurements.find_one({"hex_id": hex_id})
        if not doc:
            return {"hex_id": hex_id}
        
        doc.pop("_id", None)
        result: Dict[str, Any] = {"hex_id": hex_id}
        
        # Process each measurement type array
        for key, value in doc.items():
            if key == "hex_id":
                continue
            # Skip if measurement_types filter is set and this type isn't in it
            if measurement_types and key not in measurement_types:
                continue
            # Filter array by date range if specified
            if isinstance(value, list):
                filtered = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    ts_dt = item.get("timestamp_dt")
                    if start and (not isinstance(ts_dt, datetime) or ts_dt < start):
                        continue
                    if end and (not isinstance(ts_dt, datetime) or ts_dt > end):
                        continue
                    # Remove internal fields for cleaner response
                    clean_item = {k: v for k, v in item.items() if k not in ("timestamp_dt", "uploaded_at")}
                    filtered.append(clean_item)
                if filtered:
                    result[key] = filtered
        
        return result

    # Mysterium keystores
    def upsert_mysterium_keystore(self, miner_key: str, keystore_b64: str, identity_id: Optional[str]) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        payload = {
            "miner_key": miner_key,
            "keystore_b64": keystore_b64,
            "identity_id": identity_id,
            "updated_at": now,
        }
        self._mysterium.update_one(
            {"miner_key": miner_key},
            {"$set": payload},
            upsert=True,
        )
        return dict(payload)

    def get_mysterium_keystore(self, miner_key: str) -> Optional[Dict[str, Any]]:
        doc = self._mysterium.find_one({"miner_key": miner_key})
        if not doc:
            return None
        doc = dict(doc)
        doc.pop("_id", None)
        return doc


# MongoDB store is required - no fallback
def _create_store() -> MongoStore:
    logger = logging.getLogger("ExternalAPI")
    logger.info("Initializing MongoStore (MONGODB_URI required)")
    store = MongoStore()
    
    # Test MongoDB connectivity
    try:
        client = getattr(store, "_client", None)
        if client is not None:
            client.server_info()
        logger.info("Using MongoStore - MongoDB connection successful")
        return store
    except Exception as e:
        raise ConnectionError(f"MongoDB connection failed. Check MONGODB_URI and ensure MongoDB is running. Error: {e}")


STORE = _create_store()

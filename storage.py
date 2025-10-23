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
        self._versions: Dict[str, str] = {}
        self._miner_profiles: Dict[str, Dict[str, Any]] = {}
        self._installations: Dict[Tuple[str, str], InstallationRecord] = {}
        self._leases: Dict[str, LeaseRecord] = {}
        self._hardware_docs: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Version metadata
    def get_required_version(self, miner_code: str) -> Optional[str]:
        with self._lock:
            return self._versions.get(miner_code.upper())

    def set_required_version(self, miner_code: str, version: str) -> None:
        with self._lock:
            self._versions[miner_code.upper()] = version

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

        # Always use creds database for miner profiles/credentials
        creds_db = self._client.get_database("creds")
        self._miner_profiles: Collection = creds_db.get_collection("hardware")



    # No fallback mechanism - MongoDB is required

    # Version metadata
    def get_required_version(self, miner_code: str) -> Optional[str]:
        doc = self._versions.find_one({"miner_code": miner_code})
        return doc.get("software_version_needed") if doc else None

    def set_required_version(self, miner_code: str, version: str) -> None:
        self._versions.update_one({"miner_code": miner_code}, {"$set": {"software_version_needed": version, "miner_code": miner_code}}, upsert=True)

    # Miner profiles - always use creds.hardware collection
    def get_miner_profile(self, miner_key: str) -> Dict[str, Any]:
        try:
            doc = self._miner_profiles.find_one({"miner_key": miner_key}) or self._miner_profiles.find_one({"minerKey": miner_key})
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
        # canonical fields
        update_doc["$set"].update({
            "miner_key": miner_key,
            "install_id": install_id,
            "last_seen_at": payload_copy.get("last_seen_at") or now,
            "hostname": payload_copy.get("hostname"),
            "os": payload_copy.get("os"),
            "version_installed": payload_copy.get("version_installed") or payload_copy.get("version"),
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
        if not rec:
            return {"active": False, "holder_install_id": None, "expires_at": None, "ttl_seconds": 0}
        expires_at = rec.get("lease_expires_at")
        # Normalize expires_at: support datetime (naive or aware) or ISO string.
        expires_dt = None
        try:
            if isinstance(expires_at, str):
                # parse ISO format string
                try:
                    expires_dt = datetime.fromisoformat(expires_at)
                except Exception:
                    expires_dt = None
            elif isinstance(expires_at, datetime):
                expires_dt = expires_at
        except Exception:
            expires_dt = None

        # If we have a naive datetime, assume UTC
        if isinstance(expires_dt, datetime) and expires_dt.tzinfo is None:
            try:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        ttl = max(0, int((expires_dt - now).total_seconds())) if expires_dt else 0
        expires_iso = expires_dt.isoformat() if isinstance(expires_dt, datetime) else None
        return {"active": True, "holder_install_id": rec.get("lease_install_id"), "expires_at": expires_iso, "ttl_seconds": ttl}

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

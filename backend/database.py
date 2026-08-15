import os
import uuid
import logging
from dotenv import load_dotenv
from typing import Optional, Dict, Any, List

load_dotenv()

logger = logging.getLogger("codeoracle.database")

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "codeorbit")

_client = None
_db = None
_mongo_available = False


class InMemoryJobsCollection:
    """In-memory fallback collection conforming to PyMongo interface."""

    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def insert_one(self, doc: Dict[str, Any]):
        doc_id = doc.get("_id")
        if not doc_id:
            doc_id = str(uuid.uuid4())
            doc["_id"] = doc_id
        self._store[str(doc_id)] = dict(doc)
        return type("InsertResult", (), {"inserted_id": doc_id})()

    def find_one(self, filter_dict: Dict[str, Any]):
        if not filter_dict:
            return next(iter(self._store.values()), None)
        if "_id" in filter_dict:
            doc_id = str(filter_dict["_id"])
            return self._store.get(doc_id)
        for doc in self._store.values():
            if all(doc.get(k) == v for k, v in filter_dict.items()):
                return doc
        return None

    def update_one(self, filter_dict: Dict[str, Any], update_dict: Dict[str, Any], upsert: bool = False):
        doc_id = filter_dict.get("_id")
        if doc_id is not None:
            doc_id = str(doc_id)
        else:
            for k, doc in self._store.items():
                if all(doc.get(fk) == fv for fk, fv in filter_dict.items()):
                    doc_id = k
                    break

        doc = self._store.get(doc_id) if doc_id else None
        if not doc:
            if upsert:
                doc = {"_id": doc_id or str(uuid.uuid4())}
                doc_id = doc["_id"]
                self._store[str(doc_id)] = doc
            else:
                return type("UpdateResult", (), {"matched_count": 0, "modified_count": 0})()

        if "$set" in update_dict:
            for k, v in update_dict["$set"].items():
                doc[k] = v
        else:
            for k, v in update_dict.items():
                if not k.startswith("$"):
                    doc[k] = v

        self._store[str(doc_id)] = doc
        return type("UpdateResult", (), {"matched_count": 1, "modified_count": 1})()

    def find(self, filter_dict: Optional[Dict[str, Any]] = None):
        if not filter_dict:
            return list(self._store.values())
        results = []
        for doc in self._store.values():
            if all(doc.get(k) == v for k, v in filter_dict.items()):
                results.append(doc)
        return results

    def delete_one(self, filter_dict: Dict[str, Any]):
        doc_id = filter_dict.get("_id")
        if doc_id and str(doc_id) in self._store:
            del self._store[str(doc_id)]
            return type("DeleteResult", (), {"deleted_count": 1})()
        for k, doc in list(self._store.items()):
            if all(doc.get(fk) == fv for fk, fv in filter_dict.items()):
                del self._store[k]
                return type("DeleteResult", (), {"deleted_count": 1})()
        return type("DeleteResult", (), {"deleted_count": 0})()


class SafeJobsCollection:
    """Wrapper that tries MongoDB first, with automatic transparent fallback to in-memory store."""

    def __init__(self):
        self._memory = InMemoryJobsCollection()

    def _get_mongo_col(self):
        global _mongo_available
        if not _mongo_available or not MONGODB_URI:
            return None
        try:
            db = get_db()
            if db is not None:
                return db["jobs"]
        except Exception as e:
            logger.warning(f"MongoDB access error, disabling remote MongoDB: {e}")
            _mongo_available = False
        return None

    def insert_one(self, doc: Dict[str, Any]):
        # Always store in memory
        res = self._memory.insert_one(doc)
        # Attempt MongoDB
        col = self._get_mongo_col()
        if col is not None:
            try:
                col.insert_one(doc)
            except Exception as e:
                logger.warning(f"MongoDB insert_one failed, using memory store: {e}")
        return res

    def find_one(self, filter_dict: Dict[str, Any]):
        col = self._get_mongo_col()
        if col is not None:
            try:
                doc = col.find_one(filter_dict)
                if doc is not None:
                    return doc
            except Exception as e:
                logger.warning(f"MongoDB find_one failed, checking memory store: {e}")
        return self._memory.find_one(filter_dict)

    def update_one(self, filter_dict: Dict[str, Any], update_dict: Dict[str, Any], upsert: bool = False):
        res = self._memory.update_one(filter_dict, update_dict, upsert=upsert)
        col = self._get_mongo_col()
        if col is not None:
            try:
                col.update_one(filter_dict, update_dict, upsert=upsert)
            except Exception as e:
                logger.warning(f"MongoDB update_one failed, memory store updated: {e}")
        return res

    def find(self, filter_dict: Optional[Dict[str, Any]] = None):
        col = self._get_mongo_col()
        if col is not None:
            try:
                return list(col.find(filter_dict or {}))
            except Exception as e:
                logger.warning(f"MongoDB find failed, returning memory store: {e}")
        return self._memory.find(filter_dict)

    def delete_one(self, filter_dict: Dict[str, Any]):
        res = self._memory.delete_one(filter_dict)
        col = self._get_mongo_col()
        if col is not None:
            try:
                col.delete_one(filter_dict)
            except Exception as e:
                logger.warning(f"MongoDB delete_one failed: {e}")
        return res


_safe_collection = SafeJobsCollection()


def get_client():
    """Get or create MongoDB client singleton."""
    global _client, _mongo_available
    if _client is None and MONGODB_URI:
        try:
            from pymongo import MongoClient
            _client = MongoClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=2000,
                connectTimeoutMS=2000,
                socketTimeoutMS=2000,
                tlsAllowInvalidCertificates=True
            )
            # Fast ping check
            _client.admin.command('ping')
            _mongo_available = True
            logger.info("Connected successfully to MongoDB Atlas")
        except Exception as e:
            logger.warning(f"MongoDB Atlas unreachable ({e}). Using robust in-memory database fallback.")
            _mongo_available = False
            _client = None
    return _client


def get_db():
    """Get or create database singleton."""
    global _db
    if _db is None:
        client = get_client()
        if client is not None:
            _db = client[MONGODB_DB_NAME]
    return _db


def get_jobs_collection():
    """Get the safe jobs collection with transparent fallback."""
    return _safe_collection


def close_connection():
    """Close MongoDB connection."""
    global _client, _db, _mongo_available
    if _client:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
        _db = None
        _mongo_available = False
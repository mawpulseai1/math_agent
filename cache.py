import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path


class Cache:
    def __init__(self, db_path: str, pdf_dir: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(pdf_dir).mkdir(parents=True, exist_ok=True)
        self.pdf_dir = pdf_dir
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS search(key TEXT PRIMARY KEY, payload TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS pdfs(url TEXT PRIMARY KEY, path TEXT, ts REAL);
            """)
            self.db.commit()

    def get(self, key: str, ttl: int):
        with self.lock:
            row = self.db.execute("SELECT payload, ts FROM search WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[1] < ttl:
            return json.loads(row[0])
        return None

    def put(self, key: str, payload):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO search VALUES(?,?,?)",
                (key, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            self.db.commit()

    def save_pdf(self, url: str, data: bytes) -> str:
        h = hashlib.sha256(url.encode()).hexdigest()
        path = os.path.join(self.pdf_dir, h + ".pdf")
        Path(path).write_bytes(data)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pdfs VALUES(?,?,?)", (url, path, time.time()))
            self.db.commit()
        return path

    def load_pdf(self, url: str) -> bytes | None:
        with self.lock:
            row = self.db.execute("SELECT path FROM pdfs WHERE url=?", (url,)).fetchone()
        if row and os.path.exists(row[0]):
            return Path(row[0]).read_bytes()
        return None

    def close(self):
        with self.lock:
            self.db.close()

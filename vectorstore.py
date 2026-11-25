import hashlib
import re
import sqlite3
import threading
import time
import zlib
from pathlib import Path
import numpy as np

HASH_DIM = 2048


def read_text(path: str | Path) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        import fitz
        with fitz.open(str(p)) as pdf:
            return "\n".join(pg.get_text() for pg in pdf)
    return p.read_text(encoding="utf-8", errors="ignore")


def chunk(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text)
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return [c for c in out if c.strip()]


class Embedder:
    def __init__(self, llm, model: str = ""):
        self.llm, self.model = llm, model
        self.id = f"api:{model}" if model else f"hash{HASH_DIM}"

    def __call__(self, texts: list[str]) -> np.ndarray:
        if self.model:
            vecs = []
            for i in range(0, len(texts), 32):
                vecs += self.llm.embed(texts[i:i + 32], self.model)
            v = np.asarray(vecs, dtype=np.float32)
        else:
            v = np.zeros((len(texts), HASH_DIM), dtype=np.float32)
            for i, t in enumerate(texts):
                for tok in re.findall(r"\w+", t.lower()):
                    v[i, zlib.crc32(tok.encode()) % HASH_DIM] += 1.0
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1
        return v / n


class VectorStore:
    def __init__(self, db_path: str, embedder: Embedder):
        self.emb = embedder
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    doc_id TEXT PRIMARY KEY, title TEXT, authors TEXT, year TEXT, path TEXT, added REAL);
                CREATE TABLE IF NOT EXISTS chunks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, pos INTEGER,
                    text TEXT, emb_id TEXT, vec BLOB);
                CREATE INDEX IF NOT EXISTS ix_chunks_emb ON chunks(emb_id);
            """)
            self.db.commit()

    def ingest(self, path: str | Path, title: str = "", authors: str = "", year: str = "") -> int:
        p = Path(path).resolve()
        doc_id = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        with self.lock:
            if self.db.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone():
                return 0
        pieces = chunk(read_text(p))
        if not pieces:
            return 0
        vecs = self.emb(pieces)
        with self.lock:
            self.db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?)",
                (doc_id, title or p.stem, authors, year, str(p), time.time()),
            )
            self.db.executemany(
                "INSERT INTO chunks(doc_id,pos,text,emb_id,vec) VALUES(?,?,?,?,?)",
                [(doc_id, i, t, self.emb.id, v.tobytes()) for i, (t, v) in enumerate(zip(pieces, vecs))],
            )
            self.db.commit()
        return len(pieces)

    def search(self, q: str, k: int = 8):
        qv = self.emb([q])[0]
        with self.lock:
            rows = self.db.execute(
                "SELECT c.doc_id, c.text, c.vec, d.title, d.authors, d.year, d.path "
                "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id WHERE c.emb_id = ?",
                (self.emb.id,),
            ).fetchall()
        if not rows:
            return []
        M = np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows])
        scores = M @ qv
        seen, out = set(), []
        for i in np.argsort(-scores):
            doc = rows[i][0]
            if doc in seen:
                continue
            seen.add(doc)
            out.append((float(scores[i]), rows[i]))
            if len(out) >= k:
                break
        return out

    def close(self):
        with self.lock:
            self.db.close()

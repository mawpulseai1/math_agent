import json
import logging
import re
import threading
import time
from openai import OpenAI

log = logging.getLogger("llm")


def _parse_json_object(txt: str) -> dict:
    """Extract the first valid JSON object from an LLM response."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt.strip(), flags=re.I | re.S)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(cleaned[idx:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return {}


class LLM:
    """OpenAI-compatible LLM client with thread-safe API key rotation and rate-limit failover."""

    def __init__(self, cfg):
        self.cfg = cfg
        keys = list(getattr(cfg, "api_keys", None) or [cfg.api_key])
        self._keys = keys
        self._clients = [OpenAI(base_url=cfg.base_url, api_key=k, timeout=300) for k in keys]
        self._cooldown_until = [0.0] * len(keys)
        self._disabled = [False] * len(keys)
        self._cursor = 0
        self._lock = threading.Lock()

    @property
    def client(self) -> OpenAI:
        idx = self._next_index()
        return self._clients[idx]

    def _next_index(self) -> int:
        now = time.time()
        with self._lock:
            n = len(self._clients)
            # First pass: non-disabled and not on cooldown
            for offset in range(n):
                idx = (self._cursor + offset) % n
                if not self._disabled[idx] and self._cooldown_until[idx] <= now:
                    self._cursor = (idx + 1) % n
                    return idx
            # Second pass: soonest non-disabled key
            active = [i for i in range(n) if not self._disabled[i]]
            if not active:
                # Reset disabled flags as last resort if all keys were marked bad
                self._disabled = [False] * n
                active = list(range(n))
            best = min(active, key=lambda i: self._cooldown_until[i])
            wait = self._cooldown_until[best] - now
            self._cursor = (best + 1) % n
        if wait > 0:
            time.sleep(min(wait, 15.0))
        return best

    def _handle_error(self, idx: int, err: Exception):
        msg = str(err).lower()
        with self._lock:
            if any(tok in msg for tok in ("429", "rate", "quota", "resource_exhausted", "too many requests")):
                self._cooldown_until[idx] = time.time() + 60.0
                log.warning("API key #%d rate-limited; cooling down 60s", idx + 1)
            elif any(tok in msg for tok in ("api_key_invalid", "invalid api key", "401", "403", "404", "expired", "no longer available")):
                if len(self._clients) > 1:
                    self._disabled[idx] = True
                    log.warning("API key #%d rejected (%s); disabling for session", idx + 1, type(err).__name__)
            else:
                self._cooldown_until[idx] = time.time() + 2.0

    def chat(self, system: str, user: str, model: str | None = None, temperature: float = 0.2) -> str:
        last = None
        attempts = max(3, min(len(self._clients) * 2, 16))
        for _ in range(attempts):
            idx = self._next_index()
            try:
                r = self._clients[idx].chat.completions.create(
                    model=model or self.cfg.model,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                txt = r.choices[0].message.content or ""
                return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
            except Exception as e:
                last = e
                self._handle_error(idx, e)
        raise RuntimeError(f"LLM failed after {attempts} attempts: {last}")

    def json(self, system: str, user: str, **kw) -> dict:
        txt = self.chat(system + "\nReply with ONE valid JSON object and nothing else.", user, **kw)
        return _parse_json_object(txt)

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        last = None
        attempts = max(3, min(len(self._clients), 8))
        for _ in range(attempts):
            idx = self._next_index()
            try:
                r = self._clients[idx].embeddings.create(model=model, input=texts)
                return [d.embedding for d in r.data]
            except Exception as e:
                last = e
                self._handle_error(idx, e)
        raise RuntimeError(f"Embeddings failed: {last}")

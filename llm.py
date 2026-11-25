import json
import logging
import re
import threading
import time
from openai import OpenAI

log = logging.getLogger("llm")


def _fix_latex_escapes(txt: str) -> str:
    """Escape stray backslashes (e.g. \\mathbb, \\equiv) that are invalid JSON escapes."""
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', txt)


def _parse_json_object(txt: str) -> dict:
    """Extract the outermost valid JSON object from an LLM response."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt.strip(), flags=re.I | re.S)
    for candidate in (cleaned, _fix_latex_escapes(cleaned)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch != "{":
            continue
        for candidate in (cleaned[idx:], _fix_latex_escapes(cleaned[idx:])):
            try:
                obj, _ = decoder.raw_decode(candidate)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return {}


class _ClientPool:
    """Thread-safe round-robin pool of OpenAI-compatible clients for a base URL."""

    def __init__(self, base_url: str, keys: list[str], name: str = "llm"):
        self.base_url = base_url
        self.name = name
        self._clients = [OpenAI(base_url=base_url, api_key=k, timeout=300) for k in keys]
        self._cooldown_until = [0.0] * len(keys)
        self._disabled = [False] * len(keys)
        self._cursor = 0
        self._lock = threading.Lock()

    def next_index(self) -> int:
        now = time.time()
        with self._lock:
            n = len(self._clients)
            for offset in range(n):
                idx = (self._cursor + offset) % n
                if not self._disabled[idx] and self._cooldown_until[idx] <= now:
                    self._cursor = (idx + 1) % n
                    return idx
            active = [i for i in range(n) if not self._disabled[i]]
            if not active:
                self._disabled = [False] * n
                active = list(range(n))
            best = min(active, key=lambda i: self._cooldown_until[i])
            wait = self._cooldown_until[best] - now
            self._cursor = (best + 1) % n
        if wait > 0:
            time.sleep(min(wait, 15.0))
        return best

    def handle_error(self, idx: int, err: Exception):
        msg = str(err).lower()
        with self._lock:
            if any(tok in msg for tok in ("429", "rate", "quota", "resource_exhausted", "too many requests")):
                self._cooldown_until[idx] = time.time() + 60.0
                log.warning("[%s] API key #%d rate-limited; cooling down 60s", self.name, idx + 1)
            elif any(tok in msg for tok in ("api_key_invalid", "invalid api key", "401", "403", "404", "expired", "no longer available")):
                if len(self._clients) > 1:
                    self._disabled[idx] = True
                    log.warning("[%s] API key #%d rejected (%s); disabling for session", self.name, idx + 1, type(err).__name__)
            else:
                self._cooldown_until[idx] = time.time() + 2.0

    def create_chat(self, model: str, messages: list[dict], temperature: float, reasoning_effort: str | None) -> str:
        last = None
        attempts = max(3, min(len(self._clients) * 2, 16))
        use_effort = reasoning_effort
        for _ in range(attempts):
            idx = self.next_index()
            kwargs = {
                "model": model,
                "temperature": temperature,
                "messages": messages,
            }
            if use_effort:
                kwargs["reasoning_effort"] = use_effort
            try:
                r = self._clients[idx].chat.completions.create(**kwargs)
                txt = r.choices[0].message.content or ""
                return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
            except Exception as e:
                msg = str(e).lower()
                if use_effort and ("reasoning_effort" in msg or "unrecognized" in msg or "extra_forbidden" in msg):
                    use_effort = None
                    continue
                last = e
                self.handle_error(idx, e)
        raise RuntimeError(f"LLM ({self.name}) failed after {attempts} attempts: {last}")

    def create_embed(self, model: str, texts: list[str]) -> list[list[float]]:
        last = None
        attempts = max(3, min(len(self._clients), 8))
        for _ in range(attempts):
            idx = self.next_index()
            try:
                r = self._clients[idx].embeddings.create(model=model, input=texts)
                return [d.embedding for d in r.data]
            except Exception as e:
                last = e
                self.handle_error(idx, e)
        raise RuntimeError(f"Embeddings ({self.name}) failed: {last}")


class LLM:
    """Multi-endpoint OpenAI-compatible client with key rotation and reasoning-effort routing."""

    def __init__(self, cfg):
        self.cfg = cfg
        keys = list(getattr(cfg, "api_keys", None) or [cfg.api_key])
        self._main = _ClientPool(cfg.base_url, keys, name="main")

        prover_url = getattr(cfg, "prover_base_url", "") or cfg.base_url
        prover_keys = list(getattr(cfg, "prover_api_keys", None) or keys)
        self._prover = self._main if (prover_url == cfg.base_url and prover_keys == keys) else _ClientPool(prover_url, prover_keys, name="prover")

        embed_url = getattr(cfg, "embed_base_url", "") or cfg.base_url
        embed_keys = list(getattr(cfg, "embed_api_keys", None) or keys)
        self._embed = self._main if (embed_url == cfg.base_url and embed_keys == keys) else _ClientPool(embed_url, embed_keys, name="embed")

    @property
    def client(self) -> OpenAI:
        return self._main._clients[self._main.next_index()]

    def chat(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
        reasoning_effort: str | None = None,
        use_prover: bool = False,
    ) -> str:
        pool = self._prover if use_prover else self._main
        target_model = model or (self.cfg.prover_model if use_prover else self.cfg.model)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # Avoid sending Gemini reasoning_effort when talking to local Ollama unless explicitly requested
        effort = reasoning_effort
        if effort is None and "generativelanguage.googleapis.com" in pool.base_url:
            effort = getattr(self.cfg, "reasoning_effort", None) or None
        if "generativelanguage.googleapis.com" not in pool.base_url and effort:
            effort = None
        return pool.create_chat(target_model, messages, temperature, effort)

    def json(self, system: str, user: str, **kw) -> dict:
        kw.setdefault("reasoning_effort", "low")
        txt = self.chat(system + "\nReply with ONE valid JSON object and nothing else.", user, **kw)
        return _parse_json_object(txt)

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        return self._embed.create_embed(model, texts)

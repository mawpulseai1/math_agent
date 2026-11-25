import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.getenv(key) or default


def _parse_api_keys() -> list[str]:
    """Parse comma/whitespace-separated API keys from LLM_API_KEYS and LLM_API_KEY."""
    raw = ",".join(filter(None, [os.getenv("LLM_API_KEYS", ""), os.getenv("LLM_API_KEY", "")]))
    seen: set[str] = set()
    keys: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        k = part.strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys or ["ollama"]


@dataclass
class Config:
    # LLM
    base_url: str = env("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    api_keys: list[str] = field(default_factory=_parse_api_keys)
    api_key: str = ""
    model: str = env("LLM_MODEL", "gemini-2.5-flash")
    prover_model: str = env("PROVER_MODEL") or env("LLM_MODEL", "gemini-2.5-flash")
    embed_model: str = env("EMBED_MODEL")

    # Source keys
    email: str = env("CONTACT_EMAIL", "anon@example.com")
    s2_key: str = env("S2_API_KEY")
    core_key: str = env("CORE_API_KEY")
    se_key: str = env("STACKEXCHANGE_KEY")

    # Storage
    data_dir: str = env("DATA_DIR", "./data")
    runs_dir: str = env("RUNS_DIR", "./runs")

    # Lean
    lean_mode: str = env("LEAN_MODE", "docker")
    lean_image: str = env("LEAN_IMAGE", "mathagent-lean")
    lean_project: str = env("LEAN_PROJECT_DIR")
    repl_bin: str = env("REPL_BIN", "/home/lean/repl/.lake/build/bin/repl")
    lean_mem: str = env("LEAN_MEM", "4g")
    repl_mem: str = env("REPL_MEM", "8g")
    lean_cpus: str = env("LEAN_CPUS", "2")
    lean_timeout: int = int(env("LEAN_TIMEOUT", "120"))
    lean_workers: int = int(env("LEAN_WORKERS", "2"))
    heartbeats: int = int(env("HEARTBEATS", "400000"))

    # Tactic search
    search_budget: int = int(env("SEARCH_BUDGET", "200"))
    search_seconds: int = int(env("SEARCH_SECONDS", "600"))
    tactics_per_node: int = int(env("TACTICS_PER_NODE", "5"))

    # Python tool
    py_mode: str = env("PY_MODE", "docker")
    py_image: str = env("PY_IMAGE", "mathagent-py")

    # Agent behaviour
    research_rounds: int = 2
    papers_to_read: int = 6
    informal_revisions: int = 2
    skeleton_repairs: int = 3

    def __post_init__(self):
        if self.api_key and self.api_key not in self.api_keys:
            self.api_keys = [self.api_key] + self.api_keys
        elif not self.api_key:
            self.api_key = self.api_keys[0]

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "agent.sqlite3")

    @property
    def pdf_dir(self) -> str:
        return os.path.join(self.data_dir, "pdfs")

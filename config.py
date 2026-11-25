import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.getenv(key) or default


def _parse_keys(*env_names: str, fallback: list[str] | None = None) -> list[str]:
    """Parse comma/newline-separated API keys from the given environment variables."""
    raw = ",".join(filter(None, [os.getenv(n, "") for n in env_names]))
    seen: set[str] = set()
    keys: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        k = part.strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys or (list(fallback) if fallback else ["ollama"])


@dataclass
class Config:
    # Primary LLM (reasoning, research, informal solving)
    base_url: str = env("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    api_keys: list[str] = field(default_factory=lambda: _parse_keys("LLM_API_KEYS", "LLM_API_KEY"))
    api_key: str = ""
    model: str = env("LLM_MODEL", "gemini-3.5-flash")
    reasoning_effort: str = env("REASONING_EFFORT", "high")

    # Optional dedicated Lean prover endpoint (e.g., local Ollama on GPU)
    prover_base_url: str = env("PROVER_BASE_URL")
    prover_api_keys: list[str] = field(default_factory=list)
    prover_model: str = env("PROVER_MODEL")

    # Optional dedicated embedding endpoint/model (e.g. gemini-embedding-001 or Ollama nomic-embed-text)
    embed_base_url: str = env("EMBED_BASE_URL")
    embed_api_keys: list[str] = field(default_factory=list)
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
    search_budget: int = int(env("SEARCH_BUDGET", "300"))
    search_seconds: int = int(env("SEARCH_SECONDS", "600"))
    tactics_per_node: int = int(env("TACTICS_PER_NODE", "6"))

    # Python tool
    py_mode: str = env("PY_MODE", "docker")
    py_image: str = env("PY_IMAGE", "mathagent-py")

    # Agent behaviour
    research_rounds: int = int(env("RESEARCH_ROUNDS", "3"))
    papers_to_read: int = int(env("PAPERS_TO_READ", "8"))
    solve_candidates: int = int(env("SOLVE_CANDIDATES", "3"))
    informal_revisions: int = int(env("INFORMAL_REVISIONS", "3"))
    skeleton_repairs: int = int(env("SKELETON_REPAIRS", "4"))

    def __post_init__(self):
        if self.api_key and self.api_key not in self.api_keys:
            self.api_keys = [self.api_key] + self.api_keys
        elif not self.api_key:
            self.api_key = self.api_keys[0]

        if not self.prover_base_url:
            self.prover_base_url = self.base_url
        if not self.prover_api_keys:
            self.prover_api_keys = _parse_keys(
                "PROVER_API_KEYS",
                "PROVER_API_KEY",
                fallback=self.api_keys if self.prover_base_url == self.base_url else ["ollama"],
            )
        if not self.prover_model:
            self.prover_model = self.model

        if not self.embed_base_url:
            self.embed_base_url = self.base_url
        if not self.embed_api_keys:
            self.embed_api_keys = _parse_keys(
                "EMBED_API_KEYS",
                "EMBED_API_KEY",
                fallback=self.api_keys if self.embed_base_url == self.base_url else ["ollama"],
            )

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "agent.sqlite3")

    @property
    def pdf_dir(self) -> str:
        return os.path.join(self.data_dir, "pdfs")

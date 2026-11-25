import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

_DOCKER_DESKTOP_PATHS = [
    r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    os.path.expandvars(r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"),
    os.path.expandvars(r"%LocalAppData%\Docker\Docker Desktop.exe"),
]


def docker_ready(timeout: int = 5) -> bool:
    try:
        p = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return p.returncode == 0
    except Exception:
        return False


def image_exists(image: str) -> bool:
    if not image:
        return False
    try:
        p = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return p.returncode == 0
    except Exception:
        return False


def _find_docker_desktop() -> str | None:
    for path in _DOCKER_DESKTOP_PATHS:
        if path and os.path.exists(path):
            return path
    return None


def _is_local_ollama(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.hostname in ("localhost", "127.0.0.1") and parsed.port == 11434


def ollama_ready(base_url: str = "http://localhost:11434", timeout: float = 2.0) -> bool:
    parsed = urlparse(base_url)
    root = f"{parsed.scheme or 'http'}://{parsed.netloc or 'localhost:11434'}"
    try:
        with urllib.request.urlopen(root + "/api/tags", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_ollama(base_url: str = "http://localhost:11434", emit=print, wait_seconds: int = 20) -> bool:
    if ollama_ready(base_url):
        return True
    exe = shutil.which("ollama")
    if not exe:
        emit("startup", "Local Ollama endpoint configured, but `ollama` binary was not found in PATH.")
        return False
    emit("startup", "Ollama is not running. Starting `ollama serve`...")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as e:
        emit("startup", f"Failed to start Ollama: {e}")
        return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if ollama_ready(base_url):
            emit("startup", "Ollama server is ready.")
            return True
        time.sleep(1)
    emit("startup", f"Timed out waiting {wait_seconds}s for Ollama server.")
    return False


def start_docker(emit=print, wait_seconds: int = 90) -> bool:
    if docker_ready():
        return True

    if os.name == "nt":
        exe = _find_docker_desktop()
        if not exe:
            emit("startup", "Docker Desktop not found. Install Docker Desktop or set LEAN_MODE=local / PY_MODE=local.")
            return False
        emit("startup", "Docker is not running. Starting Docker Desktop...")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(
                [exe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception as e:
            emit("startup", f"Failed to launch Docker Desktop: {e}")
            return False
    elif os.uname().sysname == "Darwin":
        emit("startup", "Docker is not running. Starting Docker Desktop...")
        try:
            subprocess.Popen(["open", "-a", "Docker"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            emit("startup", f"Failed to launch Docker Desktop: {e}")
            return False
    else:
        emit("startup", "Docker daemon is not running. Please start the docker service.")
        return False

    deadline = time.time() + wait_seconds
    last_log = 0.0
    while time.time() < deadline:
        if docker_ready(timeout=4):
            emit("startup", "Docker Engine is ready.")
            return True
        now = time.time()
        if now - last_log >= 10:
            emit("startup", "Waiting for Docker Engine to finish starting...")
            last_log = now
        time.sleep(2)

    emit("startup", f"Timed out waiting {wait_seconds}s for Docker Engine.")
    return False


def build_image(image: str, dockerfile: str, emit=print) -> bool:
    df = Path(dockerfile)
    if not df.exists():
        emit("startup", f"Dockerfile not found: {df}")
        return False
    emit("startup", f"Building Docker image {image} from {df}...")
    p = subprocess.run(
        ["docker", "build", "-t", image, "-f", str(df), str(df.parent.parent)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode == 0:
        emit("startup", f"Built {image} successfully.")
        return True
    emit("startup", f"Failed to build {image}: {(p.stderr or p.stdout)[-800:]}")
    return False


def ensure_runtime(cfg, emit=print, build_py: bool = True) -> dict[str, bool]:
    """Ensure required background services (Docker Desktop, local Ollama) and images are ready."""
    need_docker = cfg.lean_mode == "docker" or cfg.py_mode == "docker"
    status = {"docker_ok": not need_docker, "py_image": False, "lean_image": False, "ollama_ok": True}

    for url in dict.fromkeys(
        u for u in (cfg.base_url, getattr(cfg, "prover_base_url", ""), getattr(cfg, "embed_base_url", ""))
        if _is_local_ollama(u)
    ):
        if not start_ollama(url, emit=emit):
            status["ollama_ok"] = False

    if not need_docker:
        return status

    if not start_docker(emit=emit):
        return status
    status["docker_ok"] = True

    if cfg.py_mode == "docker":
        if image_exists(cfg.py_image):
            status["py_image"] = True
        elif build_py:
            root = Path(__file__).resolve().parent
            status["py_image"] = build_image(cfg.py_image, str(root / "docker" / "Dockerfile.py"), emit=emit)
        if not status["py_image"]:
            emit("startup", f"Image '{cfg.py_image}' missing. Build with: docker build -t {cfg.py_image} -f docker/Dockerfile.py .")

    if cfg.lean_mode == "docker":
        status["lean_image"] = image_exists(cfg.lean_image)
        if not status["lean_image"]:
            emit(
                "startup",
                f"Lean image '{cfg.lean_image}' is not built yet; Lean 4 proving will be skipped until built. "
                f"Run: docker build -t {cfg.lean_image} -f docker/Dockerfile.lean .",
            )
    return status

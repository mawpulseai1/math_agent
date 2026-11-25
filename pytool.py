import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    except Exception:
        pass


class PyRunner:
    def __init__(self, cfg):
        self.cfg = cfg

    def run(self, code: str, timeout: int = 60) -> tuple[bool, str, str]:
        """Returns (ok, stdout, stderr)."""
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            f = Path(d) / "test.py"
            f.write_text(code, encoding="utf-8")
            os.chmod(f, 0o644)
            if self.cfg.py_mode == "docker":
                mount_dir = str(Path(d).resolve())
                cmd = [
                    "docker", "run", "--rm", "-i", "--network", "none",
                    "--memory", "1g", "--cpus", "1", "--pids-limit", "64",
                    "--read-only", "--tmpfs", "/tmp:size=64m",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "-v", f"{mount_dir}:/work:ro", self.cfg.py_image,
                    "timeout", str(timeout), "python", "/work/test.py",
                ]
                pre = None
            else:  # resource limits only, NOT sandboxed
                cmd = [sys.executable, str(f)]
                pre = _limits if os.name == "posix" else None
            try:
                p = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout + 20,
                    preexec_fn=pre,
                )
            except subprocess.TimeoutExpired:
                return False, "", "TIMEOUT"
            except FileNotFoundError as e:
                return False, "", str(e)
            return p.returncode == 0, p.stdout or "", p.stderr or ""

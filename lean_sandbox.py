import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SAFE_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}


@dataclass
class LeanResult:
    compiled: bool   # no errors
    complete: bool   # compiled, no sorry, only standard axioms
    output: str


class LeanSandbox:
    def __init__(self, cfg):
        self.cfg = cfg

    def check(self, code: str, theorem: str | None = None) -> LeanResult:
        if theorem:
            code += f"\n\n#print axioms {theorem}\n"
        c = self.cfg
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            f = Path(d) / "Main.lean"
            f.write_text(code, encoding="utf-8")
            os.chmod(f, 0o644)
            if c.lean_mode == "docker":
                mount_dir = str(Path(d).resolve())
                cmd = [
                    "docker", "run", "--rm", "--network", "none",
                    "--memory", c.lean_mem, "--cpus", c.lean_cpus, "--pids-limit", "256",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "-v", f"{mount_dir}:/work:ro", c.lean_image,
                    "timeout", str(c.lean_timeout), "lake", "env", "lean", "/work/Main.lean",
                ]
                cwd = None
            else:  # NOT sandboxed
                cmd, cwd = ["lake", "env", "lean", str(f)], (c.lean_project or None)
            try:
                p = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=c.lean_timeout + 60,
                    cwd=cwd,
                )
            except subprocess.TimeoutExpired:
                return LeanResult(False, False, "TIMEOUT")
            except FileNotFoundError as e:
                return LeanResult(False, False, f"sandbox unavailable: {e}")
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if c.lean_mode == "docker" and (
            "dockerDesktopLinuxEngine" in out
            or "Cannot connect to the Docker daemon" in out
            or f"Unable to find image '{c.lean_image}" in out
        ):
            return LeanResult(
                False,
                False,
                f"Docker/Lean sandbox unavailable (start Docker Desktop and build {c.lean_image}, or set LEAN_MODE=local): {out}",
            )
        if p.returncode == 124:
            return LeanResult(False, False, "TIMEOUT\n" + out)
        has_error = p.returncode != 0 or bool(re.search(r":\d+:\d+: error", out))
        has_sorry = "sorry" in out
        bad_axiom = False
        if theorem:
            m = re.search(r"depends on axioms: \[(.*?)\]", out, re.S)
            if m:
                used = {a.strip() for a in m.group(1).split(",") if a.strip()}
                bad_axiom = not used <= SAFE_AXIOMS
            elif "does not depend on any axioms" in out:
                bad_axiom = False
            else:
                bad_axiom = True  # cannot certify
        return LeanResult(not has_error, (not has_error) and not has_sorry and not bad_axiom, out)

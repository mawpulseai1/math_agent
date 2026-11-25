import json
import queue
import subprocess
import threading


class ReplError(RuntimeError):
    pass


class LeanRepl:
    """
    Talks to leanprover-community/repl over stdin/stdout.
    Each request is a JSON object followed by a blank line; each response is a
    JSON object followed by a blank line.
    """

    def __init__(self, cmd, cwd=None, timeout: int = 120):
        self.timeout = timeout
        self.p = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        buf = []
        assert self.p.stdout is not None
        for line in self.p.stdout:
            if line.strip():
                buf.append(line)
                continue
            if buf:
                text = "".join(buf)
                buf = []
                try:
                    self.q.put(json.loads(text))
                except json.JSONDecodeError:
                    pass  # stray non-JSON output (warnings etc.)
        self.q.put(None)

    def _send(self, obj: dict, timeout: int | None = None):
        if self.p.poll() is not None:
            raise ReplError("REPL process exited")
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n\n")
        self.p.stdin.flush()
        try:
            r = self.q.get(timeout=timeout or self.timeout)
        except queue.Empty:
            raise TimeoutError("REPL response timeout")
        if r is None:
            raise ReplError("REPL closed")
        return r

    def cmd(self, text: str, env: int | None = None, timeout: int | None = None):
        obj: dict = {"cmd": text}
        if env is not None:
            obj["env"] = env
        return self._send(obj, timeout)

    def tactic(self, tac: str, state: int):
        return self._send({"tactic": tac, "proofState": state})

    def close(self):
        try:
            if self.p.stdin:
                self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass

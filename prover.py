import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from lean_repl import LeanRepl
from tactic_search import search
from util import extract_block, header_of, strip_imports

SYS = "You are an expert Lean 4 / Mathlib proof engineer. Output exactly what is asked."


class ProofError(RuntimeError):
    pass


def _errors(resp: dict) -> list[str]:
    return [m.get("data", "") for m in resp.get("messages", []) if m.get("severity") == "error"]


def _strip_sorry_tail(stmt: str) -> str:
    return re.sub(r":=\s*by\s*sorry\s*$", ":= by", strip_imports(stmt).strip())


def assemble(body: str, holes: list[dict], scripts: list[list[str] | None]) -> str:
    """Replace each solved `sorry` with its tactic script. Edits go bottom-up so positions stay valid."""
    lines = body.split("\n")
    edits = [
        (h["pos"]["line"], h["pos"]["column"], h["end"]["column"], s)
        for h, s in zip(holes, scripts)
        if s
    ]
    for line_no, c1, c2, script in sorted(edits, reverse=True):
        line = lines[line_no - 1]
        ind = len(line) - len(line.lstrip(" "))
        prefix, suffix = line[:c1], line[c2:]
        if prefix.strip() == "":
            # `sorry` is a tactic on its own line
            block = [prefix + script[0]] + [" " * c1 + t for t in script[1:]]
        else:
            # `sorry` follows `by` on the same line
            block = [prefix.rstrip()] + [" " * (ind + 2) + t for t in script]
        block[-1] += suffix
        lines[line_no - 1:line_no] = block
    return "\n".join(lines)


def whole_file_repair(cfg, llm, statement: str, informal: str, check, emit, search_names, rounds: int = 4):
    """Fast compiler-guided whole-proof loop before falling back to REPL hole search."""
    want = header_of(statement)
    body = strip_imports(statement)
    feedback = ""
    last_code = ""
    for attempt in range(rounds):
        txt = llm.chat(
            SYS,
            f"""Prove this theorem completely in Lean 4 + Mathlib.
STATEMENT (keep the theorem header IDENTICAL):
{_strip_sorry_tail(statement)}

INFORMAL PROOF (use this as your guide):
{informal[:6000]}

Rules:
- Start with `import Mathlib`. Keep `theorem main_thm` signature IDENTICAL.
- No `sorry`, no `admit`, no `native_decide`, no custom axioms.
- Use clean `have` subgoals and standard Mathlib tactics (`ring`, `nlinarith`, `positivity`, `omega`, `simp`, `field_simp`, `exact`, `apply`, `induction`, `rcases`).
- Output only one ```lean block.{feedback}""",
            model=cfg.prover_model,
            temperature=0.2 + 0.15 * (attempt > 0),
            reasoning_effort=cfg.reasoning_effort,
            use_prover=True,
        )
        cand = strip_imports(extract_block(txt, ("lean", "lean4")))
        if header_of(cand) != want:
            feedback = "\n\nYou changed the theorem header. Copy `theorem main_thm ... := by` EXACTLY."
            emit("prove", f"whole-file attempt {attempt + 1}/{rounds}: header changed")
            continue
        last_code = cand
        res = check("import Mathlib\n\n" + cand, theorem="main_thm")
        emit("prove", f"whole-file attempt {attempt + 1}/{rounds}: complete={res.complete}")
        if res.complete:
            return cand, res
        unknown = re.findall(r"unknown (?:identifier|constant) '([^']+)'", res.output)
        hints = search_names(unknown) if unknown else []
        feedback = (
            f"\n\nPrevious attempt:\n{cand}\n\nLean compiler output:\n{res.output[:2500]}\n"
            + ("Possibly relevant Mathlib names:\n" + "\n".join(hints) + "\n" if hints else "")
            + "Fix all errors and eliminate any sorry."
        )
    return last_code or body, None


def make_skeleton(cfg, llm, statement: str, informal: str, check, emit, search_names):
    want = header_of(statement)
    feedback = ""
    for attempt in range(cfg.skeleton_repairs + 1):
        txt = llm.chat(
            SYS,
            f"""Write a Lean 4 + Mathlib PROOF SKELETON for the theorem below.

STATEMENT (copy the header EXACTLY):
{_strip_sorry_tail(statement)}

INFORMAL PROOF (research-backed):
{informal[:6000]}

Rules:
- Keep the theorem header identical. The proof starts after `:= by`.
- Apart from `intro`/`obtain` for naming hypotheses from the statement, use only
  `have name : <precise Lean proposition> := by sorry` lines mirroring the informal proof.
  Each proposition must typecheck with the hypotheses in scope at that point.
- Finish with one line `sorry`, which stands for closing the main goal from the haves.
- Every `sorry` becomes a hole that is filled automatically. Do not write other tactics.
- Output only one ```lean block.{feedback}""",
            model=cfg.prover_model,
            temperature=0.3,
            reasoning_effort=cfg.reasoning_effort,
            use_prover=True,
        )
        body = strip_imports(extract_block(txt, ("lean", "lean4")))
        if header_of(body) != want:
            feedback = "\n\nThe theorem header changed. Copy the statement EXACTLY as given."
            emit("prove", f"skeleton attempt {attempt + 1}: header changed, retrying")
            continue
        res = check("import Mathlib\n\n" + body)
        emit("prove", f"skeleton attempt {attempt + 1}: {'compiles' if res.compiled else 'has errors'}")
        if res.compiled:
            return body
        unknown = re.findall(r"unknown (?:identifier|constant) '([^']+)'", res.output)
        hints = search_names(unknown) if unknown else []
        feedback = (
            f"\n\nYour previous skeleton:\n{body}\n\nLean errors:\n{res.output[:2500]}\n"
            + ("Possibly relevant Mathlib names:\n" + "\n".join(hints) + "\n" if hints else "")
            + "Fix all errors."
        )
    return None


class ProofRunner:
    def __init__(self, cfg, llm, check, hints, emit):
        self.cfg, self.llm, self.check, self.hints, self.emit = cfg, llm, check, hints, emit
        self._live: list[LeanRepl] = []
        self._lock = threading.Lock()

    # ---- REPL lifecycle ----
    def _cmd(self):
        c = self.cfg
        if c.lean_mode == "docker":
            return [
                "docker", "run", "-i", "--rm", "--network", "none",
                "--memory", c.repl_mem, "--cpus", c.lean_cpus,
                "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                c.lean_image, "bash", "-c",
                f"cd /home/lean/proj && exec lake env {c.repl_bin}",
            ], None
        return ["lake", "env", c.repl_bin], (c.lean_project or None)

    def _start(self) -> LeanRepl:
        cmd, cwd = self._cmd()
        r = LeanRepl(cmd, cwd=cwd, timeout=self.cfg.lean_timeout)
        with self._lock:
            self._live.append(r)
        errs = _errors(r.cmd("import Mathlib", timeout=900))
        if errs:
            raise ProofError("import Mathlib failed: " + "\n".join(errs)[:500])
        return r

    def _load(self, repl: LeanRepl, body: str) -> list[dict]:
        """Elaborate the body in env 0 and return its holes (sorries) sorted by position."""
        r = repl.cmd(body, env=0)
        errs = _errors(r)
        if errs:
            raise ProofError("\n".join(errs)[:3000])
        ss = sorted(r.get("sorries", []), key=lambda s: (s["pos"]["line"], s["pos"]["column"]))
        return [
            {"state": s["proofState"], "goal": s["goal"], "pos": s["pos"], "end": s["endPos"]}
            for s in ss
        ]

    # ---- hole solving ----
    def _propose(self, goal: str, path: list[str], hints: list[str]) -> list[str]:
        hint_txt = "\n".join(hints) or "(none)"
        try:
            r = self.llm.json(
                SYS,
                f"""Goal state (hypotheses above ⊢, target below):
{goal[:3000]}

Tactics already applied on this branch: {path[-6:] or '(none)'}
Possibly useful Mathlib names:
{hint_txt}

Propose {self.cfg.tactics_per_node} DIFFERENT candidate tactics that make progress on the first goal.
Each must be ONE line of Lean 4. No sorry, admit, native_decide, or multi-line blocks.
A one-line `have h : P := by nlinarith [...]` is allowed.
Reply JSON: {{"tactics": ["...", "..."]}}""",
                model=self.cfg.prover_model,
                temperature=0.7,
                reasoning_effort=self.cfg.reasoning_effort,
                use_prover=True,
            )
        except Exception:
            return []
        out = []
        for t in r.get("tactics", []):
            if isinstance(t, str):
                t = t.strip()
                if t and "\n" not in t and not re.search(r"\b(sorry|admit|native_decide)\b", t):
                    out.append(t)
        return out[: self.cfg.tactics_per_node]

    def _solve_hole(self, repl: LeanRepl, h: dict, i: int):
        hints = self.hints(h["goal"])
        return search(
            repl,
            h["state"],
            h["goal"],
            lambda node: self._propose(node.goals[0], node.path, hints),
            budget=self.cfg.search_budget,
            seconds=self.cfg.search_seconds,
            emit=self.emit,
            tag=f"hole {i}",
        )

    def _worker(self, repl: LeanRepl, pre, body: str, q: queue.Queue, scripts: list):
        try:
            mine = pre if pre is not None else self._load(repl, body)
        except Exception as e:
            self.emit("prove", f"worker failed to load: {e}")
            return
        while True:
            try:
                i = q.get_nowait()
            except queue.Empty:
                return
            try:
                scripts[i] = self._solve_hole(repl, mine[i], i)
            except Exception as e:
                self.emit("prove", f"hole {i}: {type(e).__name__}: {e}; restarting REPL")
                repl.close()
                try:
                    repl = self._start()
                    mine = self._load(repl, body)
                except Exception as e2:
                    self.emit("prove", f"worker stopped: {e2}")
                    return

    # ---- public ----
    def prove(self, body: str):
        """Returns (final_code, holes_solved, holes_total, LeanResult)."""
        body = f"set_option maxHeartbeats {self.cfg.heartbeats} in\n{body}"
        n = max(1, self.cfg.lean_workers)
        try:
            with ThreadPoolExecutor(n) as ex:
                repls = list(ex.map(lambda _: self._start(), range(n)))
            holes = self._load(repls[0], body)
            self.emit("prove", f"{len(holes)} hole(s), {n} worker(s)")
            scripts: list[list[str] | None] = [None] * len(holes)
            q: queue.Queue = queue.Queue()
            for i in range(len(holes)):
                q.put(i)
            ts = [
                threading.Thread(
                    target=self._worker,
                    args=(repls[k], holes if k == 0 else None, body, q, scripts),
                    daemon=True,
                )
                for k in range(n)
            ]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        finally:
            for r in list(self._live):
                r.close()
            self._live.clear()

        final = assemble(body, holes, scripts)
        solved = sum(1 for s in scripts if s)
        res = self.check(f"import Mathlib\n\n{final}", theorem="main_thm")
        self.emit("prove", f"holes closed {solved}/{len(holes)}; kernel check complete={res.complete}")
        return final, solved, len(holes), res

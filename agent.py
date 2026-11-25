import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import premise
import sources as S
from cache import Cache
from config import Config
from export import write_run
from lean_sandbox import LeanSandbox
from llm import LLM
from prover import ProofRunner, make_skeleton, whole_file_repair
from pytool import PyRunner
from runtime import ensure_runtime
from util import extract_block, strip_imports
from vectorstore import Embedder, VectorStore

MATH = (
    "You are a rigorous research mathematician. Be precise, never invent theorem statements "
    "or citations; if unsure, say so."
)


@dataclass
class Report:
    problem: str = ""
    decomposition: dict = field(default_factory=dict)
    experiments: dict = field(default_factory=dict)
    experiment_code: str = ""
    sources: list = field(default_factory=list)
    insights: list = field(default_factory=list)
    informal: str = ""
    statement: str = ""
    lean_code: str = ""
    lean_verified: bool = False
    prover_stats: dict = field(default_factory=dict)
    status: str = "UNVERIFIED"
    markdown: str = ""


class MathAgent:
    def __init__(self, cfg: Config | None = None, emit=None):
        self.cfg = cfg or Config()
        self.emit = emit or (lambda stage, text: print(f"[{stage}] {text}"))
        self.cache = Cache(self.cfg.db_path, self.cfg.pdf_dir)
        self.llm = LLM(self.cfg)
        self.vs = VectorStore(self.cfg.db_path, Embedder(self.llm, self.cfg.embed_model))
        S.CACHE, S.VSTORE = self.cache, self.vs
        self.lean = LeanSandbox(self.cfg)
        self.py = PyRunner(self.cfg)
        self.sources: list[S.Doc] = []
        self._runtime: dict[str, bool] | None = None

    def ensure_ready(self) -> dict[str, bool]:
        self._runtime = ensure_runtime(self.cfg, emit=self.emit)
        return self._runtime

    # ------------------------------------------------------------ library
    def ingest(self, paths: list[str]) -> dict[str, int]:
        return {p: self.vs.ingest(p) for p in paths}

    # ------------------------------------------------------------ 1
    def deconstruct(self, problem: str) -> dict:
        self.emit("deconstruct", "Analyzing the problem...")
        d = self.llm.json(
            MATH,
            f"""Deconstruct this problem.
PROBLEM: {problem}

JSON keys:
 "restatement", "type" (prove | compute | find | decide), "domain", "msc",
 "givens": [..], "goal": "...", "key_concepts": [..], "subproblems": [..], "likely_techniques": [..],
 "search_queries": [{{"q": "short keyword query", "targets": [...]}}]
   6-10 diverse queries covering the exact statement, generalizations, special cases, named theorems,
   and terminology synonyms. targets are chosen from: "papers", "qa", "web", "oeis", "mine"
   ("mine" = the user's own paper library).""",
            reasoning_effort="low",
        )
        d.setdefault("restatement", problem)
        d.setdefault("type", "prove")
        d.setdefault("goal", problem)
        d.setdefault("givens", [])
        d.setdefault("key_concepts", [])
        if not d.get("search_queries"):
            d["search_queries"] = [{"q": problem[:200], "targets": ["papers", "qa", "web", "mine"]}]
        self.emit("deconstruct", json.dumps(d, indent=2, ensure_ascii=False))
        return d

    # ------------------------------------------------------------ 2
    def explore(self, problem: str, d: dict) -> tuple[dict, str]:
        self.emit("explore", "Writing and running numerical tests...")
        fb = ""
        code = ""
        for _ in range(3):
            code = extract_block(
                self.llm.chat(
                    MATH,
                    f"""PROBLEM: {d.get('restatement', problem)}
TYPE: {d.get('type')}

Write a self-contained Python 3 script that tests this claim numerically.
- Allowed modules: sympy, numpy, mpmath, fractions, itertools, random, math. No file, network, or subprocess use.
- prove/decide: look for counterexamples over small exact inputs (fractions.Fraction) and random samples
  (random.seed(0)), including edge cases. Check identities symbolically with sympy where possible.
- find/compute: enumerate candidate values and print the pattern you observe.
- Total runtime under 15 seconds.
- The last printed line must be exactly:
  RESULT: {{"verdict": ..., "evidence": ..., "counterexample": ..., "candidate_answer": ...}}
  verdict is one of: no_counterexample_found, counterexample_found, pattern_found, inconclusive.
  evidence: short string including the number of tests run. counterexample and candidate_answer: null or a string.
Reply with one ```python block only.{fb}""",
                    temperature=0.2,
                ),
                ("python", "py", ""),
            )
            ok, out, err = self.py.run(code)
            m = re.search(r"^RESULT:\s*(\{.*\})\s*$", out, re.M)
            if ok and m:
                try:
                    res = json.loads(m.group(1))
                    self.emit("explore", f"{res.get('verdict')}: {res.get('evidence')}")
                    return res, code
                except json.JSONDecodeError:
                    pass
            fb = f"\n\nYour previous script failed.\nSTDERR:\n{err[-1500:]}\nSTDOUT tail:\n{out[-500:]}\nFix it."
        return {"verdict": "inconclusive", "evidence": "numerical script failed"}, code

    # ------------------------------------------------------------ 3
    def _search(self, queries) -> list[S.Doc]:
        jobs = []
        for q in queries:
            if isinstance(q, str):
                q = {"q": q, "targets": ["papers", "qa", "web"]}
            for t in q.get("targets", ["papers"]):
                for s in S.TARGETS.get(t, []):
                    jobs.append((s, q["q"]))
        jobs = list(dict.fromkeys(jobs))
        self.emit("research", f"{len(jobs)} searches across sources")
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda j: S.safe(*j), jobs))
        return [d for r in res for d in r]

    def _rank(self, problem: str, docs: list[S.Doc]) -> list[S.Doc]:
        docs = docs[:50]
        if not docs:
            return []
        listing = "\n".join(
            f"[{i}] ({d.source}, {d.year}) {d.title}: {d.abstract[:300]}"
            for i, d in enumerate(docs)
        )
        r = self.llm.json(
            MATH,
            f"PROBLEM: {problem}\n\nCANDIDATES:\n{listing}\n\n"
            f'Pick up to {self.cfg.papers_to_read} items most likely to contain methods, '
            f'lemmas, or results useful for the problem. JSON: {{"top": [indices]}}',
        )
        idx = [i for i in r.get("top", []) if isinstance(i, int) and 0 <= i < len(docs)]
        return [docs[i] for i in idx] or docs[: self.cfg.papers_to_read]

    def _read(self, docs: list[S.Doc], d: dict) -> list[dict]:
        kws = d.get("key_concepts", [])[:8]

        def work(doc):
            try:
                text = S.best_chunks(S.fulltext(doc), kws)
                out = self.llm.chat(
                    MATH,
                    f"""PROBLEM: {d.get('restatement')}
SOURCE: {doc.title} ({doc.source}, {doc.year})
EXCERPTS:
{text[:18000]}

Extract ONLY what helps solve the problem: exact theorem/lemma statements, key techniques,
and how they could apply. Quote precisely and note hypotheses. If nothing useful, reply NONE.""",
                    reasoning_effort="low",
                )
            except Exception as e:
                out = f"NONE ({e})"
            return doc, out

        with ThreadPoolExecutor(3) as ex:
            results = list(ex.map(work, docs))
        ins = []
        for doc, out in results:
            if out.strip().upper().startswith("NONE"):
                continue
            self.sources.append(doc)
            ins.append({"src": len(self.sources), "text": out})
            self.emit("research", f"[{len(self.sources)}] {doc.title} ({doc.source}) {doc.url}")
        return ins

    def research(self, problem: str, d: dict) -> list[dict]:
        seen, insights, queries = set(), [], d["search_queries"]
        for rnd in range(self.cfg.research_rounds):
            self.emit("research", f"--- round {rnd + 1} ---")
            new = []
            for doc in self._search(queries):
                if doc.key() not in seen:
                    seen.add(doc.key())
                    new.append(doc)
            self.emit("research", f"{len(new)} new candidates")
            insights += self._read(self._rank(problem, new), d)
            digest = "\n".join(f"[{i['src']}] {i['text'][:500]}" for i in insights) or "(nothing yet)"
            gap = self.llm.json(
                MATH,
                f"PROBLEM: {d.get('restatement')}\nINSIGHTS SO FAR:\n{digest}\n\n"
                'Is this enough to attempt a solution? JSON: {"sufficient": bool, '
                '"new_queries": [{"q": "...", "targets": ["papers"]}]}',
            )
            if gap.get("sufficient") or not gap.get("new_queries"):
                break
            queries = gap["new_queries"]
        return insights

    # ------------------------------------------------------------ 4
    def _verify_solution_sympy(self, d: dict, sol: str) -> str:
        """Ask the LLM to write a short SymPy sanity check for algebraic/numeric steps in the draft."""
        try:
            code = extract_block(
                self.llm.chat(
                    MATH,
                    f"""PROBLEM: {d.get('restatement')}
PROPOSED SOLUTION:
{sol[:5000]}

Write a tiny self-contained Python 3 script (sympy/fractions/math only) that checks the key algebraic identities,
bounds, edge cases, or computed values asserted in the solution above.
Print `CHECK_OK` if every check passes, or print `CHECK_FAIL: <details>` if any claim fails.
If the proof is purely non-computational (e.g., abstract topology/set theory), just print `CHECK_OK`.
Reply with one ```python block.""",
                    temperature=0.1,
                    reasoning_effort="low",
                ),
                ("python", "py", ""),
            )
            ok, out, err = self.py.run(code, timeout=30)
            if not ok:
                return f"SymPy verification script errored: {(err or out)[-600:]}"
            if "CHECK_FAIL" in out:
                return out.strip()[-800:]
        except Exception:
            pass
        return ""

    def solve_informal(self, d: dict, insights: list[dict], experiments: dict) -> str:
        ctx = (
            "\n\n".join(
                f"[{i['src']}] {self.sources[i['src'] - 1].title}\n{i['text']}"
                for i in insights
            )
            or "(no sources found)"
        )
        n_cand = max(1, self.cfg.solve_candidates)
        angles = [
            ("direct / algebraic / constructive", 0.2),
            ("extremal / invariant / contradiction or alternative lemma", 0.45),
            ("research-guided synthesis using the strongest cited results", 0.65),
        ]
        self.emit("solve", f"Drafting {n_cand} candidate solution(s) with high reasoning...")

        def draft(idx: int) -> str:
            angle, temp = angles[idx % len(angles)]
            return self.llm.chat(
                MATH,
                f"""PROBLEM ANALYSIS:\n{json.dumps(d, indent=1, ensure_ascii=False)}

NUMERICAL EVIDENCE (Python tests):\n{json.dumps(experiments, indent=1, ensure_ascii=False)}

RESEARCH NOTES:\n{ctx}

Approach focus: {angle}.
Solve the problem. First state the strategy, citing the results you use as [n].
Then give a complete, rigorous, step-by-step solution. State the final answer clearly.
If numerical evidence contradicts the claim, say so. Mark any step you are not sure of with (UNSURE).""",
                temperature=temp,
                reasoning_effort=self.cfg.reasoning_effort,
            )

        if n_cand == 1:
            sol = draft(0)
        else:
            with ThreadPoolExecutor(min(n_cand, 3)) as ex:
                cands = list(ex.map(draft, range(n_cand)))
            listing = "\n\n".join(f"=== CANDIDATE {i} ===\n{c[:4500]}" for i, c in enumerate(cands))
            pick = self.llm.json(
                MATH,
                f"PROBLEM: {d.get('restatement')}\n\nCANDIDATE SOLUTIONS:\n{listing}\n\n"
                'Pick the most mathematically rigorous, complete, gap-free candidate. '
                'JSON: {"best": 0, "reason": "..."}',
                reasoning_effort=self.cfg.reasoning_effort,
            )
            best_idx = pick.get("best", 0)
            if not isinstance(best_idx, int) or not (0 <= best_idx < len(cands)):
                best_idx = 0
            self.emit("solve", f"Selected candidate {best_idx}: {pick.get('reason', '')}")
            sol = cands[best_idx]

        for _ in range(self.cfg.informal_revisions):
            sym_err = self._verify_solution_sympy(d, sol)
            if sym_err:
                self.emit("solve", f"SymPy check flagged: {sym_err[:200]}")
            crit = self.llm.json(
                MATH,
                f"PROBLEM: {d.get('restatement')}\n\nSOLUTION:\n{sol}\n\n"
                + (f"SYMPY VERIFICATION NOTE:\n{sym_err}\n\n" if sym_err else "")
                + 'Act as a skeptical referee. Check every step, edge cases, and misuse of theorems. '
                'JSON: {"valid": bool, "issues": ["..."]}',
                reasoning_effort=self.cfg.reasoning_effort,
            )
            self.emit("solve", f"Referee: {crit}")
            issues = list(crit.get("issues") or [])
            if sym_err and "CHECK_FAIL" in sym_err:
                issues.append(sym_err)
            if crit.get("valid") and not issues:
                break
            if not issues:
                break
            sol = self.llm.chat(
                MATH,
                f"PROBLEM: {d.get('restatement')}\nRESEARCH:\n{ctx}\n\nSOLUTION:\n{sol}\n\n"
                f"REFEREE ISSUES:\n{issues}\n\nRewrite the complete corrected solution.",
                reasoning_effort=self.cfg.reasoning_effort,
            )
        return sol

    # ------------------------------------------------------------ 5
    def formalize(self, d: dict) -> str:
        if self.cfg.lean_mode == "docker" and self._runtime is not None:
            if not self._runtime.get("docker_ok"):
                self.emit("lean", "Skipping Lean formalization: Docker Engine is unavailable.")
                return ""
            if not self._runtime.get("lean_image"):
                self.emit(
                    "lean",
                    f"Skipping Lean formalization: Docker image '{self.cfg.lean_image}' is not built yet.",
                )
                return ""
        self.emit("lean", "Formalizing the statement in Lean 4 / Mathlib...")
        feedback = ""
        for _ in range(4):
            code = extract_block(
                self.llm.chat(
                    MATH,
                    f"""Write a Lean 4 + Mathlib formal STATEMENT for this problem.
PROBLEM: {d.get('restatement')}
TYPE: {d.get('type')}   GOAL: {d.get('goal')}   GIVENS: {d.get('givens')}
Rules:
- Start with `import Mathlib`. Name the theorem exactly `main_thm`. End the statement with `:= by sorry`.
- For compute/find problems, state that the answer equals the specific value (or a set equality).
- Be FAITHFUL: no extra hypotheses, no weakening.
- Output only one ```lean block.{feedback}""",
                    model=self.cfg.prover_model,
                    reasoning_effort=self.cfg.reasoning_effort,
                    use_prover=True,
                ),
                ("lean", "lean4"),
            )
            code = "import Mathlib\n\n" + strip_imports(code)
            res = self.lean.check(code)
            if not res.compiled:
                if "sandbox unavailable" in res.output:
                    self.emit("lean", res.output)
                    return ""
                unknown = re.findall(r"unknown (?:identifier|constant) '([^']+)'", res.output)
                hints = premise.search_names(unknown) if unknown else []
                feedback = (
                    f"\n\nPrevious attempt:\n{code}\nLean errors:\n{res.output[:2500]}\n"
                    + ("Possibly relevant Mathlib names:\n" + "\n".join(hints) + "\n" if hints else "")
                    + "Fix them."
                )
                continue
            back = self.llm.json(
                MATH,
                f"ORIGINAL: {d.get('restatement')}\n\nLEAN STATEMENT:\n{code}\n\n"
                'Translate the Lean statement back into plain math and compare it with the original. '
                'JSON: {"faithful": bool, "explanation": "..."}',
                reasoning_effort=self.cfg.reasoning_effort,
            )
            self.emit("lean", f"Fidelity check: {back}")
            if back.get("faithful"):
                return code
            feedback = f"\n\nPrevious attempt:\n{code}\nUnfaithful because: {back.get('explanation')}"
        return ""

    # ------------------------------------------------------------ 6/7
    def prove(self, r: Report, d: dict):
        body = strip_imports(r.statement)

        # Fast compiler-guided whole-proof repair loop first
        self.emit("prove", "proof search mode: whole-file compiler repair")
        wf_code, wf_res = whole_file_repair(
            self.cfg,
            self.llm,
            r.statement,
            r.informal,
            self.lean.check,
            self.emit,
            premise.search_names,
        )
        if wf_res and wf_res.complete:
            r.lean_code = wf_code
            r.lean_verified = True
            r.prover_stats = {"mode": "whole_file_repair", "holes_solved": 1, "holes_total": 1}
            r.status = "VERIFIED by Lean 4 kernel"
            return

        runner = ProofRunner(self.cfg, self.llm, self.lean.check, premise.for_goal, self.emit)
        skel = make_skeleton(
            self.cfg,
            self.llm,
            r.statement,
            r.informal,
            self.lean.check,
            self.emit,
            premise.search_names,
        )
        plans = ([("skeleton", skel)] if skel else []) + [("whole", body)]
        best = None
        for name, cand in plans:
            self.emit("prove", f"proof search mode: {name}")
            try:
                final, solved, total, res = runner.prove(cand)
            except Exception as e:
                self.emit("prove", f"{name} mode failed: {e}")
                continue
            best = (name, final, solved, total)
            if res.complete:
                r.lean_code = final
                r.lean_verified = True
                r.prover_stats = {"mode": name, "holes_solved": solved, "holes_total": total}
                r.status = "VERIFIED by Lean 4 kernel"
                return
        if best:
            name, final, solved, total = best
            r.lean_code = final
            r.prover_stats = {"mode": name, "holes_solved": solved, "holes_total": total}
            r.status = f"UNVERIFIED: {solved}/{total} subgoals closed, Lean proof incomplete"
        else:
            r.status = "UNVERIFIED: Lean proof search failed"

    # ------------------------------------------------------------ run
    def run(self, problem: str) -> Report:
        self.ensure_ready()
        r = Report(problem=problem)
        d = self.deconstruct(problem)
        r.decomposition = d
        r.experiments, r.experiment_code = self.explore(problem, d)
        refuted = r.experiments.get("verdict") == "counterexample_found"
        r.insights = self.research(problem, d)
        r.sources = list(self.sources)
        r.informal = self.solve_informal(d, r.insights, r.experiments)
        r.statement = self.formalize(d)
        if refuted:
            r.status = "LIKELY FALSE: numerical counterexample found (verify it before trusting this)"
        elif r.statement:
            self.prove(r, d)
        else:
            r.status = "UNVERIFIED: no faithful Lean statement produced"
        r.markdown = self._markdown(r)
        self.emit("done", r.status)
        return r

    def export(self, report: Report):
        return write_run(self, report)

    def _markdown(self, r: Report) -> str:
        src = "\n".join(
            f"{i + 1}. {s.title} ({s.source}, {s.year}) {s.url}" for i, s in enumerate(r.sources)
        )
        exp = r.experiments
        exp_txt = (
            f"Verdict: **{exp.get('verdict')}**\n\n{exp.get('evidence', '')}"
            if exp
            else "_not run_"
        )
        ps = r.prover_stats
        ps_txt = (
            f"Mode: {ps.get('mode')}, subgoals closed {ps.get('holes_solved')}/{ps.get('holes_total')}"
            if ps
            else ""
        )
        lean_src = r.lean_code or r.statement
        lean = f"```lean\n{lean_src}\n```" if lean_src else "_none_"
        return (
            f"# Result\n\n**Status:** {r.status}\n\n## Problem\n{r.problem}\n\n"
            f"## Numerical exploration\n{exp_txt}\n\n## Solution\n{r.informal}\n\n"
            f"## Lean 4\n{ps_txt}\n\n{lean}\n\n## Sources\n{src or '_none_'}\n"
        )

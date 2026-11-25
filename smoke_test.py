"""Checks each component separately. Run: python smoke_test.py"""
import logging
from cache import Cache
from config import Config
from lean_sandbox import LeanSandbox
from llm import LLM
import premise
from prover import ProofRunner
from pytool import PyRunner
import sources as S

logging.basicConfig(level=logging.WARNING)
cfg = Config()
S.CACHE = Cache(cfg.db_path, cfg.pdf_dir)


def check(name, fn):
    try:
        print(f"[ok]   {name}: {fn()}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


check(
    "llm (gemini)",
    lambda: f"keys={len(cfg.api_keys)}, model={cfg.model}, reply={LLM(cfg).chat('Reply briefly.', 'Say OK.')!r}",
)

for name in [
    "arxiv",
    "semanticscholar",
    "openalex",
    "crossref",
    "zbmath",
    "core",
    "hal",
    "zenodo",
    "stackexchange",
    "web",
    "oeis",
]:
    if name == "core" and not cfg.core_key:
        print("[skip] source core: CORE_API_KEY not set (optional)")
        continue
    check(f"source {name}", lambda n=name: f"{len(S.SOURCES[n]('Ramsey theory'))} results")

check("loogle", lambda: f"{len(premise.loogle('Nat.succ_le'))} hits")
check("leansearch", lambda: f"{len(premise.leansearch('sum of two even numbers is even'))} hits")
check("moogle", lambda: f"{len(premise.moogle('prime numbers are infinite'))} hits")
def py_test():
    ok, out, err = PyRunner(cfg).run("print(2 + 2)")
    if not ok:
        raise RuntimeError(err.strip() or f"exit non-zero (mode={cfg.py_mode})")
    return out.strip()


check("python sandbox", py_test)


def lean_test():
    pr = ProofRunner(cfg, LLM(cfg), LeanSandbox(cfg).check, lambda g: [], print)
    final, solved, total, res = pr.prove(
        "theorem main_thm (a b : ℕ) : a + b = b + a := by sorry"
    )
    return f"holes {solved}/{total}, kernel-complete={res.complete}"


check("lean REPL + tactic search", lean_test)

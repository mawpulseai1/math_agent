import datetime
import json
import re
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from util import extract_block

SYS = "You are a LaTeX typesetter for mathematics. Output only LaTeX."

TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage{fontspec}
\usepackage{unicode-math}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amsthm,amssymb}
\usepackage[colorlinks=true]{hyperref}
\usepackage{listings}
\lstset{basicstyle=\ttfamily\small, breaklines=true, columns=flexible}
\newtheorem{theorem}{Theorem}
\newtheorem{lemma}[theorem]{Lemma}
\title{Mathematical Research Report}
\date{\today}
\begin{document}
\maketitle
\noindent\textbf{Status:} @@STATUS@@

@@BODY@@

\section*{Formal verification (Lean 4)}
\begin{lstlisting}
@@LEAN@@
\end{lstlisting}

\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""


def _esc(s: str) -> str:
    return re.sub(r"([&%$#_])", r"\\\1", s or "")


def bibtex(sources) -> tuple[str, dict[int, str]]:
    entries, keys, used = [], {}, set()
    for i, d in enumerate(sources, 1):
        parts = d.authors.split(",")[0].split()
        last = parts[-1] if parts else "anon"
        word = (re.findall(r"[A-Za-z]+", d.title) or ["ref"])[0].lower()
        base = re.sub(r"\W", "", f"{last}{d.year}{word}") or f"ref{i}"
        key, c = base, 2
        while key in used:
            key, c = f"{base}{c}", c + 1
        used.add(key)
        keys[i] = key
        authors = " and ".join(a.strip() for a in d.authors.split(",") if a.strip())
        fields = [
            ("title", _esc(d.title)),
            ("author", _esc(authors)),
            ("year", d.year),
            ("url", d.url),
            ("note", f"Source: {d.source}"),
        ]
        body = "\n".join(f"  {name} = {{{val}}}," for name, val in fields if val)
        entries.append(f"@misc{{{key},\n{body}\n}}")
    return "\n\n".join(entries), keys


def latex_body(llm, markdown: str, keys: dict[int, str]) -> str:
    md = re.sub(r"```lean.*?```", "", markdown, flags=re.S)
    txt = llm.chat(
        SYS,
        "Convert this Markdown report into the BODY of a LaTeX article.\n"
        "- No \\documentclass, no preamble, no \\begin{document}.\n"
        "- Use \\section / \\subsection, displayed math \\[ ... \\], and amsthm environments "
        "(theorem, lemma, proof) where natural.\n"
        "- Keep citation markers such as [3] unchanged.\n"
        "- Escape % & # _ wherever they are outside math mode.\n"
        "- Omit the Lean block and the sources list.\n"
        "- Output only LaTeX, no commentary.\n\nMARKDOWN:\n" + md,
        temperature=0.1,
        reasoning_effort="low",
    )
    body = extract_block(txt, ("latex", "tex", ""))
    body = re.sub(
        r"\[(\d+)\]",
        lambda m: f"\\cite{{{keys[int(m.group(1))]}}}" if int(m.group(1)) in keys else m.group(0),
        body,
    )
    return body


def compile_pdf(out: Path) -> bool:
    if not shutil.which("latexmk"):
        return False
    try:
        subprocess.run(
            ["latexmk", "-xelatex", "-interaction=nonstopmode", "-file-line-error", "report.tex"],
            cwd=out,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except Exception:
        return False
    return (out / "report.pdf").exists()


def write_run(agent, report) -> tuple[Path, bool]:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(agent.cfg.runs_dir) / stamp
    out.mkdir(parents=True, exist_ok=True)

    bib, keys = bibtex(report.sources)
    (out / "refs.bib").write_text(bib, encoding="utf-8")
    (out / "report.md").write_text(report.markdown, encoding="utf-8")
    (out / "sources.json").write_text(
        json.dumps([asdict(s) for s in report.sources], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if report.lean_code or report.statement:
        (out / "proof.lean").write_text(report.lean_code or report.statement, encoding="utf-8")
    if report.experiment_code:
        (out / "numerical_tests.py").write_text(report.experiment_code, encoding="utf-8")

    body = latex_body(agent.llm, report.markdown, keys)
    status = report.status.replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")
    tex = (
        TEMPLATE.replace("@@STATUS@@", status)
        .replace("@@BODY@@", body)
        .replace("@@LEAN@@", report.lean_code or report.statement or "(none)")
    )
    (out / "report.tex").write_text(tex, encoding="utf-8")
    return out, compile_pdf(out)

# Math Research Agent

Desktop & CLI mathematical research agent following the pipeline:
**deconstruct → numerical/symbolic testing (SymPy) → multi-source literature research → informal proof + referee loop → Lean 4 + Mathlib formalization & parallel best-first tactic search → Markdown / LaTeX / BibTeX export**.

## Setup

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure environment variables:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` to set your LLM endpoint/keys (`LLM_BASE_URL`, `LLM_API_KEY` or comma-separated `LLM_API_KEYS`, `LLM_MODEL`, `REASONING_EFFORT`), optional semantic embeddings (`EMBED_MODEL=gemini-embedding-001` or local Ollama `nomic-embed-text`), optional local GPU prover (`PROVER_BASE_URL=http://localhost:11434/v1` with e.g. `hf.co/mradermacher/Goedel-Prover-V2-8B-GGUF:Q4_K_M`), and optional academic API keys (`S2_API_KEY`, `CORE_API_KEY`, `STACKEXCHANGE_KEY`, `CONTACT_EMAIL`).

3. Build Docker sandbox images (Docker Desktop is started automatically when launching the agent):
   ```bash
   docker build -t mathagent-py -f docker/Dockerfile.py .
   docker build -t mathagent-lean -f docker/Dockerfile.lean .
   ```
   *(The `mathagent-lean` image clones `mathlib4`, fetches its prebuilt `.olean` cache, and builds `leanprover-community/repl`, taking ~15–60 minutes on first build.)*

## Usage

- **Smoke test** (check LLM, academic sources, premise search, Python sandbox, Lean REPL):
  ```bash
  python smoke_test.py
  ```
- **Desktop GUI**:
  ```bash
  python main.py
  ```
- **Terminal CLI**:
  ```bash
  python main.py --cli "Prove that for all positive reals a, b, c: a^2 + b^2 + c^2 >= ab + bc + ca"
  ```
- **Ingest local PDFs/notes into the vector store**:
  ```bash
  python main.py --ingest path/to/paper1.pdf path/to/notes.txt
  ```

## Output

Each run exports to `runs/<timestamp>/`:
- `report.md` — full Markdown report
- `report.tex` & `refs.bib` (+ `report.pdf` if `latexmk` / XeLaTeX is installed)
- `proof.lean` — formalized Lean 4 code
- `numerical_tests.py` — SymPy/NumPy verification script
- `sources.json` — retrieved references

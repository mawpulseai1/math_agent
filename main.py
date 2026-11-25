import argparse
import logging
import sys
from pathlib import Path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Math research agent")
    ap.add_argument("--cli", nargs="+", metavar="PROBLEM", help="solve a problem from the terminal")
    ap.add_argument("--ingest", nargs="+", metavar="FILE", help="add PDFs/text to your library")
    ap.add_argument("--build-lean", action="store_true", help="start Docker and build the Lean 4 + Mathlib image")
    args = ap.parse_args()

    if args.build_lean:
        from config import Config
        from runtime import build_image, start_docker
        cfg = Config()
        if start_docker():
            root = Path(__file__).resolve().parent
            ok = build_image(cfg.lean_image, str(root / "docker" / "Dockerfile.lean"), stream=True)
            sys.exit(0 if ok else 1)
        sys.exit(1)
    elif args.ingest:
        from agent import MathAgent
        agent = MathAgent()
        for path, n in agent.ingest(args.ingest).items():
            print(f"{path}: {n} chunks")
    elif args.cli:
        from agent import MathAgent
        agent = MathAgent()
        rep = agent.run(" ".join(args.cli))
        out, pdf = agent.export(rep)
        print(rep.markdown)
        print(f"\nSaved to {out} (PDF: {'yes' if pdf else 'no'})")
    else:
        from gui import run_gui
        run_gui()


if __name__ == "__main__":
    main()

FROM ubuntu:24.04
RUN apt-get update && apt-get install -y curl git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd -m lean
USER lean
WORKDIR /home/lean
ENV PATH="/home/lean/.elan/bin:${PATH}"

# Optional: pin Mathlib to a release tag, e.g. --build-arg MATHLIB_TAG=v4.x.0
ARG MATHLIB_TAG=""
RUN curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none

RUN if [ -n "$MATHLIB_TAG" ]; then B="--branch $MATHLIB_TAG"; else B=""; fi && \
    git clone --depth 1 $B https://github.com/leanprover-community/mathlib4.git proj

WORKDIR /home/lean/proj
RUN elan toolchain install "$(cat lean-toolchain)" && lake exe cache get

# The REPL must be built with the SAME Lean version as Mathlib.
# REPL tags match Lean release tags. If this checkout fails, pick the REPL tag
# that matches lean-toolchain by hand.
RUN git clone https://github.com/leanprover-community/repl.git /home/lean/repl && \
    cd /home/lean/repl && \
    git checkout "$(sed 's/.*://' /home/lean/proj/lean-toolchain)" && \
    lake build

# Sanity checks: one-shot compile and REPL binary present
RUN printf 'import Mathlib\nexample : 2 + 2 = 4 := by norm_num\n' > /tmp/t.lean && \
    lake env lean /tmp/t.lean && \
    test -x /home/lean/repl/.lake/build/bin/repl

FROM python:3.12-slim
RUN pip install --no-cache-dir sympy==1.13.3 numpy mpmath
RUN useradd -m runner
USER runner
WORKDIR /work

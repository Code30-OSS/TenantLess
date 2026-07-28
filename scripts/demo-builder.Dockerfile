# Pinned canonical builder for the bundled synthetic `demo` profile (D-11).
#
# This image provides ONLY a reproducible numerical toolchain -- Python 3.11 and
# the uv-locked dependency set (numpy / scipy / scikit-learn pinned by uv.lock).
# The project source is MOUNTED at runtime (-v <repo>:/src, PYTHONPATH=/src/src),
# not baked in, so the derivation writes the demo.json straight back into the
# working tree. Byte-identical rebuilds are guaranteed inside THIS image; the
# derivation is invoked with OMP/OPENBLAS/MKL_NUM_THREADS=1 so BLAS reductions do
# not perturb the fitted distributions.
#
# Base pinned by digest for reproducibility:
#   python:3.11-slim @ sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93
FROM python@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

# uv pinned to the version used in the canonical environment. The resolved
# dependency set is frozen by uv.lock, so the toolchain is reproducible.
RUN pip install --no-cache-dir uv==0.10.8

WORKDIR /build
# Only the manifests are copied; the deps are resolved from the frozen lockfile.
# --no-install-project: install the locked dependency graph only (the project
# itself is mounted at runtime, so it must NOT be baked into the image).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

WORKDIR /src
CMD ["/opt/venv/bin/python", "scripts/build_demo_profile.py"]

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

# HOME is set to a world-writable dir so the image runs as an ARBITRARY non-root UID
# (Wave1 #5). Invoke it with `--user "$(id -u):$(id -g)"` so the demo.json it writes
# back into the mounted working tree is owned by the host user, never root:
#
#   docker run --rm --user "$(id -u):$(id -g)" \
#     -e TENANTLESS_BUILD_ALLOW_DESTRUCTIVE=1 \
#     -e DATABASE_URL=postgres://…@<disposable-pg>:5432/tenantless \
#     -v "$PWD:/src" tenantless-demo-builder
#
# /opt/venv and /build are world-readable, so a non-root UID runs the venv fine; only
# the mounted /src is written, and only as the host user.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PYTHONPATH=/src/src \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1

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

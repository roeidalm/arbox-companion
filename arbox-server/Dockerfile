# Alpine, because there is nothing here that needs a glibc userland: every
# locked dependency ships a musllinux wheel, so the build compiles nothing and
# the image is ~100MB smaller than the Debian slim it replaces. Verified on
# this base: zoneinfo is present, and libc reports the right UTC offset on
# both sides of Israel's DST change — which the registration-window maths
# depends on.
#
# Digest-pinned, so "what is running on user boxes" is answerable from this
# repo: the tag is republished whenever Alpine moves, and two builds of the
# same git tag could otherwise ship different system libraries. Renovate keeps
# the digest current (docker:pinDigests).
FROM python:3.14-alpine@sha256:c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc AS test

# Tests have their own fully hashed dependency graph. This stage is disposable:
# pytest, httpx2 and their transitive dependencies are never inherited by the
# runtime stage below and therefore can never inflate the published image.
COPY requirements-test.txt requirements-test.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-test.lock

WORKDIR /srv/arbox-server
COPY app ./app
COPY frontend ./frontend
COPY pytest.ini ./
COPY tests ./tests

ENV DATA_DIR=/tmp/arbox-test-data \
    TZ=Asia/Jerusalem \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m pytest


# Start again from the pristine base, not from `test`: only runtime packages
# cross this boundary into the image users pull.
FROM python:3.14-alpine@sha256:c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc AS runtime

# The lock, not the six top-level pins: those left starlette, pydantic, yarl
# and the rest floating, so two builds of the same git tag could ship
# different code with no diff in this repo. --require-hashes also means a
# tampered or re-uploaded wheel fails the build instead of shipping.
# Regenerate with:
#   docker run --rm -v "$PWD":/w -w /w python:3.14-slim \
#     sh -c 'pip install -q pip-tools && pip-compile --generate-hashes \
#            --strip-extras -o requirements.lock requirements.txt'
#
# pip and its friends are then removed. Nothing in this image installs
# software, and an installer left lying around is only useful to someone who
# should not be here.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
 && (pip uninstall --yes pip setuptools wheel 2>/dev/null || true) \
 && rm -rf /root/.cache /usr/local/lib/python3.14/ensurepip

WORKDIR /srv/arbox-server
COPY app ./app
COPY frontend ./frontend

ARG BUILD_VERSION=dev
ARG BUILD_REVISION=unknown
ARG BUILD_SOURCE_DATE=unknown
ARG BUILD_SOURCE_REPOSITORY=unknown
ARG BUILD_REVISION_URL=unknown
ARG BUILD_RELEASE_URL=unknown
ARG BUILD_IMAGE=unknown
ARG BUILD_RUN_URL=unknown
ENV DATA_DIR=/data \
    TZ=Asia/Jerusalem \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Release provenance belongs to the artifact, not its runtime environment. A
# container manager may override ENV freely; it cannot rewrite this file in
# the read-only image. Build args are non-secret inputs from the tag workflow
# and do not remain as runtime environment variables.
RUN python -m app.build_info

# Compiled now, never at run time: the bytecode is already in the image, so
# refusing to write more of it costs no startup speed and the root filesystem
# can be mounted read-only without Python wanting to write into it.
RUN python -m compileall -q app

# nobody:nogroup — the account that owns nothing and may do nothing. No
# useradd: it already exists, and the point is that this process has no
# identity worth escalating from. The server touches the operating system not
# at all; every write it makes lands in /data (credentials, settings, the
# SQLite file and its journal), which is what lets this container run with a
# read-only root filesystem and every Linux capability dropped.
#
# /data is created here so a fresh named volume inherits this ownership. A
# volume written by an older, root-running version must be handed over once —
# see "Upgrades" in deploy/README.md.
RUN mkdir -p /data && chown 65534:65534 /data
USER 65534:65534

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

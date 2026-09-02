# Every dependency is pinned in requirements.txt, and the base image is pinned by digest
# rather than by tag. `python:3.12-slim` is a moving target, so a tag-only build is not
# reproducible.
#
# To move the base image deliberately:
#   docker pull python:3.12-slim && docker image inspect python:3.12-slim \
#     --format '{{index .RepoDigests 0}}'
# then update the digest here, rebuild the image, and rerun the vulnerability scan.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

COPY app /app/app

# Compose overrides this with PUID:PGID for bind-mounted libraries. Keep direct image
# runs non-root by default too.
USER 65532:65532

EXPOSE 8300
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]

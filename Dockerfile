# Every dependency is pinned in requirements.txt, and the base image is pinned by digest
# rather than by tag. `python:3.12-slim` is a moving target, so a tag-only build is not
# reproducible.
#
# To move the base image deliberately:
#   docker pull python:3.12-slim && docker image inspect python:3.12-slim \
#     --format '{{index .RepoDigests 0}}'
# then update the digest here, rebuild the image, and rerun the vulnerability scan.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

COPY app /app/app

# Compose overrides this with PUID:PGID for bind-mounted libraries. Keep direct image
# runs non-root by default too.
USER 65532:65532

EXPOSE 8300
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]

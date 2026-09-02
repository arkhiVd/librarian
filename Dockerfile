# Every dependency is pinned in requirements.txt, and the base image is pinned by digest
# rather than by tag. `python:3.12-slim` is a moving target, so a tag-only build is not
# reproducible.
#
# To move the base image deliberately:
#   docker pull python:3.12-slim && docker image inspect python:3.12-slim \
#     --format '{{index .RepoDigests 0}}'
# then update the digest here and the "Verified on" row in SPEC.md.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app/app

EXPOSE 8300
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]

# Offline CloudLeak container. Run with --network=none --read-only.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir build==1.2.2 && python -m build --wheel --outdir /dist

FROM python:3.12-slim
LABEL org.opencontainers.image.title="cloudleak" \
      org.opencontainers.image.description="Offline audit for AWS, Azure, GCP and FOCUS billing exports" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/pranavg21/cloudleak"
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl \
 && useradd --create-home --uid 10001 cloudleak
USER 10001
ENV CLOUDLEAK_TEMP_DIR=/tmp PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
ENTRYPOINT ["cloudleak"]
CMD ["--help"]

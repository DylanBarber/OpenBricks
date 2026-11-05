# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS node-base
FROM maven:3.9-eclipse-temurin-17 AS spark-jars
WORKDIR /build
COPY docker/spark-jars/pom.xml ./pom.xml
RUN mvn -B -q org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies -DincludeScope=runtime -DoutputDirectory=/opt/jars

FROM node-base AS web-build
WORKDIR /build/web
ARG OPENBRICKS_MAX_UPLOAD_MB=100
ENV NEXT_TELEMETRY_DISABLED=1
ENV OPENBRICKS_MAX_UPLOAD_MB=${OPENBRICKS_MAX_UPLOAD_MB}
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS backend-base
LABEL org.opencontainers.image.title="OpenBricks" \
      org.opencontainers.image.description="Self-hosted notebooks, workflows, Spark SQL, and data tools" \
      org.opencontainers.image.source="https://github.com/DylanBarber/OpenBricks" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production \
    DATA_DIR=/data \
    HOME=/data/home \
    SPARK_LOCAL_IP=127.0.0.1 \
    PYSPARK_PYTHON=/usr/local/bin/python \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless git openssh-client tini libstdc++6 libatomic1 build-essential ca-certificates curl unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb -o /tmp/microsoft-prod.deb \
    && dpkg -i /tmp/microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm /tmp/microsoft-prod.deb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 openbricks \
    && useradd --uid 10001 --gid 10001 --home-dir /data/home --no-create-home openbricks \
    && mkdir -p /data/home /app/web /app/backend \
    && chown -R 10001:10001 /data /app
COPY --from=node-base /usr/local/bin/node /usr/local/bin/node
WORKDIR /app/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 backend/ ./
COPY --chown=10001:10001 cli/ /app/cli/
RUN pip install --no-cache-dir --no-deps --no-build-isolation /app/cli
COPY --chown=10001:10001 docker/ /app/docker/
COPY --from=spark-jars /opt/jars/ /opt/openbricks/jars/
ARG OPENBRICKS_BUILD_ID=development
ARG OPENBRICKS_VERSION=0.4.0
ENV OPENBRICKS_BUILD_ID=${OPENBRICKS_BUILD_ID} \
    OPENBRICKS_VERSION=${OPENBRICKS_VERSION}
LABEL org.opencontainers.image.version="${OPENBRICKS_VERSION}" \
      org.opencontainers.image.revision="${OPENBRICKS_BUILD_ID}"
USER 10001:10001

FROM backend-base AS compute
VOLUME ["/data"]
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "openbricks.compute:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]

FROM backend-base AS runtime
ENV OPENBRICKS_MANAGED_CONTAINER=1
COPY --from=web-build --chown=10001:10001 /build/web/.next/standalone /app/web/
COPY --from=web-build --chown=10001:10001 /build/web/.next/static /app/web/.next/static/
VOLUME ["/data"]
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/docker/start.py"]

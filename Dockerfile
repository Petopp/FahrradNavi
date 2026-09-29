# syntax=docker/dockerfile:1
# Basis-Image austauschbar, z. B. --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim
ARG PYTHON_IMAGE=python:3.12-slim

# --- Build-Stufe: Abhängigkeiten + Paket in ein separates Präfix installieren --------------------
FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

# --- Laufzeit: schlankes Image, unprivilegierter Benutzer -----------------------------------------
FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="FahrradNavi" \
      org.opencontainers.image.description="Fahrrad-Routenplaner auf OSM-Basis, der Autostraßen meidet"
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
 && mkdir /data && chown app:app /data
COPY --from=build /install /usr/local

USER app
WORKDIR /home/app
# Der Routing-Graph liegt im Volume /data (Erzeugen: `docker compose run --rm builder`)
ENV FAHRRADNAVI_GRAPH=/data/graph.npz \
    FAHRRADNAVI_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["fahrradnavi", "serve", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Der Routing-Graph (graph.npz) wird als Volume eingebunden, siehe README.
ENV FAHRRADNAVI_GRAPH=/data/graph.npz
VOLUME /data
EXPOSE 8000
CMD ["fahrradnavi", "serve", "--host", "0.0.0.0", "--port", "8000"]

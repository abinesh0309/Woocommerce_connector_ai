FROM python:3.13-slim AS build
RUN pip install --no-cache-dir uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 woo
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER woo
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"
ENTRYPOINT ["woo-connector"]
CMD ["serve", "--http", "--host", "0.0.0.0"]

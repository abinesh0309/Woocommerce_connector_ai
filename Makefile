.PHONY: install check test lint format spec spec-check demo docker web ui-test
install:
	uv sync
check: lint test spec-check
test:
	uv run pytest
lint:
	uv run ruff check . && uv run ruff format --check .
format:
	uv run ruff check --fix . && uv run ruff format .
spec:
	uv run woo-connector spec
spec-check:
	uv run woo-connector spec -o /tmp/spec-check.json && diff -q /tmp/spec-check.json mcp-tool-spec.json
demo:
	uv run python scripts/demo.py --fake --rate-limit
docker:
	docker build -t woo-connector .
web:
	uv run woo-connector web
ui-test:
	cd tests/ui && npm install && cd ../.. && uv run python scripts/ui_smoke.py

.PHONY: help test

help:            ## List make targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:[ ]*## /\t/'
test:            ## Lint, format check, type-check and unit tests
	@uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest

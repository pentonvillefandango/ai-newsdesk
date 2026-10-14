.PHONY: help deploy rollback status health smoke logs backup version set-password test
SERVICE ?= app
SINCE ?= 30m
SHA ?=
# Passed to scripts via the environment (never expanded into the recipe text).
export SERVICE SINCE SHA

help:            ## List ops commands
	@grep -E '^[a-z][a-z-]*:.*##' $(MAKEFILE_LIST) | sed -E 's/:[ ]*## /\t/'
deploy:          ## Build + start the committed HEAD, smoke test, auto-rollback on failure (approval required)
	@ops/deploy.sh
rollback:        ## Redeploy the last good commit, or SHA=<sha> (approval required)
	@ops/rollback.sh
status:          ## Containers, resources, image, last deploy/backup (read-only)
	@ops/status.sh
health:          ## /health, container health, Caddy host match, secrets, password (read-only)
	@ops/health.sh
smoke:           ## /health, /login and the auth gate from inside the container (read-only)
	@ops/smoke.sh
logs:            ## Recent logs: make logs SERVICE=app SINCE=30m (read-only)
	@ops/logs.sh "$$SERVICE" "$$SINCE"
backup:          ## Online SQLite backup to backups/ with retention
	@ops/backup.sh
version:         ## Running image tag, deployed commit and repo HEAD (read-only)
	@ops/version.sh
set-password:    ## Set the owner login password (interactive; then make deploy)
	@ops/set_password.sh
test:            ## Lint, format check, type check and unit tests
	@uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest

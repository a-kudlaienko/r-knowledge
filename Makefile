.PHONY: guide clean build install-local test-integration sync-skill \
        pg-build pg-run pg-stop pg-logs pg-psql pg-clean _check-pg-env

PACKAGE_NAME := repo-knowledge
REPO_PATH = readlink -f .

# ---------------------------------------------------------------------------
# Local-dev PostgreSQL container for storage.mode = shared_postgresql.
# ---------------------------------------------------------------------------
# Image is built locally from ./Dockerfile (pgvector + repo schema baked in).
# Container holds postgres data in a named volume so `make pg-stop &&
# make pg-run` keeps your indexed projects across restarts; `make pg-clean`
# wipes both. Repo is bind-mounted at /repo:rw for ad-hoc inspection
# (psql \i /repo/some_file.sql, etc).
PG_IMAGE_NAME  := repo-knowledge-pg
PG_CONTAINER   := knowledge-pg
PG_DATA_VOLUME := knowledge-pg-data
PG_PORT        := 5432
PG_DB_NAME     := knowledge

clean:
	rm -rfv dist build *.egg-info src/*.egg-info

# Opt-in end-to-end test of shared-PostgreSQL team mode (needs Docker + the
# [postgres] extra). Stands up an isolated pgvector container and two
# simulated teammates; tears everything down on exit. Not part of the
# lightweight build+smoke CI.
test-integration:
	bash tests/integration/shared_pg/run.sh

# Regenerate the per-IDE skill siblings (AGENTS.md, knowledge.mdc) from the
# canonical skill-template/SKILL.md. Run after editing SKILL.md;
# tests/test_skill_sync.py fails CI if these drift.
sync-skill:
	python -m knowledge.skill_render

build: clean
	python -m pip install --upgrade build
	python -m build

install-local: build
	python -m pip install --no-index --find-links=dist $(PACKAGE_NAME)

# ---------------------------------------------------------------------------
# Postgres targets
# ---------------------------------------------------------------------------

# Validate required env vars before any docker call. We do NOT default
# user/password — shared_postgresql is a team feature where each developer
# carries their own credentials, and silently using "postgres/postgres"
# would mask laptop misconfiguration that bites in CI later.
_check-pg-env:
	@if [ -z "$$KNOWLEDGE_PG_USER" ] || [ -z "$$KNOWLEDGE_PG_PASSWORD" ]; then \
		echo "error: KNOWLEDGE_PG_USER and KNOWLEDGE_PG_PASSWORD must be set in your shell."; \
		echo ""; \
		echo "Quickstart for local dev:"; \
		echo "  export KNOWLEDGE_PG_USER=postgres"; \
		echo "  export KNOWLEDGE_PG_PASSWORD=\$$(openssl rand -hex 16)"; \
		echo ""; \
		echo "(Each developer carries their own credentials — see the 'Shared PostgreSQL' section in README.md.)"; \
		exit 2; \
	fi

pg-build:
	docker build -t $(PG_IMAGE_NAME) .

# Start the container. Mounts:
#   $(PG_DATA_VOLUME):/var/lib/postgresql/data   — persistent DB data
#   $(CURDIR):/repo:rw                            — repo bind-mount (RW)
# Env:
#   POSTGRES_USER/PASSWORD — pulled from your shell, not embedded in the
#   Makefile (so `make -n pg-run` doesn't print the password).
pg-run: _check-pg-env pg-build
	@if [ -n "$$(docker ps -q -f name=^$(PG_CONTAINER)$$)" ]; then \
		echo "container '$(PG_CONTAINER)' is already running on localhost:$(PG_PORT)"; \
		exit 0; \
	fi
	@docker rm -f $(PG_CONTAINER) >/dev/null 2>&1 || true
	docker run -d \
		--name $(PG_CONTAINER) \
		-e POSTGRES_USER=$$KNOWLEDGE_PG_USER \
		-e POSTGRES_PASSWORD=$$KNOWLEDGE_PG_PASSWORD \
		-e POSTGRES_DB=$(PG_DB_NAME) \
		-p $(PG_PORT):5432 \
		-v $(PG_DATA_VOLUME):/var/lib/postgresql/data \
		-v $(CURDIR):/repo:rw \
		$(PG_IMAGE_NAME)
	@echo ""
	@echo "started: $(PG_CONTAINER) on localhost:$(PG_PORT) (db=$(PG_DB_NAME))"
	@echo ""
	@echo "Next steps on the laptop (knowledge CLI):"
	@echo "  knowledge config init                # writes ~/.knowledge/config.json (laptop default)"
	@echo "  # or: knowledge config init --project (writes <git-root>/.knowledge-config.json — closer wins)"
	@echo "  # edit the JSON file (keys shown in dotted form):"
	@echo "  #   storage.mode               = \"shared_postgresql\""
	@echo "  #   storage.postgresql.host    = \"localhost\""
	@echo "  #   storage.postgresql.sslmode = \"disable\"   # local docker has no TLS"
	@echo "  knowledge config show          # confirm DSN + env-var status + which file is active"
	@echo "  knowledge db init-postgres     # idempotent re-apply (no-op on first run)"
	@echo "  cd /path/to/some/repo && knowledge build"

pg-stop:
	@docker stop $(PG_CONTAINER) >/dev/null 2>&1 && echo "stopped: $(PG_CONTAINER)" || echo "$(PG_CONTAINER) not running"

pg-logs:
	docker logs -f $(PG_CONTAINER)

pg-psql: _check-pg-env
	docker exec -it -e PGPASSWORD=$$KNOWLEDGE_PG_PASSWORD \
		$(PG_CONTAINER) psql -U $$KNOWLEDGE_PG_USER -d $(PG_DB_NAME)

# Destructive: removes the container AND the data volume. Used when you
# want a clean schema re-init from initdb.d, or when bumping the postgres
# major version (data dirs aren't compatible across majors).
pg-clean:
	@docker rm -f $(PG_CONTAINER) >/dev/null 2>&1 || true
	@docker volume rm $(PG_DATA_VOLUME) >/dev/null 2>&1 || true
	@echo "removed container + data volume (destructive)"

guide:
	@echo ""
	@echo " * To build and install ${PACKAGE_NAME} inside your project Python venv:"
	@echo "     - jump in inside your repo (not this one)"
	@echo "     - create VENV if doesn't exist: 'python -m venv ~/.venv'"
	@echo "     - activate your Python venv: 'source ~/.venv/bin/activate'"
	@echo "     - python -m pip install -e `${REPO_PATH}`"
	@echo ""
	@echo ""
	@echo " * Build the knowledge base:"
	@echo "     - knowledge build"
	@echo "   First time: scan + chunk + embed (cold: 1-5 min)"
	@echo ""
	@echo ""
	@echo " * Then check how it works(EXAMPLE):"
	@echo "     - knowledge search 'terraform resource: load balancer' --kind resource --lang hcl"
	@echo ""
	@echo ""
	@echo " * To update the knowledge base:"
	@echo "     - knowledge update"
	@echo "   Incremental; auto-detects changed files"
	@echo ""
	@echo ""
	@echo " * Local dev PostgreSQL (storage.mode=shared_postgresql):"
	@echo "     - export KNOWLEDGE_PG_USER=postgres"
	@echo "     - export KNOWLEDGE_PG_PASSWORD=\$$(openssl rand -hex 16)"
	@echo "     - make pg-run         # build image + start container"
	@echo "     - make pg-psql        # interactive psql shell"
	@echo "     - make pg-stop        # stop (data preserved)"
	@echo "     - make pg-clean       # remove + wipe data volume (destructive)"

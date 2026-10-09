.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
COMPOSE := docker compose -f $(ROOT)/compose.yaml
RUN := $(COMPOSE) run --rm app
# `make sync PLAYLIST_NAMES=plain` overrides the value from .env for one run.
NAMES := $(if $(PLAYLIST_NAMES),-e PLAYLIST_NAMES=$(PLAYLIST_NAMES))
DATA := $(ROOT)/data

.PHONY: help build export smoke-export summary auth match match-until-done migrate-until-done rematch report accept plan sync review apply archive-matched restore-archived test

help: ## List the targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

build: ## Build the Docker image
	$(COMPOSE) build

export: ## Snapshot the Music.app library to data/library.json (host, via osascript)
	@if [ -f $(DATA)/archive-manifest.csv ] && [ -z "$(FORCE)" ]; then \
		echo "Tracks were archived and removed from Music; a new snapshot without them would make the"; \
		echo "next sync drop them from every Spotify playlist. Keeping data/library.json (FORCE=1 overrides)."; \
		exit 1; fi
	@mkdir -p $(DATA)
	osascript -l JavaScript $(ROOT)/exporter/export_library.js > $(DATA)/library.json.tmp
	mv $(DATA)/library.json.tmp $(DATA)/library.json
	$(RUN) summary

smoke-export: export ## Export the real library and check it is complete and consistent
	$(RUN) validate

summary: ## Print the snapshot as a folder tree with counts
	$(RUN) summary

auth: ## Log in to Spotify; the redirect comes back to http://127.0.0.1:8888/callback
	$(COMPOSE) run --rm --service-ports app auth

match: ## Find every library track on Spotify and write reports/ (cached, resumable)
	$(RUN) match

match-until-done: ## Repeat match, sleeping out each Spotify quota block, until every track is searched
	$(ROOT)/scripts/until-done.sh match

migrate-until-done: ## Like match-until-done, with a sync after every run so playlists fill up daily
	PLAYLIST_NAMES='$(PLAYLIST_NAMES)' $(ROOT)/scripts/until-done.sh migrate

rematch: ## Re-score from cached searches only, without calling Spotify
	$(RUN) match --offline

report: ## Rewrite reports/ from the saved matches
	$(RUN) report

accept: ## Apply accept/spotify columns from reports/*.csv as overrides, then match again
	$(RUN) accept
	$(RUN) match

plan: ## Write reports/plan.md: playlists and folders a sync would produce (no writes)
	$(COMPOSE) run --rm $(NAMES) app plan

sync: ## Create or update the playlists in Spotify (PLAYLIST_NAMES=plain once in folders)
	$(COMPOSE) run --rm $(NAMES) app sync

review: ## Browse, play and decide the open tracks in a terminal UI (on the Mac: needs Music, Spotify, audio)
	@python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null || { echo "make review needs Python 3.11+ on the Mac (python3 on PATH)"; exit 1; }
	@cd $(ROOT) && set -a && { [ ! -f .env ] || . ./.env; } && set +a && python3 -m music2spotify review

apply: ## Push decisions from data/overrides.csv to Spotify (track lookups and sync, no search quota)
	$(RUN) match
	$(COMPOSE) run --rm $(NAMES) app sync

archive-matched: ## Dry run: list the files of tracks on Spotify to archive (CONFIRM=yes moves them, host only)
	@cd $(ROOT) && python3 -m music2spotify archive-matched $(if $(filter yes,$(CONFIRM)),--confirm)

restore-archived: ## Move archived files back to where Music had them (host only)
	@cd $(ROOT) && python3 -m music2spotify restore-archived

test: build ## Run the unit and end-to-end tests in Docker
	$(COMPOSE) run --rm --entrypoint python app -m unittest discover -s tests -t . -v

.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
COMPOSE := docker compose -f $(ROOT)/compose.yaml
RUN := $(COMPOSE) run --rm app
# `make sync PLAYLIST_NAMES=plain` overrides the value from .env for one run.
NAMES := $(if $(PLAYLIST_NAMES),-e PLAYLIST_NAMES=$(PLAYLIST_NAMES))
DATA := $(ROOT)/data

.PHONY: help build export smoke-export summary auth match rematch report accept plan sync test

help: ## List the targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

build: ## Build the Docker image
	$(COMPOSE) build

export: ## Snapshot the Music.app library to data/library.json (host, via osascript)
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

test: build ## Run the unit and end-to-end tests in Docker
	$(COMPOSE) run --rm --entrypoint python app -m unittest discover -s tests -t . -v

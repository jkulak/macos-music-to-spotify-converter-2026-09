.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
COMPOSE := docker compose -f $(ROOT)/compose.yaml
RUN := $(COMPOSE) run --rm app
DATA := $(ROOT)/data

.PHONY: help build export smoke-export summary test

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

test: build ## Run the unit and end-to-end tests in Docker
	$(COMPOSE) run --rm --entrypoint python app -m unittest discover -s tests -t . -v

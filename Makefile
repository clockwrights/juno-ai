# Juno — make targets.
#
# Every target has a `uv run` equivalent, listed here so Windows users without
# make can run the same command by hand:
#
#   make test        ->  uv run pytest tests infra/tests
#   make lint        ->  uv run ruff check . && uv run ruff format --check .
#   make types       ->  uv run mypy core ops scripts infra gates sandbox agent supervisor panel
#   make package     ->  uv run python scripts/build_lambdas.py
#   make synth       ->  npx cdk synth
#   make stage-up    ->  uv run python scripts/deploy.py --stage NAME
#   make stage-down  ->  npx cdk destroy --all -c stage=NAME --force
#   make params      ->  uv run python scripts/params.py init --stage STAGE
#   make backup      ->  uv run python scripts/backup.py --stage STAGE
#   make stop        ->  uv run python scripts/stop.py --stage STAGE
#   make destroy     ->  uv run python scripts/destroy.py --stage STAGE
#                       (I_KNOW=1 adds --i-know, required for prod; DRY_RUN=1 adds --dry-run)
#
# STAGE defaults to dev so that no bare invocation can touch prod. Under ADR-32
# there is only one permanent stage and it is named prod, so a bare `make backup`
# fails on a stage that does not exist — which is the point.

STAGE ?= dev
NAME  ?= $(STAGE)
UV    ?= uv
# The CDK CLI is pinned in package.json so local synth and CI agree.
CDK   ?= npx --no-install cdk

.DEFAULT_GOAL := help
.PHONY: help install lint fmt types test unit contract infra-tests invariants \
        synth package diff params stage-up stage-down integration backup stop start destroy ci

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Sync the virtualenv from uv.lock
	$(UV) sync

lint:  ## ruff check + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:  ## Apply ruff formatting and fixes
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

types:  ## mypy --strict
	$(UV) run mypy core ops scripts infra gates sandbox agent supervisor panel

unit:  ## Unit tests only
	$(UV) run pytest tests/unit

contract:  ## Contract tests only
	$(UV) run pytest tests/contract

infra-tests:  ## CDK assertion tests only
	$(UV) run pytest infra/tests

invariants:  ## Invariant clause check
	$(UV) run pytest tests/invariants_test.py

test:  ## Everything that runs without AWS credentials
	$(UV) run pytest tests infra/tests -m "not integration and not live"

ci: lint types  ## Exactly what .github/workflows/ci.yml runs, in the same order
	$(UV) run pytest tests -m "not integration and not live"
	$(UV) run pytest tests/invariants_test.py
	$(CDK) synth -c stage=dev -q
	$(CDK) synth -c stage=prod -q
	$(UV) run pytest infra/tests

synth:  ## Synthesize all stacks for STAGE
	$(CDK) synth -c stage=$(STAGE)

package:  ## Build the Lambda assets (ADR-26); required before a deploy
	$(UV) run python scripts/build_lambdas.py

# Deploys are local (ADR-38). scripts/deploy.py refuses prod off a clean main,
# builds the assets, passes `require_real_code` so a missing build is a
# synthesis error rather than a deployed placeholder (ADR-26), handles a low
# Lambda quota (ADR-34) and publishes the deployed prompts.
stage-up:  ## Deploy every stack for NAME (prod only from a clean main)
	$(UV) run python scripts/deploy.py --stage $(NAME)

diff: package  ## What `make stage-up` would change in NAME
	$(CDK) diff --all -c stage=$(NAME) -c require_real_code=1

stage-down:  ## Destroy a non-prod stage (see `make destroy` for the full sweep)
	@if [ "$(NAME)" = "prod" ]; then echo "refusing: use 'make destroy STAGE=prod' for prod"; exit 1; fi
	$(CDK) destroy --all -c stage=$(NAME) --force

params:  ## Write STAGE's SSM parameters; secrets are prompted for locally
	$(UV) run python scripts/params.py init --stage $(STAGE)

integration:  ## Integration tests against a deployed stage
	$(UV) run pytest -m integration -p no:xdist --stage $(NAME)

# ---------------------------------------------------------------- lifecycle
# ADR-27: backup, stop and destroy are each one command. See docs/14 §11.

backup:  ## Full offline export of STAGE
	$(UV) run python scripts/backup.py --stage $(STAGE) --verify

stop:  ## Pause STAGE and remove its Telegram webhook
	$(UV) run python scripts/stop.py --stage $(STAGE)

start:  ## Reverse `make stop`
	$(UV) run python scripts/start.py --stage $(STAGE)

# Script flags are passed as make variables: anything spelled `--flag` on a make
# command line is parsed by make itself (`--dry-run` is make's own -n, which
# prints the command and runs nothing).
destroy:  ## Stop, back up, then remove STAGE (prod needs I_KNOW=1; DRY_RUN=1 to preview)
	$(UV) run python scripts/destroy.py --stage $(STAGE) $(if $(I_KNOW),--i-know) $(if $(DRY_RUN),--dry-run)

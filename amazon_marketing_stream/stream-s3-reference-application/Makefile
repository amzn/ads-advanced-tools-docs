.DEFAULT_GOAL := help
PY ?= python3
CDK ?= cdk

# Deployment settings. Override on the command line:
#   make deploy-staging GEO=NA REGION=us-east-1 ALARM_EMAIL=you@example.com
#
# REGION is not defaulted on purpose. The delivery service permits only certain regions per
# geography and the staging layer must sit in the same one as its destination, so guessing here
# would turn a wrong region into a silent cross-region deployment.
GEO ?= NA
REGION ?=
# cloudformation or cdk. Both build the same resources; neither uploads the job artifacts, which
# is why the deploy targets exist rather than being a bare `aws deploy` or `cdk deploy`.
ENGINE ?= cloudformation
# Required by ENGINE=cloudformation for the staging and QuickSight layers, whose templates are both
# over CloudFormation's 51,200-byte limit for a template sent in the API call; past that the service
# requires it to be uploaded to S3 first, and nothing creates a bucket for you — aws cloudformation
# deploy has no --resolve-s3. Any bucket you can write to will do. ENGINE=cdk needs none, because
# CDK uploads to its own bootstrap bucket.
TEMPLATE_BUCKET ?=
STAGING_STACK ?= stream-staging
DESTINATION_STACK ?= stream-destination
DATASETS ?= ads-performance-v1,ads-converted-product-v1
ALARM_EMAIL ?=
# Extra arguments passed straight through, e.g. DEPLOY_ARGS=--dry-run
DEPLOY_ARGS ?=

# An override, not a requirement. The delivery service's account is a fixed published value and is
# defaulted in the template, the CDK stack and the deploy tool. Set this only if your integration
# guide gives you a different one.
PUBLISHER_ACCOUNT_ID ?=

QUICKSIGHT_STACK ?= stream-quicksight
# Not defaulted, for the same reason PUBLISHER_ACCOUNT_ID is not: a QuickSight principal belongs
# to one account and one region, so any default here would be wrong everywhere.
QUICKSIGHT_USER_ARN ?=
# A different kind of ARN from QUICKSIGHT_USER_ARN, and optional. Lake Formation takes an IAM
# principal and rejects a quicksight: user ARN with AccessDenied, so the grants that let a human
# query these tables in the Athena console need the IAM role behind that user. Left empty, those
# five grants are skipped; the dashboard reads through the role the stack creates and does not
# need them.
QUICKSIGHT_LF_PRINCIPAL_ARN ?=
# Normally left empty. deploy-quicksight derives the Athena catalog from the deployed staging
# stack, because that is the value that is easy to get wrong by hand and fails silently when it
# is. Set this only for a catalog registered under some other name.
ATHENA_CATALOG ?=
# Set to yes to have `make undeploy` remove the QuickSight layer too.
INCLUDE_QUICKSIGHT ?=

.PHONY: help install lock lint fmt test test-unit test-e2e test-spark cov check scan clean \
	release-check bootstrap deploy-all deploy-destination deploy-staging deploy-quicksight verify \
	undeploy

help: ## Show available targets
	@# The character class includes digits: without them 'test-e2e' is silently omitted.
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-19s %s\n", $$1, $$2}'

install: ## Install the package with development extras
	$(PY) -m pip install -e '.[dev]'

# The lock is what CI installs: every package, transitive ones included, at one exact version and
# with its hashes, so a run next month installs what this one did and a tampered download fails.
# One file for every supported Python: --universal writes environment markers where a version
# differs by interpreter. Needs uv to regenerate; installing from it needs only pip.
lock: ## Regenerate requirements-lock.txt from pyproject.toml and the CDK requirements
	uv pip compile pyproject.toml infrastructure/cdk/requirements.txt --extra dev \
		--universal --python-version 3.10 --generate-hashes --quiet \
		--custom-compile-command 'make lock' -o requirements-lock.txt

# infrastructure/ is included: the CDK app and the Glue entry scripts are Python that ships, and
# leaving them out meant the deployment code was the only code CI did not check.
lint: ## Static analysis
	ruff check src tests tools infrastructure
	ruff format --check src tests tools infrastructure
# The package ships py.typed, which tells every consumer's type checker to trust these
# annotations, so they are checked here rather than taken on trust.
	mypy src
# cfn-lint validates the templates against the AWS resource schemas. A test that reads a template
# and asserts the template cannot catch a property shape both engines got wrong.
	cfn-lint infrastructure/cloudformation/*.yaml

fmt: ## Apply formatting
	ruff format src tests tools infrastructure
	ruff check --fix src tests tools infrastructure

test-unit: ## Unit tests only
	$(PY) -m pytest tests/unit -q

test-e2e: ## End-to-end tests only
	$(PY) -m pytest tests/e2e -q

# /usr/libexec/java_home exists on macOS only, and its failure was swallowed: on Linux JAVA_HOME
# came out empty, the Spark tests skipped, and the target reported a pass. Those are the tests
# that assert the generated MERGE INTO agrees with the Python reference, so a skip nobody sees is
# the one outcome worth preventing. The fallback stays; the skip is now announced. A missing JDK
# is still a skip rather than a failure, because the suite has to run for people without one.
test-spark: ## Spark integration tests against a local Iceberg catalog (needs a JDK)
	@JDK="$${JAVA_HOME:-$$(/usr/libexec/java_home -v 17 2>/dev/null)}"; \
	if [ -z "$$JDK" ]; then \
		echo "test-spark: no JDK found. JAVA_HOME is unset and /usr/libexec/java_home returned"; \
		echo "test-spark: nothing (it is macOS-only). tests/spark will SKIP, not pass -- the"; \
		echo "test-spark: generated-SQL differential tests will NOT have run. Install a JDK 17"; \
		echo "test-spark: or set JAVA_HOME to run them."; \
	fi; \
	JAVA_HOME="$$JDK" $(PY) -m pytest tests/spark -q

test: ## Full test suite
	$(PY) -m pytest -q

cov: ## Full suite with coverage
	$(PY) -m pytest --cov=stream_staging --cov-report=term-missing --cov-report=xml \
		--cov-fail-under=85

scan: ## Fail if identifying information or credentials leaked into the tree
	@$(PY) tools/scan_repo.py

release-check: check ## Everything CI runs, plus the gates that must pass before publishing
	@echo ""
	@echo "Release gates:"
	$(PY) -m pytest -q -m release -p no:cacheprovider

check: lint test scan ## Everything CI runs

# Pin the bootstrap environment to the account returned by the active AWS credentials and the region
# typed by the operator. A bare `cdk bootstrap` can inherit either from ambient configuration and
# prepare a different account or region while still reporting success. Idempotent: re-running
# updates the same CDKToolkit stack rather than creating another one.
bootstrap: ## Prepare an account and region for CDK once (needs REGION)
	@test -n "$(REGION)" || { \
		echo "REGION is required, e.g. make bootstrap REGION=eu-west-2"; \
		exit 1; \
	}
	$(CDK) bootstrap "aws://$$(aws --region '$(REGION)' sts get-caller-identity --query Account --output text)/$(REGION)"

# One command, still two stacks. The ordering is a data dependency, not a preference: the staging
# layer's required inputs (delivery bucket, notification queue, KMS key) are outputs of the
# destination, and `deploy-staging` reads them from the deployed stack. Reverse these two and there
# is nothing to read.
#
# Merging the templates instead was considered and rejected. The destination holds delivered data
# that cannot be regenerated -- it is the rebuild source for the 70-day restatement window -- while
# staging and the dashboards are entirely derived. One stack would let a single `delete-stack` take
# the irreplaceable layer with it, and would not remove this wrapper anyway, because enabling the
# S3 Tables integration, creating the subscription and uploading the job artifacts cannot live in a
# template. See AWS's "Organize your stacks by lifecycle and ownership".
#
# Two things are deliberately NOT here:
#   - the subscription, because it needs Ads API credentials and a token that expires hourly, and
#     because it is an external side effect that should not hide inside a deploy. It is printed.
#   - QuickSight, because it depends on four console steps no template can create, so including it
#     would build resources that exist and can read nothing. It is also what keeps
#     `DEPLOY_ARGS=--dry-run` fully offline, along with not invoking `make verify`, which has no
#     dry run and would reach for real credentials.
deploy-all: ## Deploy destination + staging; print the manual next steps (needs GEO and REGION)
	@test -n "$(REGION)" || { \
		echo "REGION is required. Both stacks must be deployed in the same region, and passing"; \
		echo "it once here is what lets the second one check that."; \
		echo ""; \
		echo "  make deploy-all GEO=NA REGION=us-east-1"; \
		exit 1; \
	}
	$(MAKE) deploy-destination GEO=$(GEO) REGION=$(REGION) ENGINE=$(ENGINE) \
		DESTINATION_STACK=$(DESTINATION_STACK) \
		PUBLISHER_ACCOUNT_ID='$(PUBLISHER_ACCOUNT_ID)' \
		ALARM_EMAIL='$(ALARM_EMAIL)' \
		DEPLOY_ARGS='$(DEPLOY_ARGS)'
	$(MAKE) deploy-staging GEO=$(GEO) REGION=$(REGION) ENGINE=$(ENGINE) \
		STAGING_STACK=$(STAGING_STACK) DESTINATION_STACK=$(DESTINATION_STACK) \
		DATASETS=$(DATASETS) \
		ALARM_EMAIL='$(ALARM_EMAIL)' \
		TEMPLATE_BUCKET='$(TEMPLATE_BUCKET)' \
		DEPLOY_ARGS='$(DEPLOY_ARGS)'
	@echo ""
	@echo "Both stacks are deployed. One step left, and no template can do it:"
	@echo ""
	@echo "  stream-subscription --accounts <advertiser-account-id> create --geo $(GEO) --region $(REGION)"
	@echo ""
	@echo "Data starts arriving 1-2 hours after that. Then check it with:"
	@echo ""
	@echo "  make verify REGION=$(REGION) ENGINE=$(ENGINE)"
	@echo ""
	@echo "After rows arrive, deploy the optional dashboard layer with:"
	@echo ""
	@echo "  make deploy-quicksight REGION=$(REGION) ENGINE=$(ENGINE) QUICKSIGHT_USER_ARN=<arn>$(if $(TEMPLATE_BUCKET), TEMPLATE_BUCKET=$(TEMPLATE_BUCKET),)"

deploy-destination: ## Enable S3 Tables and deploy the destination (needs GEO and REGION)
	@test -n "$(REGION)" || { echo "REGION is required, e.g. make deploy-destination GEO=NA REGION=us-east-1"; exit 1; }
	$(PY) tools/deploy_destination.py \
		--geo $(GEO) \
		--region $(REGION) \
		$(if $(PUBLISHER_ACCOUNT_ID),--publisher-account-id $(PUBLISHER_ACCOUNT_ID),) \
		--engine $(ENGINE) \
		--stack-name $(DESTINATION_STACK) \
		$(if $(ALARM_EMAIL),--alarm-email $(ALARM_EMAIL),) \
		$(DEPLOY_ARGS)

deploy-staging: ## Build, deploy and upload the staging layer (needs GEO and REGION)
	@test -n "$(REGION)" || { \
		echo "REGION is required. The staging layer must be deployed in the same region as"; \
		echo "the destination it reads, and passing it is what enables that check."; \
		echo ""; \
		echo "  make deploy-staging GEO=NA REGION=us-east-1"; \
		exit 1; \
	}
	$(PY) tools/deploy_staging.py \
		--geo $(GEO) \
		--region $(REGION) \
		--stack-name $(STAGING_STACK) \
		--destination-stack $(DESTINATION_STACK) \
		--engine $(ENGINE) \
		--datasets $(DATASETS) \
		$(if $(ALARM_EMAIL),--alarm-email $(ALARM_EMAIL),) \
		$(if $(TEMPLATE_BUCKET),--template-bucket $(TEMPLATE_BUCKET),) \
		$(DEPLOY_ARGS)

# The CDK app names the staging stack "<deploymentName>-staging", so with ENGINE=cdk the stack to
# read is stream-staging-staging while the job and rule prefix stays stream-staging. Two different
# names for the same deployment, which is why they are separate flags rather than one.
VERIFY_STACK = $(if $(filter cdk,$(ENGINE)),$(STAGING_STACK)-staging,$(STAGING_STACK))

verify: ## Check the deployed staging layer is actually working (needs REGION)
	@test -n "$(REGION)" || { echo "REGION is required, e.g. make verify REGION=us-east-1"; exit 1; }
	$(PY) tools/verify.py \
		--region $(REGION) \
		--stack-name $(VERIFY_STACK) \
		--deployment-name $(STAGING_STACK) \
		--datasets $(DATASETS) \
		$(DEPLOY_ARGS)

# Deploy this after the staging layer, not before: it reads TableBucketArn out of that stack to
# work out the Athena catalog name. Deployed on its own so the staging layer can be rebuilt
# without disturbing the dashboards.
deploy-quicksight: ## Deploy the optional QuickSight dashboards (needs REGION and QUICKSIGHT_USER_ARN)
	@test -n "$(REGION)" || { echo "REGION is required, e.g. make deploy-quicksight REGION=us-east-1 QUICKSIGHT_USER_ARN=<arn>"; exit 1; }
	@test -n "$(QUICKSIGHT_USER_ARN)" || { \
		echo "QUICKSIGHT_USER_ARN is required. A QuickSight principal belongs to one account and"; \
		echo "one region, so it cannot be defaulted. Find yours with:"; \
		echo ""; \
		echo "  aws quicksight list-users --aws-account-id <account> --namespace default --region $(REGION)"; \
		echo ""; \
		echo "  make deploy-quicksight REGION=$(REGION) QUICKSIGHT_USER_ARN=<arn>"; \
		exit 1; \
	}
	$(PY) tools/deploy_quicksight.py \
		--region $(REGION) \
		--engine $(ENGINE) \
		--stack-name $(QUICKSIGHT_STACK) \
		--staging-stack $(VERIFY_STACK) \
		--deployment-name $(STAGING_STACK) \
		--quicksight-user-arn $(QUICKSIGHT_USER_ARN) \
		$(if $(QUICKSIGHT_LF_PRINCIPAL_ARN),--quicksight-lakeformation-principal-arn $(QUICKSIGHT_LF_PRINCIPAL_ARN),) \
		$(if $(ATHENA_CATALOG),--athena-catalog $(ATHENA_CATALOG),) \
		$(if $(TEMPLATE_BUCKET),--template-bucket $(TEMPLATE_BUCKET),) \
		$(DEPLOY_ARGS)

# CONFIRM is not defaulted on purpose. This deletes tables, so the acknowledgement has to be
# typed at the call site rather than supplied by the Makefile -- otherwise "make undeploy" alone
# would destroy state, which is exactly the accident the flag exists to prevent.
undeploy: ## Tear down the staging layer (needs REGION and CONFIRM=yes; try DEPLOY_ARGS=--dry-run first)
	@test -n "$(REGION)" || { echo "REGION is required, e.g. make undeploy REGION=us-east-1 CONFIRM=yes"; exit 1; }
	$(PY) tools/undeploy.py \
		--region $(REGION) \
		--stack-name $(VERIFY_STACK) \
		--destination-stack $(DESTINATION_STACK) \
		--quicksight-stack $(QUICKSIGHT_STACK) \
		$(if $(filter yes,$(INCLUDE_QUICKSIGHT)),--include-quicksight,) \
		$(if $(filter yes,$(CONFIRM)),--confirm,) \
		$(DEPLOY_ARGS)

clean: ## Remove build and test artifacts
	rm -rf build dist .pytest_cache .ruff_cache .coverage coverage.xml htmlcov
	rm -rf infrastructure/cdk/cdk.out
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find src -name '*.egg-info' -type d -prune -exec rm -rf {} +

#!/usr/bin/env bash
# Deployment script for the billing service.

set -euo pipefail

DEPLOY_ENV="${1:-staging}"

deploy_app() {
  echo "deploying billing service to ${DEPLOY_ENV}"
  docker build -t billing-service .
  docker push "billing-service:${DEPLOY_ENV}"
}

rollback_app() {
  echo "rolling back billing service on ${DEPLOY_ENV}"
  docker service update --rollback billing-service
}

deploy_app

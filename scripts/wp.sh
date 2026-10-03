#!/usr/bin/env sh
# Run WP-CLI inside the dev store:  scripts/wp.sh option get siteurl
set -eu
cd "$(dirname "$0")/.."
exec docker compose -f docker/docker-compose.yml run --rm -T wpcli wp "$@"

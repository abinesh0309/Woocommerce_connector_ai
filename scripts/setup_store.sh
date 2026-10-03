#!/usr/bin/env sh
# Create the dev store: WordPress + WooCommerce + fictional seed data.
#   ORDER_STORAGE=hpos|legacy ./scripts/setup_store.sh      (default: hpos)
set -eu
cd "$(dirname "$0")/.."
ORDER_STORAGE="${ORDER_STORAGE:-hpos}"
COMPOSE="docker compose -f docker/docker-compose.yml"
WP="./scripts/wp.sh"

$COMPOSE up -d db wordpress caddy
echo "Waiting for WordPress files..."
for i in $(seq 1 60); do $COMPOSE exec -T wordpress test -f /var/www/html/wp-load.php 2>/dev/null && break; sleep 2; done
# wp-config.php is written by the image's entrypoint on first start:
for i in $(seq 1 60); do $COMPOSE exec -T wordpress test -f /var/www/html/wp-config.php 2>/dev/null && break; sleep 2; done

if ! $WP core is-installed 2>/dev/null; then
  $WP core install --url="http://localhost:8080" --title="Connector Dev Store" \
    --admin_user=admin --admin_password=admin --admin_email=admin@example.test --skip-email
fi
$WP plugin is-installed woocommerce || $WP plugin install woocommerce --activate
$WP plugin activate woocommerce
$WP rewrite structure '/%postname%/' --hard   # pretty permalinks: wc/v3 needs them

if [ "$ORDER_STORAGE" = "legacy" ]; then
  $WP option update woocommerce_custom_orders_table_enabled no
  $WP option update woocommerce_custom_orders_table_data_sync_enabled no
else
  $WP option update woocommerce_custom_orders_table_enabled yes
  $WP option update woocommerce_custom_orders_table_data_sync_enabled no
fi
$WP option update woocommerce_currency USD
$WP option update woocommerce_default_country US:CA
$WP option update woocommerce_onboarding_profile '{"skipped":true}' --format=json || true

if [ "$($WP option get connector_dev_seeded 2>/dev/null || true)" != "1" ]; then
  $WP eval-file /seed/seed.php
  $WP option update connector_dev_seeded 1
fi

echo
echo "Dev store ready: http://localhost:8080  (admin / admin), TLS proxy: https://localhost:8443  [order storage: $ORDER_STORAGE]"
echo "Connect the connector (read-only):"
echo "  uv run woo-connector --store http://localhost:8080 auth --no-browser --callback-host host.docker.internal"
echo "  then approve in the browser, or:  uv run python scripts/approve_local.py '<printed URL>'"

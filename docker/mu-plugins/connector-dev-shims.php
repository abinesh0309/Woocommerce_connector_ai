<?php
/**
 * Plugin Name: Connector dev shims (DEV ONLY)
 * Description: Lets WooCommerce's consent flow call back to a local HTTPS listener with a self-signed cert,
 *              and simulates a host-level rate limit. NEVER install this on a real store.
 */

defined( 'ABSPATH' ) || exit;

// 1. wc-auth POSTs the new API key with wp_safe_remote_post(), which refuses private hosts, odd ports and self-signed certs.
add_filter( 'http_request_host_is_external', function ( $external, $host ) {
	return in_array( $host, array( 'host.docker.internal', 'localhost', '127.0.0.1' ), true ) ? true : $external;
}, 10, 2 );
add_filter( 'http_allowed_safe_ports', function ( $ports ) {
	return array_merge( (array) $ports, range( 1024, 65535 ) );
} );
add_filter( 'http_request_args', function ( $args, $url ) {
	$host = wp_parse_url( $url, PHP_URL_HOST );
	if ( in_array( $host, array( 'host.docker.internal', 'localhost', '127.0.0.1' ), true ) ) {
		$args['sslverify'] = false;
	}
	return $args;
}, 10, 2 );

// 2. Simulated rate limit: `scripts/wp.sh option update connector_dev_rate_limit 5` allows 5 REST requests per 10 s, then answers 429.
add_filter( 'rest_pre_dispatch', function ( $result ) {
	$limit = (int) get_option( 'connector_dev_rate_limit', 0 );
	if ( $limit <= 0 ) {
		return $result;
	}
	$now  = microtime( true );
	$hits = array_filter( (array) get_option( 'connector_dev_hits', array() ), function ( $t ) use ( $now ) {
		return $now - $t < 10;
	} );
	if ( count( $hits ) >= $limit ) {
		update_option( 'connector_dev_hits', $hits, false );
		header( 'Retry-After: 2' );
		return new WP_Error( 'too_many_requests', 'Too many requests (simulated).', array( 'status' => 429 ) );
	}
	$hits[] = $now;
	update_option( 'connector_dev_hits', array_values( $hits ), false );
	return $result;
}, 1 );

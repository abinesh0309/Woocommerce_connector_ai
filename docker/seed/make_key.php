<?php
/**
 * Create a READ-ONLY WooCommerce REST API key for the admin user and print it as JSON (last line of output).
 * Used by scripts/fault_check.py to simulate "the merchant reconnects". Dev only.
 */
global $wpdb;
$user = get_user_by( 'login', 'admin' );
$ck   = 'ck_' . wc_rand_hash();
$cs   = 'cs_' . wc_rand_hash();
$wpdb->insert(
	$wpdb->prefix . 'woocommerce_api_keys',
	array(
		'user_id'         => $user->ID,
		'description'     => 'Woo Connector (read-only) dev',
		'permissions'     => 'read',
		'consumer_key'    => wc_api_hash( $ck ),
		'consumer_secret' => $cs,
		'truncated_key'   => substr( $ck, -7 ),
	)
);
echo wp_json_encode( array( 'consumer_key' => $ck, 'consumer_secret' => $cs ) ) . "\n";

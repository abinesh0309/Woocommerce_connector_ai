from urllib.parse import parse_qsl, urlsplit

from woo_connector.woocommerce import oauth1

# Independent vector: the signature below was computed with `openssl dgst -sha256 -hmac 'cs_secret&' -binary | base64`
# over the base string written out by hand, not with this module.
VECTOR = "wpa/Ziazd0OJ5cKwmhWDdW0sLZ5pHh6H1MkfACDrkRE="
PARAMS = [
    ("per_page", "1"),
    ("search", "a b"),
    ("oauth_consumer_key", "ck_test"),
    ("oauth_timestamp", "1700000000"),
    ("oauth_nonce", "abc123"),
    ("oauth_signature_method", "HMAC-SHA256"),
]


def test_signature_matches_independent_vector():
    assert oauth1.signature("GET", "http://shop.test/wp-json/wc/v3/orders", PARAMS, "cs_secret") == VECTOR


def test_secret_without_trailing_ampersand_gives_a_different_signature():
    import base64
    import hashlib
    import hmac

    base = "&".join(["GET", oauth1.rfc3986("http://shop.test/wp-json/wc/v3/orders"), oauth1.rfc3986(oauth1.normalize(PARAMS))])
    wrong = base64.b64encode(hmac.new(b"cs_secret", base.encode(), hashlib.sha256).digest()).decode()
    assert wrong != VECTOR


def test_parameters_sorted_by_encoded_key_then_value():
    assert oauth1.normalize([("b", "2"), ("a", "z"), ("a", "b"), ("B", "x")]) == "B=x&a=b&a=z&b=2"


def test_rfc3986_encoding():
    assert oauth1.rfc3986("a b+c/d~e") == "a%20b%2Bc%2Fd~e"


def test_sign_url_is_fresh_every_time_and_verifiable():
    u1 = oauth1.sign_url("GET", "http://shop.test/wp-json/wc/v3/orders", [("per_page", "1")], "ck", "cs")
    u2 = oauth1.sign_url("GET", "http://shop.test/wp-json/wc/v3/orders", [("per_page", "1")], "ck", "cs")
    q1, q2 = dict(parse_qsl(urlsplit(u1).query)), dict(parse_qsl(urlsplit(u2).query))
    assert q1["oauth_nonce"] != q2["oauth_nonce"]
    assert q1["oauth_signature_method"] == "HMAC-SHA256"
    given = q1.pop("oauth_signature")
    assert oauth1.signature("GET", "http://shop.test/wp-json/wc/v3/orders", list(q1.items()), "cs") == given

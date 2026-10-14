import pytest

from bgremover.access import DomainPolicy, DuplicateRule, InvalidRule, normalize_pattern
from bgremover.db import Database


@pytest.fixture
def policy():
    return DomainPolicy(Database(":memory:"))


def test_blocks_everything_by_default(policy):
    assert not policy.check("https://example.com").allowed
    assert not policy.check(None).allowed


def test_exact_host_any_scheme_and_port(policy):
    policy.add_rule("example.com")
    assert policy.check("https://example.com").allowed
    assert policy.check("http://example.com:8080").allowed
    assert not policy.check("https://www.example.com").allowed
    assert not policy.check("https://example.com.evil.io").allowed
    assert not policy.check("https://notexample.com").allowed


def test_wildcard_matches_subdomains_only(policy):
    policy.add_rule("*.example.com")
    assert policy.check("https://a.example.com").allowed
    assert policy.check("https://a.b.example.com").allowed
    assert not policy.check("https://example.com").allowed
    assert not policy.check("https://badexample.com").allowed


def test_referer_fallback_and_cors_echo(policy):
    policy.add_rule("example.com")
    d = policy.check(None, "https://example.com/some/page?q=1")
    assert d.allowed and d.host == "example.com" and d.origin == "https://example.com"


def test_disable_and_delete(policy):
    rule = policy.add_rule("example.com")
    policy.update_rule(rule.id, enabled=False)
    assert not policy.check("https://example.com").allowed
    policy.update_rule(rule.id, enabled=True)
    assert policy.check("https://example.com").allowed
    policy.delete_rule(rule.id)
    assert not policy.check("https://example.com").allowed


def test_no_origin_setting(policy):
    policy.set_allow_no_origin(True)
    assert policy.check(None).allowed
    assert not policy.check("https://example.com").allowed


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Example.COM", "example.com"),
        ("https://shop.example.com/cart", "shop.example.com"),
        ("example.com:3000", "example.com"),
        ("*.Example.com", "*.example.com"),
        ("localhost", "localhost"),
        ("bücher.de", "xn--bcher-kva.de"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_pattern(raw) == expected


@pytest.mark.parametrize("raw", ["", "*", "*.", "exa mple.com", "-bad.com", "a..b", "foo.*.com"])
def test_invalid_patterns(raw):
    with pytest.raises(InvalidRule):
        normalize_pattern(raw)


def test_duplicates_rejected(policy):
    policy.add_rule("example.com")
    with pytest.raises(DuplicateRule):
        policy.add_rule("https://EXAMPLE.com/")

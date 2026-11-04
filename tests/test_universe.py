from scripts.universe_candidates import bn_base, hl_base


def test_hl_base_strips_k_prefix_only_before_uppercase():
    assert hl_base("kPEPE") == ("PEPE", 1000)
    assert hl_base("BTC") == ("BTC", 1)
    assert hl_base("kaito") == ("kaito", 1)  # lower-case after k: not a unit prefix


def test_bn_base_strips_numeric_unit_prefixes():
    assert bn_base("1000PEPE") == ("PEPE", 1000)
    assert bn_base("1000000MOG") == ("MOG", 1_000_000)
    assert bn_base("1MBABYDOGE") == ("BABYDOGE", 1_000_000)
    assert bn_base("BTC") == ("BTC", 1)
    assert bn_base("1INCH") == ("1INCH", 1)  # a ticker that starts with a digit, not a prefix

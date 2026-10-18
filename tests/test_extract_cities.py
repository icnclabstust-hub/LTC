from app.db.queries import SERVICE_CITIES, extract_cities, is_in_service_area


def test_explicit_city_overrides_default():
    assert extract_cities("台南市有哪些長照據點？") == ("臺南市",)


def test_bare_out_of_area_city_is_recognized():
    assert extract_cities("台南有哪些長照據點？") == ("臺南市",)


def test_bare_chiayi_matches_both_city_and_county():
    assert extract_cities("嘉義有哪些長照據點？") == ("嘉義市", "嘉義縣")


def test_no_city_falls_back_to_service_area():
    assert extract_cities("附近有哪些長照據點？") == SERVICE_CITIES


def test_only_chiayi_is_in_service_area():
    assert is_in_service_area(("嘉義縣",))
    assert not is_in_service_area(("臺南市",))

import pytest

PUBLIC_ROUTES = [
    "/",
    "/login",
    "/signup",
    "/search",
    "/rss.xml",
    "/privacy",
    "/terms",
    "/youth-policy",
    "/correction-policy",
    "/about",
    "/ethics",
    "/subscribe-info",
    "/healthz",
    "/robots.txt",
    "/sitemap.xml",
    "/contact",
    "/reporter/reporter",
]


@pytest.mark.parametrize("path", PUBLIC_ROUTES)
def test_public_route_returns_200(app_and_client, path):
    _, client = app_and_client
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} 이(가) {resp.status_code}를 반환했습니다"


def test_healthz_reports_db_ok(app_and_client):
    _, client = app_and_client
    resp = client.get("/healthz")
    assert resp.get_json() == {"status": "ok", "db": "ok"}


def test_category_filter_page_works(app_and_client):
    _, client = app_and_client
    resp = client.get("/?category=임상시험")
    assert resp.status_code == 200


def test_negative_page_number_does_not_crash(app_and_client):
    _, client = app_and_client
    resp = client.get("/?category=임상시험&page=-5")
    assert resp.status_code == 200


def test_non_numeric_page_number_does_not_crash(app_and_client):
    _, client = app_and_client
    resp = client.get("/?category=임상시험&page=abc")
    assert resp.status_code in (200, 400)


def test_unknown_page_returns_custom_404(app_and_client):
    _, client = app_and_client
    resp = client.get("/no-such-page-xyz")
    assert resp.status_code == 404


def test_newsletter_signup_and_duplicate(app_and_client):
    _, client = app_and_client
    resp = client.get("/")
    import re

    token = re.search(r'name="csrf_token" value="([^"]+)"', resp.get_data(as_text=True)).group(1)
    r1 = client.post("/newsletter", data={"email": "dup@test.com", "csrf_token": token}, follow_redirects=True)
    assert "구독이 완료" in r1.get_data(as_text=True)

    resp2 = client.get("/")
    token2 = re.search(r'name="csrf_token" value="([^"]+)"', resp2.get_data(as_text=True)).group(1)
    r2 = client.post("/newsletter", data={"email": "dup@test.com", "csrf_token": token2}, follow_redirects=True)
    assert "이미 구독" in r2.get_data(as_text=True)

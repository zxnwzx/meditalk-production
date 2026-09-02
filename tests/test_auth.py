from conftest import login, get_token_for


def test_login_with_correct_credentials_redirects_to_admin(app_and_client):
    _, client = app_and_client
    resp = login(client, "editor", "editor1234")
    assert resp.status_code == 302
    assert "/admin" in resp.headers["Location"]


def test_login_with_wrong_password_fails(app_and_client):
    _, client = app_and_client
    resp = login(client, "editor", "wrongpassword")
    assert resp.status_code == 200
    assert "올바르지 않습니다" in resp.get_data(as_text=True)


def test_csrf_missing_token_rejected(app_and_client):
    _, client = app_and_client
    resp = client.post("/login", data={"username": "editor", "password": "editor1234"})
    assert resp.status_code == 400


def test_login_rate_limit_blocks_after_max_attempts(app_and_client):
    app_module, client = app_and_client
    blocked_at = None
    for i in range(app_module.LOGIN_MAX_ATTEMPTS + 1):
        token = get_token_for(client, "/login")
        resp = client.post(
            "/login",
            data={"username": "editor", "password": f"wrong{i}", "csrf_token": token},
        )
        if "너무 많습니다" in resp.get_data(as_text=True):
            blocked_at = i + 1
            break
    assert blocked_at == app_module.LOGIN_MAX_ATTEMPTS + 1


def test_signup_creates_reader_account(app_and_client):
    _, client = app_and_client
    token = get_token_for(client, "/signup")
    resp = client.post(
        "/signup",
        data={"name": "테스트유저", "username": "testuser1", "password": "12345678", "csrf_token": token},
    )
    assert resp.status_code == 302
    login_resp = login(client, "testuser1", "12345678")
    assert login_resp.status_code == 302


def test_signup_rejects_short_password(app_and_client):
    _, client = app_and_client
    token = get_token_for(client, "/signup")
    resp = client.post(
        "/signup",
        data={"name": "테스트유저", "username": "shortpw", "password": "1234", "csrf_token": token},
    )
    assert "8자 이상" in resp.get_data(as_text=True)


def test_session_id_changes_after_login(app_and_client):
    """세션 고정(session fixation) 공격 방지 확인 — 로그인 전후 세션 쿠키가 달라져야 합니다."""
    _, client = app_and_client
    client.get("/login")
    before = client.get_cookie("session")
    login(client, "editor", "editor1234")
    after = client.get_cookie("session")
    assert before is None or before.value != after.value

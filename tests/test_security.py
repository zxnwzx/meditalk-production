from conftest import login, get_token_for


def test_search_escapes_xss_payload(app_and_client):
    _, client = app_and_client
    resp = client.get("/search?q=%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script>alert(1)</script>" not in resp.get_data(as_text=True)


def test_search_handles_sql_injection_attempt_safely(app_and_client):
    _, client = app_and_client
    resp = client.get("/search?q=%27%20OR%201%3D1")
    assert resp.status_code == 200  # 에러 없이 정상 처리되어야 함(결과 0건이어도 무방)


def test_reporter_cannot_access_inquiries(app_and_client):
    """IDOR 방지 — 기자는 광고문의(편집장 전용) 목록에 접근할 수 없어야 합니다."""
    _, client = app_and_client
    login(client, "reporter", "reporter1234")
    resp = client.get("/admin/inquiries", follow_redirects=True)
    assert "권한이 필요합니다" in resp.get_data(as_text=True)


def test_reporter_cannot_access_member_list(app_and_client):
    _, client = app_and_client
    login(client, "reporter", "reporter1234")
    resp = client.get("/admin/members", follow_redirects=True)
    assert "권한이 필요합니다" in resp.get_data(as_text=True)


def test_anonymous_cannot_access_admin_dashboard(app_and_client):
    _, client = app_and_client
    resp = client.get("/admin")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_path_traversal_on_uploads_blocked(app_and_client):
    _, client = app_and_client
    resp = client.get("/static/uploads/../../../app.py")
    assert resp.status_code != 200


def test_security_headers_present(app_and_client):
    _, client = app_and_client
    resp = client.get("/")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in resp.headers


def test_admin_pages_are_not_cached_by_browser(app_and_client):
    _, client = app_and_client
    login(client, "editor", "editor1234")
    resp = client.get("/admin")
    assert "no-store" in resp.headers.get("Cache-Control", "")


def test_reporter_cannot_edit_published_article(app_and_client):
    """발행된 기사는 (본인이 쓴 기사라도) 편집장만 수정할 수 있어야 합니다."""
    _, client = app_and_client
    # 기자 본인이 기사를 작성해서 심사 요청합니다.
    login(client, "reporter", "reporter1234")
    token = get_token_for(client, "/admin/write")
    client.post(
        "/admin/write",
        data={
            "title": "수정권한테스트",
            "category": "임상시험",
            "deck": "d",
            "body": "b",
            "action": "submit_review",
            "csrf_token": token,
        },
    )
    client.get("/logout")

    import sqlite3
    import os

    db = sqlite3.connect(os.environ["MEDITOK_DB_PATH"])
    aid = db.execute("SELECT id FROM articles WHERE title='수정권한테스트'").fetchone()[0]

    # 편집장이 그 기사를 발행합니다.
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/review")
    client.post(f"/admin/publish/{aid}", data={"csrf_token": token})
    client.get("/logout")

    # 원작성자인 기자 본인이라도, 발행된 기사는 이제 편집장만 수정할 수 있어야 합니다.
    login(client, "reporter", "reporter1234")
    resp = client.get(f"/admin/edit/{aid}", follow_redirects=True)
    assert "편집장만 수정" in resp.get_data(as_text=True)

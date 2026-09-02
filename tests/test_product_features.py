from conftest import get_csrf_token, get_token_for, login


def test_reader_topic_follow_and_newsletter_preferences_are_persisted(app_and_client):
    app, client = app_and_client
    signup = client.get("/signup")
    token = get_csrf_token(signup.get_data(as_text=True))
    response = client.post(
        "/signup", data={"name": "관심 독자", "username": "topic_reader", "password": "password123", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 302
    login(client, "topic_reader", "password123")
    token = get_token_for(client, "/mypage")
    response = client.post(
        "/topics/toggle", data={"csrf_token": token, "topic_type": "category", "topic_value": "임상시험"},
        follow_redirects=True,
    )
    assert "임상시험을(를) 팔로우했습니다" in response.get_data(as_text=True)
    assert "관심 주제 새 기사" in response.get_data(as_text=True)

    token = get_token_for(client, "/")
    response = client.post(
        "/newsletter", data={"csrf_token": token, "email": "topic@example.com", "categories": ["임상시험", "정책·규제"]},
        follow_redirects=True,
    )
    assert "뉴스레터 구독이 완료됐습니다" in response.get_data(as_text=True)
    with app.app.app_context():
        db = app.get_db()
        assert db.execute("SELECT COUNT(*) FROM topic_follows WHERE topic_value='임상시험'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM newsletter_preferences WHERE email='topic@example.com'").fetchone()[0] == 2


def test_editor_article_history_snapshot_and_restore(app_and_client):
    app, client = app_and_client
    login(client, "editor", "editor1234")
    with app.app.app_context():
        article = app.get_db().execute("SELECT * FROM articles WHERE id=1").fetchone()
        original_title = article["title"]
        version = article["updated_at"]
    token = get_token_for(client, "/admin/edit/1")
    response = client.post(
        "/admin/edit/1",
        data={
            "csrf_token": token, "_version": version, "title": "수정 이력 검증 기사", "category": "임상시험",
            "deck": "수정 이력과 복원 기능을 확인하기 위한 요약입니다.", "body": "수정 이력 검증을 위한 본문입니다.", "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    history = client.get("/admin/articles/1/history")
    assert history.status_code == 200
    assert original_title in history.get_data(as_text=True)
    with app.app.app_context():
        version_id = app.get_db().execute("SELECT id FROM article_versions WHERE article_id=1 ORDER BY id DESC LIMIT 1").fetchone()[0]
    token = get_token_for(client, "/admin/articles/1/history")
    restored = client.post(
        f"/admin/articles/1/history/{version_id}/restore", data={"csrf_token": token}, follow_redirects=False
    )
    assert restored.status_code == 302
    with app.app.app_context():
        title = app.get_db().execute("SELECT title FROM articles WHERE id=1").fetchone()[0]
        assert title == original_title


def test_editor_can_publish_article_with_documents_and_checklist(app_and_client):
    app, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    response = client.post(
        "/admin/write",
        data={
            "csrf_token": token, "title": "근거 문서 검증 기사", "category": "정책·규제",
            "deck": "독자가 원문을 확인할 수 있는 출처 문서와 발행 체크리스트 검증입니다.",
            "body": "공개 문서 링크와 체크리스트가 함께 저장되어야 합니다.", "action": "publish",
            "document_label": ["식약처 공지"], "document_url": ["https://example.com/source"],
            "source_verified": "1", "conflicts_reviewed": "1", "seo_reviewed": "1", "rights_confirmed": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    with app.app.app_context():
        db = app.get_db()
        article_id = db.execute("SELECT id FROM articles WHERE title='근거 문서 검증 기사'").fetchone()[0]
        checklist = db.execute("SELECT * FROM article_checklists WHERE article_id=?", (article_id,)).fetchone()
        assert checklist["source_verified"] == 1
        assert db.execute("SELECT COUNT(*) FROM article_documents WHERE article_id=?", (article_id,)).fetchone()[0] == 1
    public = client.get(f"/article/{article_id}")
    assert "출처·근거 문서" in public.get_data(as_text=True)
    assert "식약처 공지" in public.get_data(as_text=True)

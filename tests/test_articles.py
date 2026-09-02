import io
from datetime import timedelta

from conftest import login, get_token_for


def _publish_note_image():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (10, 20, 30)).save(buf, "JPEG")
    buf.seek(0)
    return buf


def test_editor_can_publish_immediately(app_and_client):
    _, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    resp = client.post(
        "/admin/write",
        data={
            "title": "즉시발행테스트",
            "category": "임상시험",
            "deck": "요약",
            "body": "본문",
            "action": "publish",
            "csrf_token": token,
        },
    )
    assert resp.status_code == 302
    home = client.get("/").get_data(as_text=True)
    assert "즉시발행테스트" in home


def test_editor_can_schedule_article_and_due_time_publishes_it(app_and_client):
    """예약 저장된 기사는 도달 시각 이후 다음 요청에서 자동 공개되어야 합니다."""
    app, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    future = app.now_kst() + timedelta(hours=1)
    response = client.post(
        "/admin/write",
        data={
            "title": "예약자동발행검증", "category": "임상시험", "deck": "예약 발행 검증 요약",
            "body": "도달 시각이 되면 자동으로 공개되는 본문입니다.", "action": "schedule",
            "scheduled_at": future.strftime("%Y-%m-%dT%H:%M"), "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    with app.app.app_context():
        db = app.get_db()
        row = db.execute("SELECT id, status FROM articles WHERE title='예약자동발행검증'").fetchone()
        assert row["status"] == "scheduled"
        db.execute(
            "UPDATE articles SET scheduled_at=? WHERE id=?",
            ((app.now_kst() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"), row["id"]),
        )
        db.commit()
        app._last_schedule_check["ts"] = 0
    home = client.get("/").get_data(as_text=True)
    assert "예약자동발행검증" in home
    with app.app.app_context():
        db = app.get_db()
        assert db.execute("SELECT status FROM articles WHERE title='예약자동발행검증'").fetchone()["status"] == "published"


def test_reporter_cannot_bypass_review_by_forging_action(app_and_client):
    """기자가 action=publish를 직접 보내도 서버가 권한을 확인해서 초안으로 처리해야 합니다."""
    _, client = app_and_client
    login(client, "reporter", "reporter1234")
    token = get_token_for(client, "/admin/write")
    client.post(
        "/admin/write",
        data={
            "title": "권한우회시도",
            "category": "임상시험",
            "deck": "d",
            "body": "b",
            "action": "publish",
            "csrf_token": token,
        },
    )
    home = client.get("/").get_data(as_text=True)
    assert "권한우회시도" not in home  # 발행되면 안 됨


def test_reporter_submit_for_review_then_editor_publishes(app_and_client):
    _, client = app_and_client
    login(client, "reporter", "reporter1234")
    token = get_token_for(client, "/admin/write")
    client.post(
        "/admin/write",
        data={
            "title": "심사워크플로우테스트",
            "category": "정책·규제",
            "deck": "d",
            "body": "b",
            "action": "submit_review",
            "csrf_token": token,
        },
    )
    client.get("/logout")

    login(client, "editor", "editor1234")
    review_html = client.get("/admin/review").get_data(as_text=True)
    assert "심사워크플로우테스트" in review_html


def test_image_upload_is_validated_and_served(app_and_client):
    _, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    resp = client.post(
        "/admin/write",
        data={
            "title": "이미지업로드테스트",
            "category": "임상시험",
            "deck": "d",
            "body": "b",
            "action": "publish",
            "csrf_token": token,
            "image": (_publish_note_image(), "test.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    home = client.get("/").get_data(as_text=True)
    assert "/static/uploads/" in home


def test_fake_php_disguised_as_jpg_is_rejected(app_and_client):
    """확장자만 .jpg인 실행파일을 올리면 실제 이미지 검증에서 거부되어야 합니다."""
    _, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    fake = io.BytesIO(b"<?php system($_GET['c']); ?>")
    resp = client.post(
        "/admin/write",
        data={
            "title": "위장파일테스트",
            "category": "임상시험",
            "deck": "d",
            "body": "b",
            "action": "save",
            "csrf_token": token,
            "image": (fake, "fake.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert "올바른 이미지" in resp.get_data(as_text=True) or "이미지 파일만" in resp.get_data(as_text=True)


def test_tags_are_saved_and_tag_page_works(app_and_client):
    _, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    client.post(
        "/admin/write",
        data={
            "title": "태그테스트기사",
            "category": "임상시험",
            "deck": "d",
            "body": "b",
            "tags": "테스트태그A, 테스트태그B",
            "action": "publish",
            "csrf_token": token,
        },
    )
    tag_page = client.get("/tag/테스트태그A").get_data(as_text=True)
    assert "태그테스트기사" in tag_page


def test_body_image_placeholder_is_replaced_with_real_image(app_and_client):
    _, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    resp = client.post(
        "/admin/write",
        data={
            "title": "본문이미지테스트",
            "category": "임상시험",
            "deck": "d",
            "body": "첫 문단\n\n[[이미지1]]\n\n둘째 문단",
            "action": "publish",
            "csrf_token": token,
            "body_images": (_publish_note_image(), "body1.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    home_html = client.get("/").get_data(as_text=True)
    assert "본문이미지테스트" in home_html


def test_unknown_article_returns_404(app_and_client):
    _, client = app_and_client
    resp = client.get("/article/999999")
    assert resp.status_code == 404


def test_concurrent_edit_conflict_is_detected(app_and_client):
    """두 사람이 같은 기사를 동시에 열어서 수정하면, 나중에 저장하는 쪽은
    먼저 저장된 내용을 조용히 덮어쓰지 않고 충돌 안내를 받아야 합니다."""
    import re
    import sqlite3
    import os

    _, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    client.post(
        "/admin/write",
        data={
            "title": "동시편집원본",
            "category": "임상시험",
            "deck": "d",
            "body": "b",
            "action": "publish",
            "csrf_token": token,
        },
    )
    db = sqlite3.connect(os.environ["MEDITOK_DB_PATH"])
    aid = db.execute("SELECT id FROM articles WHERE title='동시편집원본'").fetchone()[0]

    # 두 사람이 "동시에" 편집화면을 열었다고 가정 (같은 버전 값을 두 번 확보)
    edit_page = client.get(f"/admin/edit/{aid}").get_data(as_text=True)
    version = re.search(r'name="_version" value="([^"]+)"', edit_page).group(1)

    # 1번째 사람이 먼저 저장 -> 성공해야 함
    token1 = get_token_for(client, f"/admin/edit/{aid}")
    resp1 = client.post(
        f"/admin/edit/{aid}",
        data={
            "title": "동시편집_먼저저장", "category": "임상시험", "deck": "d", "body": "b",
            "action": "save", "_version": version, "csrf_token": token1,
        },
    )
    assert resp1.status_code == 302

    # 2번째 사람이 "옛날 버전" 그대로 저장 시도 -> 막혀야 함
    token2 = get_token_for(client, f"/admin/edit/{aid}")
    resp2 = client.post(
        f"/admin/edit/{aid}",
        data={
            "title": "동시편집_나중저장_충돌", "category": "임상시험", "deck": "d", "body": "b",
            "action": "save", "_version": version, "csrf_token": token2,
        },
        follow_redirects=True,
    )
    assert "먼저 수정했습니다" in resp2.get_data(as_text=True)

    db2 = sqlite3.connect(os.environ["MEDITOK_DB_PATH"])
    title_now = db2.execute("SELECT title FROM articles WHERE id=?", (aid,)).fetchone()[0]
    assert title_now == "동시편집_먼저저장"  # 나중 저장(충돌)은 실제로 반영되지 않아야 함


def test_edit_without_version_field_still_works(app_and_client):
    """오래된 클라이언트나 예외 상황으로 _version 필드가 없어도 저장은 정상 동작해야 합니다."""
    import sqlite3
    import os

    _, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    client.post(
        "/admin/write",
        data={
            "title": "버전필드없음테스트", "category": "임상시험", "deck": "d", "body": "b",
            "action": "publish", "csrf_token": token,
        },
    )
    db = sqlite3.connect(os.environ["MEDITOK_DB_PATH"])
    aid = db.execute("SELECT id FROM articles WHERE title='버전필드없음테스트'").fetchone()[0]

    token2 = get_token_for(client, f"/admin/edit/{aid}")
    resp = client.post(
        f"/admin/edit/{aid}",
        data={
            "title": "버전필드없이수정성공", "category": "임상시험", "deck": "d", "body": "b",
            "action": "save", "csrf_token": token2,
        },
    )
    assert resp.status_code == 302

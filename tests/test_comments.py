from conftest import login, get_token_for


def _publish_sample_article(client):
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    client.post(
        "/admin/write",
        data={
            "title": "댓글테스트기사",
            "category": "임상시험",
            "deck": "d",
            "body": "b",
            "action": "publish",
            "csrf_token": token,
        },
    )
    client.get("/logout")
    import sqlite3
    import os

    db_path = os.environ["MEDITOK_DB_PATH"]
    db = sqlite3.connect(db_path)
    return db.execute("SELECT id FROM articles WHERE title='댓글테스트기사'").fetchone()[0]


def _signup_and_login_reader(client, username="commentuser"):
    token = get_token_for(client, "/signup")
    client.post(
        "/signup",
        data={"name": "댓글유저", "username": username, "password": "12345678", "csrf_token": token},
    )
    login(client, username, "12345678")


def test_comment_requires_4_digit_pin(app_and_client):
    _, client = app_and_client
    aid = _publish_sample_article(client)
    _signup_and_login_reader(client)

    token = get_token_for(client, f"/article/{aid}")
    resp = client.post(
        f"/article/{aid}/comment",
        data={"body": "테스트댓글", "delete_pin": "12", "csrf_token": token},
        follow_redirects=True,
    )
    assert "숫자 4자리" in resp.get_data(as_text=True)

    art = client.get(f"/article/{aid}").get_data(as_text=True)
    assert "테스트댓글" not in art  # 형식이 틀린 PIN이면 저장되면 안 됨


def test_comment_with_valid_pin_is_saved(app_and_client):
    _, client = app_and_client
    aid = _publish_sample_article(client)
    _signup_and_login_reader(client)

    token = get_token_for(client, f"/article/{aid}")
    client.post(
        f"/article/{aid}/comment",
        data={"body": "정상댓글입니다", "delete_pin": "1234", "csrf_token": token},
    )
    art = client.get(f"/article/{aid}").get_data(as_text=True)
    assert "정상댓글입니다" in art


def test_wrong_pin_cannot_delete_comment(app_and_client):
    _, client = app_and_client
    aid = _publish_sample_article(client)
    _signup_and_login_reader(client)

    token = get_token_for(client, f"/article/{aid}")
    client.post(
        f"/article/{aid}/comment",
        data={"body": "삭제테스트댓글", "delete_pin": "1234", "csrf_token": token},
    )
    import sqlite3
    import os

    db = sqlite3.connect(os.environ["MEDITOK_DB_PATH"])
    cid = db.execute("SELECT id FROM comments WHERE body='삭제테스트댓글'").fetchone()[0]

    token2 = get_token_for(client, f"/article/{aid}")
    resp = client.post(
        f"/comment/{cid}/delete",
        data={"delete_pin": "9999", "csrf_token": token2},
        follow_redirects=True,
    )
    assert "일치하지 않습니다" in resp.get_data(as_text=True)
    art = client.get(f"/article/{aid}").get_data(as_text=True)
    assert "삭제테스트댓글" in art  # 여전히 남아있어야 함


def test_correct_pin_deletes_comment(app_and_client):
    _, client = app_and_client
    aid = _publish_sample_article(client)
    _signup_and_login_reader(client)

    token = get_token_for(client, f"/article/{aid}")
    client.post(
        f"/article/{aid}/comment",
        data={"body": "정상삭제댓글", "delete_pin": "5678", "csrf_token": token},
    )
    import sqlite3
    import os

    db = sqlite3.connect(os.environ["MEDITOK_DB_PATH"])
    cid = db.execute("SELECT id FROM comments WHERE body='정상삭제댓글'").fetchone()[0]

    token2 = get_token_for(client, f"/article/{aid}")
    client.post(f"/comment/{cid}/delete", data={"delete_pin": "5678", "csrf_token": token2})
    art = client.get(f"/article/{aid}").get_data(as_text=True)
    assert "정상삭제댓글" not in art

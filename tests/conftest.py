"""
pytest 공용 픽스처.

테스트를 실행할 때마다 완전히 새로운 임시 SQLite DB와 업로드 폴더를 만들어서,
실제 운영 DB(meditok.db)나 서로 다른 테스트끼리 절대 영향을 주고받지 않게 합니다.
"""
import os
import re
import shutil
import tempfile

import pytest


@pytest.fixture()
def app_and_client():
    """테스트 하나당 독립된 앱 인스턴스를 만듭니다 (DB·업로드 폴더 격리)."""
    tmp_dir = tempfile.mkdtemp(prefix="meditok_test_")
    db_path = os.path.join(tmp_dir, "test.db")
    upload_dir = os.path.join(tmp_dir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    os.environ["MEDITOK_DB_PATH"] = db_path
    os.environ["MEDITOK_UPLOAD_DIR"] = upload_dir
    os.environ.pop("DATABASE_URL", None)  # 테스트는 항상 SQLite로 (PostgreSQL 테스트는 별도 마커로 분리)
    os.environ.pop("REDIS_URL", None)

    import importlib
    import app as app_module
    importlib.reload(app_module)  # 매 테스트마다 새 DB_PATH를 반영해서 완전히 새로 초기화

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()

    yield app_module, client

    shutil.rmtree(tmp_dir, ignore_errors=True)


def get_csrf_token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "CSRF 토큰을 찾을 수 없습니다 (페이지 구조가 바뀌었을 수 있습니다)"
    return m.group(1)


def login(client, username, password):
    resp = client.get("/login")
    token = get_csrf_token(resp.get_data(as_text=True))
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=False,
    )


def get_token_for(client, url):
    resp = client.get(url)
    return get_csrf_token(resp.get_data(as_text=True))

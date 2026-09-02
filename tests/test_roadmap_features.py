from conftest import get_csrf_token, get_token_for, login


def test_transparency_corrections_and_trust_card(app_and_client):
    app, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/write")
    created = client.post(
        "/admin/write",
        data={
            "csrf_token": token, "title": "투명성 검증 기사", "category": "정책·규제",
            "deck": "취재 방식과 불확실성을 독자에게 보여주는 기사입니다.",
            "body": "검증을 위한 기사 본문입니다.", "action": "publish",
            "report_type": "analysis", "reporting_method": "공개 고시와 논문, 복수 취재원을 검토했습니다.",
            "uncertainty_note": "후속 데이터에 따라 해석이 달라질 수 있습니다.",
            "conflict_note": "이해상충 없음", "source_verified": "1",
        },
        follow_redirects=False,
    )
    assert created.status_code == 302
    with app.app.app_context():
        db = app.get_db()
        article = db.execute("SELECT id,updated_at FROM articles WHERE title='투명성 검증 기사'").fetchone()
        assert db.execute("SELECT report_type FROM article_transparency WHERE article_id=?", (article["id"],)).fetchone()[0] == "analysis"
    page = client.get(f"/article/{article['id']}")
    assert "취재·검증 정보" in page.get_data(as_text=True)
    assert "공개 고시와 논문" in page.get_data(as_text=True)
    trust = client.get(f"/article/{article['id']}/trust-card.svg")
    assert trust.status_code == 200
    assert trust.mimetype == "image/svg+xml"
    assert "MEDITALK" in trust.get_data(as_text=True)

    token = get_token_for(client, f"/admin/edit/{article['id']}")
    edited = client.post(
        f"/admin/edit/{article['id']}",
        data={
            "csrf_token": token, "_version": article["updated_at"], "title": "투명성 검증 기사 수정",
            "category": "정책·규제", "deck": "취재 방식과 불확실성을 독자에게 보여주는 기사입니다.",
            "body": "정정 후 기사 본문입니다.", "action": "save", "report_type": "analysis",
            "reporting_method": "공개 고시와 논문, 복수 취재원을 검토했습니다.",
            "publish_correction": "1", "correction_summary": "본문의 기준일을 정정했습니다.",
            "correction_detail": "최신 공개 자료를 확인해 기준일을 수정했습니다.",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 302
    public = client.get(f"/article/{article['id']}")
    assert "정정 이력" in public.get_data(as_text=True)
    assert "본문의 기준일을 정정했습니다" in public.get_data(as_text=True)


def test_reporter_profile_industry_alerts_and_editorial_question(app_and_client):
    app, client = app_and_client
    login(client, "reporter", "reporter1234")
    token = get_token_for(client, "/admin/profile")
    profile = client.post(
        "/admin/profile",
        data={
            "csrf_token": token, "expertise": "신약 허가·임상개발", "bio": "제약·바이오 산업을 취재합니다.",
            "contact_email": "reporter@example.com", "verification_note": "복수 취재원을 교차 확인합니다.",
        },
        follow_redirects=False,
    )
    assert profile.status_code == 302
    page = client.get("/reporter/reporter")
    assert "신약 허가·임상개발" in page.get_data(as_text=True)

    client.get("/logout")
    signup = client.get("/signup")
    token = get_csrf_token(signup.get_data(as_text=True))
    client.post(
        "/signup", data={"csrf_token": token, "name": "알림 독자", "username": "watch_reader", "password": "password123"},
        follow_redirects=False,
    )
    login(client, "watch_reader", "password123")
    token = get_token_for(client, "/mypage")
    followed = client.post(
        "/industry/toggle", data={"csrf_token": token, "topic_type": "company", "topic_value": "한서바이오"},
        follow_redirects=True,
    )
    assert "한서바이오 알림을 추가했습니다" in followed.get_data(as_text=True)
    assert "INDUSTRY WATCH" in followed.get_data(as_text=True)

    token = get_token_for(client, "/article/1")
    question = client.post(
        "/questions", data={"csrf_token": token, "article_id": "1", "name": "알림 독자", "question": "근거 자료의 기준일은 언제인가요?"},
        follow_redirects=False,
    )
    assert question.status_code == 302
    with app.app.app_context():
        question_id = app.get_db().execute("SELECT id FROM editorial_questions ORDER BY id DESC LIMIT 1").fetchone()[0]
    client.get("/logout")
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/questions")
    answer = client.post(
        f"/admin/questions/{question_id}/answer",
        data={"csrf_token": token, "status": "answered", "answer": "공개 자료의 발표일을 기준으로 검토했습니다."},
        follow_redirects=False,
    )
    assert answer.status_code == 302
    article_page = client.get("/article/1")
    assert "편집국 답변" in article_page.get_data(as_text=True)
    assert "공개 자료의 발표일" in article_page.get_data(as_text=True)


def test_team_subscription_seats_and_team_feed(app_and_client):
    app, client = app_and_client
    signup = client.get("/signup")
    reader_token = get_csrf_token(signup.get_data(as_text=True))
    client.post(
        "/signup", data={"csrf_token": reader_token, "name": "팀 독자", "username": "team_reader", "password": "password123"},
        follow_redirects=False,
    )
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/teams")
    made = client.post(
        "/teams", data={"csrf_token": token, "name": "바이오 전략팀", "domain": "meditalk.co.kr", "seat_limit": "3"},
        follow_redirects=False,
    )
    assert made.status_code == 302
    with app.app.app_context():
        org_id = app.get_db().execute("SELECT id FROM team_organizations WHERE name='바이오 전략팀'").fetchone()[0]
    team_url = f"/teams/{org_id}"
    token = get_token_for(client, team_url)
    added = client.post(team_url, data={"csrf_token": token, "action": "add_member", "username": "team_reader"}, follow_redirects=True)
    assert "팀에 추가했습니다" in added.get_data(as_text=True)
    token = get_token_for(client, team_url)
    watched = client.post(
        team_url, data={"csrf_token": token, "action": "toggle_follow", "topic_type": "compound", "topic_value": "GLP-1"},
        follow_redirects=True,
    )
    assert "팀 관심 키워드를 추가했습니다" in watched.get_data(as_text=True)
    assert "이번 주 공용 관심 기사" in watched.get_data(as_text=True)
    client.get("/logout")
    login(client, "team_reader", "password123")
    member_view = client.get(team_url)
    assert member_view.status_code == 200
    assert "바이오 전략팀" in member_view.get_data(as_text=True)

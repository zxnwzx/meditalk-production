import io

from conftest import get_token_for, login


ONE_PIXEL_GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"


def _animated_gif():
    from PIL import Image

    first = Image.new("RGB", (2, 2), (220, 39, 63))
    second = Image.new("RGB", (2, 2), (7, 83, 185))
    payload = io.BytesIO()
    first.save(payload, "GIF", save_all=True, append_images=[second], duration=120, loop=0)
    payload.seek(0)
    return payload


def test_editor_can_create_multislot_campaign_and_public_pages_render_it(app_and_client):
    app, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/ads")
    response = client.post(
        "/admin/ads",
        data={
            "csrf_token": token,
            "name": "홈·기사 동시 캠페인",
            "sponsor": "검증 광고주",
            "target_url": "https://example.com/campaign",
            "slot_keys": ["home_1", "article_bottom_1"],
            "image": (io.BytesIO(ONE_PIXEL_GIF), "ad.gif"),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "캠페인을 저장했습니다" in response.get_data(as_text=True)

    public = client.get("/")
    html = public.get_data(as_text=True)
    assert "홈·기사 동시 캠페인" in html
    assert "/ad/click/1/home_1" in html
    assert 'rel="sponsored noopener"' in html
    assert ".gif" in html
    with app.app.app_context():
        db = app.get_db()
        filename = db.execute("SELECT image_filename FROM ad_campaigns WHERE id=1").fetchone()[0]
        assert filename.endswith(".gif")
        with open(app.UPLOAD_DIR + "/" + filename, "rb") as gif_file:
            assert gif_file.read(6) == b"GIF89a"
    pixel = client.get("/ad/impression/1/home_1.gif")
    assert pixel.status_code == 200
    click = client.get("/ad/click/1/home_1", follow_redirects=False)
    assert click.status_code == 302
    assert click.headers["Location"] == "https://example.com/campaign"
    with app.app.app_context():
        db = app.get_db()
        assert db.execute("SELECT COUNT(*) FROM ad_events WHERE event_type='impression'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM ad_events WHERE event_type='click'").fetchone()[0] == 1


def test_animated_gif_campaign_preserves_animation_frames(app_and_client):
    from PIL import Image

    app, client = app_and_client
    login(client, "editor", "editor1234")
    token = get_token_for(client, "/admin/ads")
    response = client.post(
        "/admin/ads",
        data={
            "csrf_token": token, "name": "움직이는 GIF 캠페인", "sponsor": "검증 광고주",
            "target_url": "https://example.com/animated", "slot_keys": ["home_2"],
            "image": (_animated_gif(), "animated-ad.gif"),
        },
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert "캠페인을 저장했습니다" in response.get_data(as_text=True)
    with app.app.app_context():
        db = app.get_db()
        filename = db.execute("SELECT image_filename FROM ad_campaigns WHERE name='움직이는 GIF 캠페인'").fetchone()[0]
        assert filename.endswith(".gif")
        with Image.open(app.UPLOAD_DIR + "/" + filename) as image:
            assert image.format == "GIF"
            assert image.n_frames == 2
    public = client.get("/").get_data(as_text=True)
    assert filename in public


def test_reporter_cannot_manage_ads(app_and_client):
    _, client = app_and_client
    login(client, "reporter", "reporter1234")
    response = client.get("/admin/ads")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin")

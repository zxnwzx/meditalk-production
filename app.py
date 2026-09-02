import sqlite3
import os
import re
import time
import secrets
import hmac
import logging
import base64
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    flash, g, abort, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from markupsafe import Markup, escape

# ---------------------------------------------------------------- 공유 상태 저장소 (Redis 우선, 없으면 메모리)
# 워커(worker)를 2개 이상 띄우면 각 프로세스가 메모리를 공유하지 않아서,
# 로그인 시도 제한·홈 캐시·조회수 집계가 워커별로 따로 놀 수 있습니다.
# REDIS_URL 환경변수가 설정되어 있으면 Redis를 공유 저장소로 써서 이 문제를 없애고,
# 설정 안 되어 있으면 기존처럼 프로세스 메모리를 씁니다 (워커 1개일 때는 이걸로 충분합니다).
_redis_client = None
_redis_url = os.environ.get("REDIS_URL")
if _redis_url:
    try:
        import redis as _redis_module
        _redis_client = _redis_module.from_url(_redis_url, decode_responses=True, socket_connect_timeout=2)
        _redis_client.ping()
    except Exception as _e:
        _redis_client = None
        logging.getLogger("meditok.security").warning(
            "REDIS_URL이 설정됐지만 연결에 실패해 메모리 저장소로 대체합니다: %s", _e
        )

# ---------------------------------------------------------------- 이미지 저장소 (S3/R2 우선, 없으면 로컬 디스크)
# Render 무료 플랜 등은 재배포할 때마다 디스크가 초기화되어 업로드한 이미지가 사라집니다.
# S3_BUCKET 환경변수가 있으면 S3 호환 저장소(AWS S3, Cloudflare R2 등)에 이미지를 올리고,
# 없으면 기존처럼 로컬 static/uploads 폴더를 씁니다.
_s3_client = None
_s3_public_base = None
S3_BUCKET = os.environ.get("S3_BUCKET")
if S3_BUCKET:
    try:
        import boto3
        _s3_client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,  # Cloudflare R2 등 S3 호환 서비스용
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("S3_REGION") or "auto",
        )
        _s3_public_base = os.environ.get("S3_PUBLIC_URL_BASE")
        if not _s3_public_base:
            logging.getLogger("meditok.security").warning(
                "S3_BUCKET은 설정됐지만 S3_PUBLIC_URL_BASE가 없어 이미지 URL을 만들 수 없습니다."
            )
            _s3_client = None
    except Exception as _e:
        _s3_client = None
        logging.getLogger("meditok.security").warning(
            "S3_BUCKET이 설정됐지만 초기화에 실패해 로컬 디스크로 대체합니다: %s", _e
        )

# 서버가 어느 시간대(UTC 등)에서 돌든, 이 사이트의 모든 시각은 항상 한국 표준시(KST, UTC+9)
# 기준으로 계산·저장합니다. (한국은 서머타임이 없어 고정 오프셋으로 충분히 정확합니다.)
KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(KST)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MEDITOK_DB_PATH") or os.path.join(BASE_DIR, "meditok.db")
UPLOAD_DIR = os.environ.get("MEDITOK_UPLOAD_DIR") or os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6MB
MAX_IMAGE_DIM = 1600  # 긴 변 기준 리사이즈
MAX_GIF_FRAMES = 180  # 애니메이션 GIF의 과도한 디코딩 방지

# 보안 관련 이벤트(로그인 실패, CSRF 차단, 레이트리밋 등)를 따로 눈에 띄게 로깅합니다.
security_logger = logging.getLogger("meditok.security")
security_logger.setLevel(logging.INFO)
if not security_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("SECURITY: %(asctime)s %(message)s"))
    security_logger.addHandler(_handler)


def _client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


# ---------------------------------------------------------------- DB 엔진 추상화 (SQLite ↔ PostgreSQL)
# DATABASE_URL 환경변수가 있으면 PostgreSQL을, 없으면 기존처럼 SQLite 파일을 씁니다.
# 기존의 137곳에 달하는 db.execute("...?...", (...)) 호출부는 하나도 안 건드리고,
# 아래 얇은 호환 레이어가 "?" 자리표시자·lastrowid·INSERT OR IGNORE 등의 차이를 흡수합니다.
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)

# 이 테이블들은 복합 기본키(article_id, tag_id 등)만 있고 별도 id 컬럼이 없어서,
# INSERT 문에 자동으로 "RETURNING id"를 붙이면 안 됩니다.
_PG_TABLES_WITHOUT_ID = {"article_tags", "article_related"}

try:
    import psycopg2
    import psycopg2.extras
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
except ImportError:
    psycopg2 = None
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

if USE_POSTGRES and psycopg2 is None:
    raise RuntimeError(
        "DATABASE_URL이 설정되어 있지만 psycopg2가 설치되어 있지 않습니다. "
        "requirements.txt에 psycopg2-binary를 추가해 주세요."
    )


def _pg_table_from_insert(sql):
    m = re.match(r"\s*INSERT(?:\s+OR\s+IGNORE)?\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.I)
    return m.group(1) if m else None


def _pg_translate_sql(sql):
    """SQLite 문법(?placeholder, INSERT OR IGNORE)을 PostgreSQL 문법으로 변환합니다."""
    table = _pg_table_from_insert(sql)
    pg_sql = sql
    if re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO", pg_sql, re.I):
        pg_sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", pg_sql, flags=re.I)
        pg_sql = pg_sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # "?" 자리표시자를 순서대로 "%s"로 치환 (우리 쿼리 문자열 리터럴 안에는 "?"가 들어가지 않습니다)
    pg_sql = pg_sql.replace("?", "%s")
    add_returning = (
        table is not None
        and table not in _PG_TABLES_WITHOUT_ID
        and re.match(r"\s*INSERT\s+INTO", pg_sql, re.I)
        and "RETURNING" not in pg_sql.upper()
        and "ON CONFLICT" not in pg_sql.upper()
    )
    if add_returning:
        pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"
    return pg_sql


class _PGResult:
    """psycopg2 커서를 감싸서 sqlite3의 .execute() 반환값(fetchone/fetchall/lastrowid)과 동일하게 동작합니다."""

    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor.fetchall())


class PGConnection:
    """psycopg2 연결을 감싸서 sqlite3.Connection과 최대한 동일한 인터페이스를 제공하는 얇은 호환 레이어."""

    def __init__(self, dsn):
        self._conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.DictCursor)

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        pg_sql = _pg_translate_sql(sql)
        cur.execute(pg_sql, tuple(params))
        lastrowid = None
        if "RETURNING ID" in pg_sql.upper():
            row = cur.fetchone()
            lastrowid = row["id"] if row else None
        return _PGResult(cur, lastrowid)

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        pg_sql = _pg_translate_sql(sql).replace(" RETURNING id", "")  # executemany는 반환값을 안 씁니다
        cur.executemany(pg_sql, [tuple(p) for p in seq_of_params])
        return _PGResult(cur)

    def executescript(self, sql):
        cur = self._conn.cursor()
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                cur.execute(statement)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ---------------------------------------------------------------- 보안 설정
_DEFAULT_SECRET = "meditok-dev-secret-change-in-production"
app.secret_key = os.environ.get("SECRET_KEY", _DEFAULT_SECRET)

app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 이미지 업로드 6MB + 폼 필드 여유분
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24

DEBUG_MODE = os.environ.get("FLASK_DEBUG", "0") == "1"
SCHEDULE_WORKER_ENABLED = os.environ.get("SCHEDULE_WORKER_ENABLED", "0") == "1"

if app.config["SESSION_COOKIE_SECURE"] and app.secret_key == _DEFAULT_SECRET:
    security_logger.warning(
        "⚠️  운영 모드로 보이는데 SECRET_KEY가 기본값입니다! Render 환경변수에 무작위 값을 설정하세요."
    )


# ---------------------------------------------------------------- CSRF 보호 (외부 의존성 없는 경량 구현)
def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


def csp_nonce():
    if "_csp_nonce" not in g:
        g._csp_nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    return g._csp_nonce


app.jinja_env.globals["csp_nonce"] = csp_nonce

CSRF_EXEMPT_ENDPOINTS = set()

# ---------------------------------------------------------------- 요청 빈도 제한 (로그인/댓글/신고 등 공용)
_rate_buckets = {}


def _rate_limited(bucket, max_attempts, window_sec, ip=None):
    ip = ip or _client_ip()
    now = time.time()
    if _redis_client is not None:
        # Redis 정렬셋(sorted set)에 타임스탬프를 점수로 저장해서, 워커가 몇 개든 항상 같은 카운트를 봅니다.
        key = f"rl:{bucket}:{ip}"
        try:
            pipe = _redis_client.pipeline()
            pipe.zremrangebyscore(key, 0, now - window_sec)
            pipe.zcard(key)
            _, count = pipe.execute()
            if count >= max_attempts:
                security_logger.info("레이트리밋 초과(Redis) — bucket=%s ip=%s", bucket, ip)
                return True
            return False
        except Exception:
            pass  # Redis 장애 시에도 사이트가 멈추지 않도록 메모리 방식으로 조용히 대체
    key = (bucket, ip)
    attempts = [t for t in _rate_buckets.get(key, []) if now - t < window_sec]
    _rate_buckets[key] = attempts
    if len(attempts) >= max_attempts:
        security_logger.info("레이트리밋 초과 — bucket=%s ip=%s", bucket, ip)
        return True
    return False


def _record_rate_hit(bucket, ip=None):
    ip = ip or _client_ip()
    now = time.time()
    if _redis_client is not None:
        key = f"rl:{bucket}:{ip}"
        try:
            pipe = _redis_client.pipeline()
            pipe.zadd(key, {str(now): now})
            pipe.expire(key, 3600)
            pipe.execute()
            return
        except Exception:
            pass
    _rate_buckets.setdefault((bucket, ip), []).append(now)


LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SEC = 300
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-password-for-timing-safety")


@app.before_request
def check_csrf():
    csp_nonce()
    # 컨테이너 운영 시에는 scheduler.py 단일 프로세스가 예약 발행을 전담합니다.
    # 로컬 단일 프로세스 실행에서는 기존처럼 요청 시에도 예약 발행을 확인합니다.
    if not SCHEDULE_WORKER_ENABLED:
        _publish_due_scheduled_articles()
    _flush_view_counts()
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
            return
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(sent, expected):
            security_logger.info("CSRF 차단 — ip=%s path=%s", _client_ip(), request.path)
            abort(400, description="보안 토큰이 유효하지 않습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.")


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    # 관리자 화면과 로그인 상태에 따라 달라지는 페이지는 브라우저가 캐시하면 안 됩니다.
    # (기사를 발행했는데 뒤로가기로 옛 목록이 보이거나, 로그아웃 후 캐시된 관리자 화면이 남는 것을 방지)
    if request.path.startswith("/admin") or request.path in ("/mypage", "/login", "/signup"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    nonce = g.get("_csp_nonce", "")
    csp = (
        "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}' https://www.googletagmanager.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net data:; "
        "img-src 'self' data:; "
        "connect-src 'self' https://www.google-analytics.com https://www.googletagmanager.com"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


# ---------------------------------------------------------------- 예약 발행
_last_schedule_check = {"ts": 0.0}
SCHEDULE_CHECK_INTERVAL_SEC = 5   # 예약 발행 확인 주기 — 짧을수록 예약 시각에 정확히 맞춰 발행됩니다


_pending_views = {}
_last_view_flush = {"ts": 0.0}
VIEW_FLUSH_INTERVAL_SEC = 20


def _bump_view_count(article_id):
    _pending_views[article_id] = _pending_views.get(article_id, 0) + 1


def _flush_view_counts():
    """모아둔 조회수를 한 번에 DB에 반영합니다 (기사 조회마다 쓰지 않아 부하가 크게 줄어듭니다)."""
    now = time.time()
    if now - _last_view_flush["ts"] < VIEW_FLUSH_INTERVAL_SEC or not _pending_views:
        return
    _last_view_flush["ts"] = now
    pending = dict(_pending_views)
    _pending_views.clear()
    try:
        db = get_db()
        db.executemany(
            "UPDATE articles SET view_count = view_count + ? WHERE id=?",
            [(cnt, aid) for aid, cnt in pending.items()],
        )
        db.commit()
    except Exception:
        # 반영 실패 시 카운트를 되돌려 다음 기회에 다시 시도
        for aid, cnt in pending.items():
            _pending_views[aid] = _pending_views.get(aid, 0) + cnt


def _publish_due_scheduled_articles():
    now = time.time()
    if now - _last_schedule_check["ts"] < SCHEDULE_CHECK_INTERVAL_SEC:
        return
    _last_schedule_check["ts"] = now
    if not os.path.exists(DB_PATH):
        return
    try:
        db = get_db()
    except Exception:
        return
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    due = db.execute(
        "SELECT id FROM articles WHERE status='scheduled' AND scheduled_at IS NOT NULL AND scheduled_at<=?",
        (now_str,),
    ).fetchall()
    if due:
        db.execute(
            "UPDATE articles SET status='published', published_at=?, updated_at=? "
            "WHERE status='scheduled' AND scheduled_at IS NOT NULL AND scheduled_at<=?",
            (now_str, now_str, now_str),
        )
        db.commit()
        _invalidate_home_cache()
        for row in due:
            security_logger.info("예약 발행 자동 실행 — article_id=%s", row["id"])


class ImageUploadError(Exception):
    pass


def _save_uploaded_image(file_storage):
    """업로드된 기사 이미지를 검증·정제해서 저장합니다.
    확장자 화이트리스트, 용량 제한, PIL 실제 이미지 검증, 메타데이터 제거, 무작위 파일명.
    S3_BUCKET 환경변수가 설정되어 있으면 S3(R2 포함) 호환 저장소에, 없으면 로컬 디스크에 저장합니다."""
    if not file_storage or not file_storage.filename:
        return None, None, None

    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMAGE_EXT:
        raise ImageUploadError("이미지 파일만 업로드할 수 있습니다 (jpg, png, webp, gif).")

    data = file_storage.read(MAX_IMAGE_BYTES + 1)
    if len(data) == 0:
        return None, None, None
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageUploadError("이미지 용량은 6MB를 넘을 수 없습니다.")

    import io
    from PIL import Image, ImageOps

    try:
        probe = Image.open(io.BytesIO(data))
        image_format = (probe.format or "").upper()
        probe.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ImageUploadError("올바른 이미지 파일이 아닙니다.")

    allowed_formats = {
        "jpg": {"JPEG"}, "jpeg": {"JPEG"}, "png": {"PNG"},
        "webp": {"WEBP"}, "gif": {"GIF"},
    }
    if image_format not in allowed_formats[ext]:
        raise ImageUploadError("파일 확장자와 실제 이미지 형식이 일치하지 않습니다.")

    if image_format == "GIF":
        frame_count = getattr(img, "n_frames", 1)
        if frame_count > MAX_GIF_FRAMES:
            raise ImageUploadError(f"GIF 프레임 수는 {MAX_GIF_FRAMES}개를 넘을 수 없습니다.")
        if max(img.width, img.height) > MAX_IMAGE_DIM:
            raise ImageUploadError(f"GIF 긴 변은 {MAX_IMAGE_DIM}px를 넘을 수 없습니다.")
        out_name = f"{secrets.token_hex(16)}.gif"
        if _s3_client is not None:
            _s3_client.put_object(
                Bucket=S3_BUCKET, Key=out_name, Body=data,
                ContentType="image/gif", CacheControl="public, max-age=31536000, immutable",
            )
        else:
            with open(os.path.join(UPLOAD_DIR, out_name), "wb") as f:
                f.write(data)
        return out_name, img.width, img.height

    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB",):
        img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))

    out_name = f"{secrets.token_hex(16)}.jpg"
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=True)
    jpeg_bytes = buf.getvalue()

    if _s3_client is not None:
        _s3_client.put_object(
            Bucket=S3_BUCKET, Key=out_name, Body=jpeg_bytes,
            ContentType="image/jpeg", CacheControl="public, max-age=31536000, immutable",
        )
    else:
        with open(os.path.join(UPLOAD_DIR, out_name), "wb") as f:
            f.write(jpeg_bytes)
    return out_name, img.width, img.height


def _delete_uploaded_image(filename):
    if not filename:
        return
    if _s3_client is not None:
        try:
            _s3_client.delete_object(Bucket=S3_BUCKET, Key=filename)
        except Exception:
            pass
        return
    path = os.path.join(UPLOAD_DIR, filename)
    try:
        if os.path.commonpath([os.path.abspath(path), UPLOAD_DIR]) == UPLOAD_DIR and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def image_url(filename):
    """업로드된 이미지의 실제 접근 URL을 만듭니다. S3/R2 설정 시 그쪽 URL을, 아니면 로컬 /static/uploads/ 경로를 씁니다.
    OG 태그 등에서 절대경로가 필요하므로 항상 절대 URL로 반환합니다."""
    if not filename:
        return ""
    if _s3_public_base:
        return f"{_s3_public_base.rstrip('/')}/{filename}"
    return url_for("static", filename="uploads/" + filename, _external=True)


app.jinja_env.globals["image_url"] = image_url


MAX_BODY_IMAGES = 8


def _save_body_images(file_storages):
    """본문 삽입용 이미지 여러 장을 검증·저장합니다. 반환: [(filename, width, height), ...]"""
    saved = []
    for f in (file_storages or [])[:MAX_BODY_IMAGES]:
        if not f or not f.filename:
            continue
        try:
            filename, w, h = _save_uploaded_image(f)
        except ImageUploadError:
            continue  # 본문 이미지 중 하나가 잘못됐다고 전체 저장을 막지는 않음
        if filename:
            saved.append((filename, w, h))
    return saved


def _replace_article_images(db, article_id, file_storages, captions):
    """기존 본문 이미지를 전부 지우고 새로 저장합니다 (수정 시에도 순서·번호가 예측 가능하도록)."""
    old_rows = db.execute("SELECT filename FROM article_images WHERE article_id=?", (article_id,)).fetchall()
    db.execute("DELETE FROM article_images WHERE article_id=?", (article_id,))
    for row in old_rows:
        _delete_uploaded_image(row["filename"])
    saved = _save_body_images(file_storages)
    for i, (filename, w, h) in enumerate(saved, start=1):
        caption = (captions[i - 1] if i - 1 < len(captions) else "").strip()[:200]
        db.execute(
            "INSERT INTO article_images (article_id, filename, width, height, caption, position) VALUES (?,?,?,?,?,?)",
            (article_id, filename, w, h, caption or None, i),
        )
    return len(saved)


def _keep_article_images(db, article_id, file_storages, captions):
    """새로 첨부된 파일이 있으면만 교체하고, 없으면 기존 본문 이미지를 그대로 둡니다."""
    has_new = any(f and f.filename for f in (file_storages or []))
    if has_new:
        return _replace_article_images(db, article_id, file_storages, captions)
    return None


def _save_related_articles(db, article_id, related_ids):
    """편집장이 직접 고른 관련기사(최대 3개)를 저장합니다. 자기 자신·미발행 기사는 제외합니다."""
    db.execute("DELETE FROM article_related WHERE article_id=?", (article_id,))
    seen = []
    for rid in related_ids[:3]:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if rid == article_id or rid in seen:
            continue
        row = db.execute("SELECT id FROM articles WHERE id=? AND status='published'", (rid,)).fetchone()
        if not row:
            continue
        seen.append(rid)
    for i, rid in enumerate(seen, start=1):
        db.execute(
            "INSERT INTO article_related (article_id, related_article_id, position) VALUES (?,?,?)",
            (article_id, rid, i),
        )


BODY_IMAGE_TOKEN = re.compile(r"\[\[이미지(\d+)\]\]")


def render_article_body(article_id, body_text):
    """본문 텍스트의 [[이미지1]], [[이미지2]] 같은 표시를 실제 이미지로 바꿔서 안전한 HTML을 만듭니다.
    사용자가 입력한 일반 텍스트는 전부 이스케이프하고, 우리가 만든 <figure> 태그만 신뢰합니다."""
    from markupsafe import escape

    db = get_db()
    images = {
        row["position"]: row
        for row in db.execute(
            "SELECT * FROM article_images WHERE article_id=? ORDER BY position", (article_id,)
        ).fetchall()
    }
    if not images:
        return Markup(str(escape(body_text)))

    parts = []
    last_end = 0
    for m in BODY_IMAGE_TOKEN.finditer(body_text):
        parts.append(str(escape(body_text[last_end:m.start()])))
        n = int(m.group(1))
        img = images.get(n)
        if img:
            cap = f"<figcaption>{escape(img['caption'])}</figcaption>" if img["caption"] else ""
            parts.append(
                f'<figure class="article-figure">'
                f'<img src="{image_url(img["filename"])}" '
                f'alt="{escape(img["caption"] or "본문 이미지")}" loading="lazy">{cap}</figure>'
            )
        else:
            parts.append(str(escape(m.group(0))))  # 없는 번호면 원문 그대로 표시
        last_end = m.end()
    parts.append(str(escape(body_text[last_end:])))
    return Markup("".join(parts))


app.jinja_env.globals["render_article_body"] = render_article_body

SITE_NAME = "메디톡"
SITE_TAGLINE = "제약·바이오 업계의 목소리를 전합니다"

CATEGORIES = ["임상시험", "신약·허가", "정책·규제", "기업·M&A", "디지털헬스", "오피니언"]

STATUS_LABELS = {
    "draft": "초안",
    "pending": "심사 대기",
    "published": "발행됨",
    "rejected": "반려됨",
    "scheduled": "예약 발행",
}
ROLE_LABELS = {"reader": "독자", "journalist": "기자", "editor": "편집장"}
PAGE_SIZE = 15

AD_SLOT_CATALOG = (
    {
        "key": "home_1", "name": "홈페이지 1", "group": "홈 상단",
        "placement": "메인 헤드라인 아래", "recommended_size": "1200 × 180px",
    },
    {
        "key": "home_2", "name": "홈페이지 2", "group": "홈 우측",
        "placement": "메인 주요 뉴스 아래", "recommended_size": "360 × 250px",
    },
    {
        "key": "home_3", "name": "홈페이지 3", "group": "홈 중단",
        "placement": "PICK·인기기사 아래", "recommended_size": "1200 × 180px",
    },
    {
        "key": "home_4", "name": "홈페이지 4", "group": "홈 하단",
        "placement": "세 번째 섹션 기사 아래", "recommended_size": "1200 × 180px",
    },
    {
        "key": "article_left_1", "name": "기사 왼쪽 1", "group": "기사 좌측",
        "placement": "기사 제목 영역 좌측", "recommended_size": "300 × 250px",
    },
    {
        "key": "article_left_2", "name": "기사 왼쪽 2", "group": "기사 좌측",
        "placement": "기사 본문 중간 좌측", "recommended_size": "300 × 250px",
    },
    {
        "key": "article_right_1", "name": "기사 오른쪽 1", "group": "기사 우측",
        "placement": "기사 제목 영역 우측", "recommended_size": "300 × 250px",
    },
    {
        "key": "article_right_2", "name": "기사 오른쪽 2", "group": "기사 우측",
        "placement": "관련 기사 영역 우측", "recommended_size": "300 × 250px",
    },
    {
        "key": "article_bottom_1", "name": "기사 하단 1", "group": "기사 하단",
        "placement": "본문·태그 아래", "recommended_size": "880 × 200px",
    },
    {
        "key": "article_bottom_2", "name": "기사 하단 2", "group": "기사 하단",
        "placement": "관련 기사·댓글 아래", "recommended_size": "880 × 200px",
    },
)


def _escape_like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

# 데모용 고정 콘텐츠 — 실제 운영 시에는 관리자 사이트에서 편집 가능한 DB 테이블로 확장 가능
UPCOMING_EVENTS = [
    {"date": "08.21", "title": "2026 제약바이오 디지털헬스 포럼", "place": "코엑스 컨퍼런스룸 C"},
    {"date": "09.03", "title": "임상시험 데이터 표준화 세미나", "place": "온라인 웨비나"},
    {"date": "09.17", "title": "메디톡 어워즈 — 올해의 신약", "place": "서울 롯데호텔"},
]
JOB_LISTINGS = [
    {"company": "한서바이오", "title": "임상개발팀 대리~과장", "tag": "경력 3~7년"},
    {"company": "청안제약", "title": "허가·인허가 전문가", "tag": "경력무관"},
    {"company": "대현제약", "title": "R&D 기획 신입/경력", "tag": "신입가능"},
]


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            g.db = PGConnection(DATABASE_URL)
        else:
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            # WAL 모드: 읽기와 쓰기가 서로를 막지 않아, 방문자가 늘어도 페이지가 밀리지 않습니다.
            g.db.execute("PRAGMA journal_mode = WAL")
            g.db.execute("PRAGMA synchronous = NORMAL")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


DEFAULT_TICKER_SPEED_SEC = 29
DEFAULT_TICKER_CATEGORY = ""  # 빈 문자열 = 전체 섹션


def get_setting(key, default=""):
    row = get_db().execute("SELECT value FROM site_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    row = db.execute("SELECT key FROM site_settings WHERE key=?", (key,)).fetchone()
    if row:
        db.execute("UPDATE site_settings SET value=? WHERE key=?", (value, key))
    else:
        db.execute("INSERT INTO site_settings (key, value) VALUES (?,?)", (key, value))
    db.commit()


def _slot_definition(slot_key):
    return next((slot for slot in AD_SLOT_CATALOG if slot["key"] == slot_key), None)


def _load_active_ad_slots():
    """활성 기간 안의 캠페인만 슬롯별로 반환합니다. 한 슬롯에는 하나의 캠페인만 노출됩니다."""
    today = now_kst().strftime("%Y-%m-%d")
    rows = get_db().execute(
        "SELECT c.*, p.slot_key FROM ad_campaigns c JOIN ad_placements p ON p.campaign_id=c.id "
        "WHERE c.is_active=1 AND (c.starts_at IS NULL OR c.starts_at<=?) "
        "AND (c.ends_at IS NULL OR c.ends_at>=?) ORDER BY c.updated_at DESC",
        (today, today),
    ).fetchall()
    slots = {}
    for row in rows:
        item = dict(row)
        definition = _slot_definition(item["slot_key"])
        if definition:
            slots[item["slot_key"]] = {**definition, **item}
    return slots


def _record_article_version(db, article, actor, change_note=""):
    """덮어쓰기 전에 원본을 남겨 발행 기사를 안전하게 복원할 수 있게 합니다."""
    db.execute(
        "INSERT INTO article_versions "
        "(article_id,title,category,deck,body,status,image_filename,image_caption,scheduled_at,changed_by,change_note,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            article["id"], article["title"], article["category"], article["deck"], article["body"],
            article["status"], article["image_filename"], article["image_caption"], article["scheduled_at"],
            actor["id"], change_note[:300], now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


def _save_article_documents(db, article_id, labels, urls):
    """외부 근거 문서는 URL 형식만 허용하고 기사별 최대 5개로 제한합니다."""
    documents = []
    for label, url in zip(labels or [], urls or []):
        label = (label or "").strip()[:100]
        url = (url or "").strip()[:500]
        if not label and not url:
            continue
        if not label or not _valid_external_url(url):
            raise ValueError("근거 문서는 이름과 http:// 또는 https:// 주소를 함께 입력해 주세요.")
        documents.append((label, url))
        if len(documents) == 5:
            break
    db.execute("DELETE FROM article_documents WHERE article_id=?", (article_id,))
    db.executemany(
        "INSERT INTO article_documents (article_id,label,url,created_at) VALUES (?,?,?,?)",
        [(article_id, label, url, now_kst().strftime("%Y-%m-%d %H:%M:%S")) for label, url in documents],
    )


def _save_article_checklist(db, article_id, actor_id, form):
    values = tuple(1 if form.get(name) == "1" else 0 for name in (
        "source_verified", "conflicts_reviewed", "seo_reviewed", "rights_confirmed"
    ))
    now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    existing = db.execute("SELECT article_id FROM article_checklists WHERE article_id=?", (article_id,)).fetchone()
    if existing:
        db.execute(
            "UPDATE article_checklists SET source_verified=?,conflicts_reviewed=?,seo_reviewed=?,rights_confirmed=?,updated_by=?,updated_at=? WHERE article_id=?",
            (*values, actor_id, now, article_id),
        )
    else:
        db.execute(
            "INSERT INTO article_checklists (article_id,source_verified,conflicts_reviewed,seo_reviewed,rights_confirmed,updated_by,updated_at) VALUES (?,?,?,?,?,?,?)",
            (article_id, *values, actor_id, now),
        )


def _valid_external_url(value):
    return not value or bool(re.match(r"^https?://", value, flags=re.IGNORECASE))


INDUSTRY_FOLLOW_TYPES = ("company", "compound", "trial")
REPORT_TYPES = ("news", "analysis", "opinion")


def _save_article_transparency(db, article_id, actor_id, form):
    """취재 방식·불확실성·이해상충을 기사 단위로 저장합니다."""
    report_type = form.get("report_type", "news")
    if report_type not in REPORT_TYPES:
        report_type = "news"
    values = (
        report_type,
        (form.get("reporting_method") or "").strip()[:800],
        (form.get("uncertainty_note") or "").strip()[:800],
        (form.get("conflict_note") or "").strip()[:500],
        actor_id,
        now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        article_id,
    )
    existing = db.execute("SELECT article_id FROM article_transparency WHERE article_id=?", (article_id,)).fetchone()
    if existing:
        db.execute(
            "UPDATE article_transparency SET report_type=?,reporting_method=?,uncertainty_note=?,conflict_note=?,updated_by=?,updated_at=? WHERE article_id=?",
            values,
        )
    else:
        db.execute(
            "INSERT INTO article_transparency (article_id,report_type,reporting_method,uncertainty_note,conflict_note,updated_by,updated_at) VALUES (?,?,?,?,?,?,?)",
            (article_id, *values[:-1]),
        )


def _record_public_correction(db, article_id, actor_id, form):
    """편집장이 명시적으로 공개를 선택한 유의미한 수정만 정정 이력으로 남깁니다."""
    if form.get("publish_correction") != "1":
        return
    summary = (form.get("correction_summary") or "").strip()[:300]
    detail = (form.get("correction_detail") or "").strip()[:1000]
    if not summary:
        raise ValueError("공개 정정에는 독자가 이해할 수 있는 수정 요약을 입력해 주세요.")
    db.execute(
        "INSERT INTO article_corrections (article_id,summary,detail,corrected_by,created_at) VALUES (?,?,?,?,?)",
        (article_id, summary, detail, actor_id, now_kst().strftime("%Y-%m-%d %H:%M:%S")),
    )


def _team_for_user(db, organization_id, user_id):
    return db.execute(
        "SELECT o.*, m.role AS membership_role FROM team_organizations o JOIN team_members m ON m.organization_id=o.id "
        "WHERE o.id=? AND m.user_id=?",
        (organization_id, user_id),
    ).fetchone()


def _industry_feed(db, follows, limit=8):
    """회사·성분·임상 키워드가 언급된 최근 공개 기사를 관심 피드로 반환합니다."""
    values = [row["topic_value"] for row in follows]
    if not values:
        return []
    clauses, params = [], []
    for value in values[:20]:
        like = f"%{_escape_like(value)}%"
        clauses.append("(a.title LIKE ? ESCAPE '\\' OR a.deck LIKE ? ESCAPE '\\' OR a.body LIKE ? ESCAPE '\\')")
        params.extend([like, like, like])
    return db.execute(
        "SELECT DISTINCT a.*, u.name AS author_name FROM articles a JOIN users u ON u.id=a.author_id "
        "WHERE a.status='published' AND a.published_at>=? AND (" + " OR ".join(clauses) + ") "
        "ORDER BY a.published_at DESC, a.id DESC LIMIT ?",
        [(now_kst() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M"), *params, limit],
    ).fetchall()


def _init_product_tables(db, postgres=False):
    pk = "SERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS ad_campaigns (
            id {pk}, name TEXT NOT NULL, sponsor TEXT, target_url TEXT,
            image_filename TEXT, starts_at TEXT, ends_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ad_placements (
            id {pk}, campaign_id INTEGER NOT NULL, slot_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES ad_campaigns(id)
        );
        CREATE TABLE IF NOT EXISTS ad_events (
            id {pk}, campaign_id INTEGER NOT NULL, slot_key TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('impression','click')), created_at TEXT NOT NULL,
            FOREIGN KEY(campaign_id) REFERENCES ad_campaigns(id)
        );
        CREATE TABLE IF NOT EXISTS article_versions (
            id {pk}, article_id INTEGER NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL,
            deck TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, image_filename TEXT,
            image_caption TEXT, scheduled_at TEXT, changed_by INTEGER NOT NULL, change_note TEXT,
            created_at TEXT NOT NULL, FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(changed_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS topic_follows (
            id {pk}, user_id INTEGER NOT NULL, topic_type TEXT NOT NULL CHECK(topic_type IN ('category','tag')),
            topic_value TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id, topic_type, topic_value),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS newsletter_preferences (
            id {pk}, email TEXT NOT NULL, category TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(email, category)
        );
        CREATE TABLE IF NOT EXISTS article_documents (
            id {pk}, article_id INTEGER NOT NULL, label TEXT NOT NULL, url TEXT NOT NULL,
            created_at TEXT NOT NULL, FOREIGN KEY(article_id) REFERENCES articles(id)
        );
        CREATE TABLE IF NOT EXISTS article_checklists (
            article_id INTEGER PRIMARY KEY, source_verified INTEGER NOT NULL DEFAULT 0,
            conflicts_reviewed INTEGER NOT NULL DEFAULT 0, seo_reviewed INTEGER NOT NULL DEFAULT 0,
            rights_confirmed INTEGER NOT NULL DEFAULT 0, updated_by INTEGER, updated_at TEXT NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id), FOREIGN KEY(updated_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS article_transparency (
            article_id INTEGER PRIMARY KEY, report_type TEXT NOT NULL DEFAULT 'news', reporting_method TEXT,
            uncertainty_note TEXT, conflict_note TEXT, updated_by INTEGER, updated_at TEXT NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id), FOREIGN KEY(updated_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS article_corrections (
            id {pk}, article_id INTEGER NOT NULL, summary TEXT NOT NULL, detail TEXT,
            corrected_by INTEGER NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id), FOREIGN KEY(corrected_by) REFERENCES users(id)
        );
CREATE TABLE IF NOT EXISTS reporter_profiles (
            user_id {pk}, expertise TEXT, bio TEXT, contact_email TEXT,
            tip_url TEXT, verification_note TEXT, avatar_filename TEXT, updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS industry_follows (
            id {pk}, user_id INTEGER NOT NULL, topic_type TEXT NOT NULL CHECK(topic_type IN ('company','compound','trial')),
            topic_value TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id, topic_type, topic_value),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS editorial_questions (
            id {pk}, article_id INTEGER, asker_name TEXT NOT NULL, asker_email TEXT,
            question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', answer TEXT, answered_by INTEGER,
            asked_at TEXT NOT NULL, answered_at TEXT, FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(answered_by) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS team_organizations (
            id {pk}, name TEXT NOT NULL, domain TEXT, owner_id INTEGER NOT NULL, seat_limit INTEGER NOT NULL DEFAULT 5,
            created_at TEXT NOT NULL, FOREIGN KEY(owner_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS team_members (
            id {pk}, organization_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'member',
            joined_at TEXT NOT NULL, UNIQUE(organization_id, user_id), FOREIGN KEY(organization_id) REFERENCES team_organizations(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS team_follows (
            id {pk}, organization_id INTEGER NOT NULL, topic_type TEXT NOT NULL CHECK(topic_type IN ('company','compound','trial')),
            topic_value TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(organization_id, topic_type, topic_value),
            FOREIGN KEY(organization_id) REFERENCES team_organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ad_events_campaign ON ad_events(campaign_id, event_type, created_at);
        CREATE INDEX IF NOT EXISTS idx_article_versions_article ON article_versions(article_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_topic_follows_user ON topic_follows(user_id, topic_type);
        CREATE INDEX IF NOT EXISTS idx_newsletter_preferences_category ON newsletter_preferences(category, email);
        CREATE INDEX IF NOT EXISTS idx_article_documents_article ON article_documents(article_id);
        CREATE INDEX IF NOT EXISTS idx_article_corrections_article ON article_corrections(article_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_industry_follows_user ON industry_follows(user_id, topic_type);
        CREATE INDEX IF NOT EXISTS idx_editorial_questions_status ON editorial_questions(status, asked_at DESC);
        CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id, organization_id);
        """
    )
    # 기자 프로필 사진은 기존 DB에도 무중단으로 추가합니다.
    try:
        profile_cols = [row[1] for row in db.execute("SELECT * FROM reporter_profiles LIMIT 0").description]
        if "avatar_filename" not in profile_cols:
            alter_sql = "ALTER TABLE reporter_profiles ADD COLUMN avatar_filename TEXT"
            db.execute(alter_sql)
            db.commit()
    except Exception:
        db.rollback()


def log_activity(actor_name, action, detail=""):
    """관리자 활동을 감사(audit) 목적으로 기록합니다 — 누가 언제 무엇을 했는지 나중에 확인할 수 있도록."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO activity_log (actor_name, action, detail, created_at) VALUES (?,?,?,?)",
            (actor_name, action, detail, now_kst().strftime("%Y-%m-%d %H:%M:%S")),
        )
        db.commit()
    except Exception:
        pass  # 로그 기록 실패가 실제 기능을 막으면 안 되므로 조용히 무시


def init_db():
    if USE_POSTGRES:
        _init_db_postgres()
        return
    fresh = not os.path.exists(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('reader','journalist','editor')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            deck TEXT NOT NULL,
            body TEXT NOT NULL,
            author_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft','pending','published','rejected','scheduled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT,
            view_count INTEGER NOT NULL DEFAULT 0,
            is_pick INTEGER NOT NULL DEFAULT 0,
            review_note TEXT,
            image_filename TEXT,
            image_width INTEGER,
            image_height INTEGER,
            image_caption TEXT,
            scheduled_at TEXT,
            FOREIGN KEY(author_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, article_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delete_pin_hash TEXT,
            FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS comment_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER NOT NULL,
            reporter_user_id INTEGER NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(comment_id) REFERENCES comments(id),
            FOREIGN KEY(reporter_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS article_tags (
            article_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (article_id, tag_id),
            FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(tag_id) REFERENCES tags(id)
        );

        CREATE TABLE IF NOT EXISTS article_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            caption TEXT,
            position INTEGER NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS article_related (
            article_id INTEGER NOT NULL,
            related_article_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (article_id, related_article_id),
            FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(related_article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS typo_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            reporter_email TEXT,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS newsletter_subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            company TEXT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ticker_articles (
            article_id INTEGER PRIMARY KEY,
            position INTEGER NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_date TEXT NOT NULL,
            location TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            position TEXT NOT NULL,
            experience_level TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_name TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    db.commit()

    # lightweight migration for DBs created before these columns existed
    existing_cols = [r[1] for r in db.execute("PRAGMA table_info(articles)").fetchall()]
    if "review_note" not in existing_cols:
        db.execute("ALTER TABLE articles ADD COLUMN review_note TEXT")
    if "view_count" not in existing_cols:
        db.execute("ALTER TABLE articles ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0")
    if "is_pick" not in existing_cols:
        db.execute("ALTER TABLE articles ADD COLUMN is_pick INTEGER NOT NULL DEFAULT 0")
    if "image_filename" not in existing_cols:
        db.execute("ALTER TABLE articles ADD COLUMN image_filename TEXT")
        db.execute("ALTER TABLE articles ADD COLUMN image_width INTEGER")
        db.execute("ALTER TABLE articles ADD COLUMN image_height INTEGER")
        db.execute("ALTER TABLE articles ADD COLUMN image_caption TEXT")
    if "scheduled_at" not in existing_cols:
        db.execute("ALTER TABLE articles ADD COLUMN scheduled_at TEXT")
    comment_cols = [r[1] for r in db.execute("PRAGMA table_info(comments)").fetchall()]
    if comment_cols and "delete_pin_hash" not in comment_cols:
        db.execute("ALTER TABLE comments ADD COLUMN delete_pin_hash TEXT")
    db.commit()

    # ------------------------------------------------------------------ 인덱스
    # 기사·댓글이 수천 건 이상 쌓여도 목록/검색/정렬이 느려지지 않도록 인덱스를 겁니다.
    # (인덱스가 없으면 SQLite가 매번 전체 테이블을 훑기 때문에, 데이터가 늘수록 급격히 느려집니다.)
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_status_pub
            ON articles(status, published_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_cat_status_pub
            ON articles(category, status, published_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_author
            ON articles(author_id, status);
        CREATE INDEX IF NOT EXISTS idx_articles_scheduled
            ON articles(status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_articles_views
            ON articles(status, view_count DESC);
        CREATE INDEX IF NOT EXISTS idx_comments_article
            ON comments(article_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_bookmarks_user
            ON bookmarks(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_article_tags_tag
            ON article_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_typo_reports_status
            ON typo_reports(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_article_images_article
            ON article_images(article_id, position);
        CREATE INDEX IF NOT EXISTS idx_article_related_article
            ON article_related(article_id, position);
        """
    )
    db.commit()
    _init_product_tables(db)

    if fresh:
        _seed_initial_data(db)
    db.close()


def _init_db_postgres():
    """PostgreSQL용 스키마 생성. SQLite 버전과 테이블 구조는 동일하고,
    AUTOINCREMENT 대신 SERIAL을, PRAGMA 대신 PostgreSQL 기본 동작(외래키는 항상 강제됨)을 씁니다."""
    db = PGConnection(DATABASE_URL)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('reader','journalist','editor')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS articles (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            deck TEXT NOT NULL,
            body TEXT NOT NULL,
            author_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft','pending','published','rejected','scheduled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT,
            view_count INTEGER NOT NULL DEFAULT 0,
            is_pick INTEGER NOT NULL DEFAULT 0,
            review_note TEXT,
            image_filename TEXT,
            image_width INTEGER,
            image_height INTEGER,
            image_caption TEXT,
            scheduled_at TEXT,
            FOREIGN KEY(author_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bookmarks (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, article_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY,
            article_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delete_pin_hash TEXT,
            FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS comment_reports (
            id SERIAL PRIMARY KEY,
            comment_id INTEGER NOT NULL,
            reporter_user_id INTEGER NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(comment_id) REFERENCES comments(id),
            FOREIGN KEY(reporter_user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS tags (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS article_tags (
            article_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (article_id, tag_id),
            FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(tag_id) REFERENCES tags(id)
        );

        CREATE TABLE IF NOT EXISTS article_images (
            id SERIAL PRIMARY KEY,
            article_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            caption TEXT,
            position INTEGER NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS article_related (
            article_id INTEGER NOT NULL,
            related_article_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (article_id, related_article_id),
            FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(related_article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS typo_reports (
            id SERIAL PRIMARY KEY,
            article_id INTEGER NOT NULL,
            reporter_email TEXT,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS newsletter_subscribers (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inquiries (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            company TEXT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ticker_articles (
            article_id INTEGER PRIMARY KEY,
            position INTEGER NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            event_date TEXT NOT NULL,
            location TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_listings (
            id SERIAL PRIMARY KEY,
            company TEXT NOT NULL,
            position TEXT NOT NULL,
            experience_level TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            actor_name TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_articles_status_pub
            ON articles(status, published_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_cat_status_pub
            ON articles(category, status, published_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_author
            ON articles(author_id, status);
        CREATE INDEX IF NOT EXISTS idx_articles_scheduled
            ON articles(status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_articles_views
            ON articles(status, view_count DESC);
        CREATE INDEX IF NOT EXISTS idx_comments_article
            ON comments(article_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_bookmarks_user
            ON bookmarks(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_article_tags_tag
            ON article_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_typo_reports_status
            ON typo_reports(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_article_images_article
            ON article_images(article_id, position);
        CREATE INDEX IF NOT EXISTS idx_article_related_article
            ON article_related(article_id, position);
        """
    )
    db.commit()
    _init_product_tables(db, postgres=True)

    fresh = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
    if fresh:
        _seed_initial_data(db)
    db.close()


def _seed_initial_data(db):
    """최초 설치 시(테이블이 비어있을 때) 데모 계정 2개와 기사 7건을 넣습니다. SQLite/PostgreSQL 공용."""
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    editor_pw = generate_password_hash("editor1234")
    reporter_pw = generate_password_hash("reporter1234")
    db.execute(
        "INSERT INTO users (username,password_hash,name,role,created_at) VALUES (?,?,?,?,?)",
        ("editor", editor_pw, "김편집", "editor", now),
    )
    db.execute(
        "INSERT INTO users (username,password_hash,name,role,created_at) VALUES (?,?,?,?,?)",
        ("reporter", reporter_pw, "정수아", "journalist", now),
    )
    db.commit()
    reporter_id = db.execute(
        "SELECT id FROM users WHERE username='reporter'"
    ).fetchone()[0]
    editor_id = db.execute(
        "SELECT id FROM users WHERE username='editor'"
    ).fetchone()[0]

    seed_articles = [
        (
            "한서바이오, 3세대 표적항암제 국내 3상 조건부 승인 신청",
            "임상시험",
            "식품의약품안전처가 조건부 허가 대상 항목을 확대한 가운데, 한서바이오의 EGFR 이중표적 항암 후보물질이 2상 유효성 데이터를 근거로 조건부 승인 트랙에 진입했다.",
            "식품의약품안전처가 조건부 허가 대상 항목을 확대한 가운데, 한서바이오의 EGFR 이중표적 항암 후보물질이 2상 유효성 데이터를 근거로 조건부 승인 트랙에 진입했다.\n\n업계는 이번 신청이 국내 바이오텍의 신속승인 활용 사례를 늘리는 신호탄이 될 것으로 본다. 식약처 관계자는 심사 기간을 기존 대비 단축할 방침이라고 밝혔다.\n\n한서바이오 관계자는 하반기 중 데이터 보완 자료를 추가로 제출할 계획이라고 전했다.",
            reporter_id,
        ),
        (
            "식약처, AI 활용 의료기기 허가 가이드라인 개정안 행정예고",
            "정책·규제",
            "인공지능 기반 진단보조 소프트웨어의 시판 후 성능 변경 관리 기준이 새로 마련된다.",
            "식품의약품안전처는 인공지능 기반 진단보조 소프트웨어의 시판 후 성능 변경 관리 기준을 담은 가이드라인 개정안을 행정예고했다.\n\n개정안은 학습 데이터 갱신에 따른 모델 성능 변화를 정기적으로 보고하도록 하는 내용을 골자로 한다.",
            reporter_id,
        ),
        (
            "청안제약, 마이크로바이옴 전문기업 지분 62% 인수",
            "기업·M&A",
            "청안제약이 장내미생물 기반 치료제 개발사의 경영권을 인수하며 파이프라인 다각화에 나섰다.",
            "청안제약이 마이크로바이옴 치료제 개발기업의 지분 62%를 인수하며 경영권을 확보했다고 밝혔다.\n\n이번 인수로 청안제약은 대사질환 및 자가면역질환 분야 파이프라인을 확대하게 됐다.",
            editor_id,
        ),
        (
            "원격모니터링 수가 시범사업, 2차 참여기관 공모 시작",
            "디지털헬스",
            "만성질환자 대상 원격모니터링 서비스의 건강보험 수가 적용 범위를 넓히는 2차 시범사업이 시작된다.",
            "보건복지부는 만성질환자 대상 원격모니터링 서비스의 건강보험 수가 시범사업 2차 참여기관 공모를 시작한다고 밝혔다.\n\n1차 시범사업에서는 참여 의료기관의 재입원율이 감소한 것으로 나타났다.",
            reporter_id,
        ),
        (
            "국내 IND 승인 건수, 상반기 전년 대비 18% 증가",
            "임상시험",
            "세포·유전자치료제 분야 신청이 증가세를 이끌었으며, 희귀질환 적응증 비중도 확대됐다.",
            "식약처 임상시험계획(IND) 승인 통계에 따르면 올해 상반기 승인 건수는 전년 동기 대비 18% 증가했다.\n\n세포·유전자치료제 분야의 신청 증가가 전체 증가세를 이끌었으며, 희귀질환 적응증을 대상으로 한 신청 비중도 함께 늘었다.",
            editor_id,
        ),
        (
            "[기고] 임상시험 데이터 표준화, 더 이상 미룰 수 없다",
            "오피니언",
            "다기관 임상 데이터의 형식이 기관마다 달라 발생하는 비효율을 짚고, 표준화 로드맵을 제안하는 기고문.",
            "다기관 임상시험에서 수집되는 데이터의 형식이 기관마다 상이해 통계 분석 단계에서 상당한 시간이 소요되는 것이 현실이다.\n\n표준 데이터 모델 도입과 함께 중앙 데이터관리기관의 역할을 확대할 필요가 있다.",
            editor_id,
        ),
        (
            "10년 적자 딛고 흑자전환, 대현제약의 체질 개선 스토리",
            "기업·M&A",
            "제네릭 중심 사업구조에서 특허 자산 중심으로 전환한 대현제약의 최근 5년을 짚어봤다. 이번 주 '메디톡'가 주목한 기업이다.",
            "대현제약은 2020년까지만 해도 제네릭 매출 의존도가 90%에 달하는 전형적인 중견 제약사였다.\n\n하지만 지속형 GLP-1 제형 특허를 확보하면서 사업 구조가 빠르게 바뀌기 시작했다. 연구개발 투자 비중을 매출 대비 3%에서 11%로 늘린 것이 전환점이었다.\n\n업계는 이런 체질 개선이 중견 제약사들에게 하나의 참고 모델이 될 수 있다고 평가한다. '메디톡'는 탁월한 약과 탁월한 기업을 함께 조명한다는 취지로, 이번 주 대현제약의 사례를 첫 PICK으로 선정했다.",
            editor_id,
        ),
    ]
    pick_index = len(seed_articles) - 1  # 마지막 기사를 이주의 PICK으로 지정
    view_seeds = [612, 488, 1204, 356, 290, 174, 940]
    for idx, (title, cat, deck, body, author_id) in enumerate(seed_articles):
        vc = view_seeds[idx] if idx < len(view_seeds) else 120
        is_pick = 1 if idx == pick_index else 0
        db.execute(
            """INSERT INTO articles
            (title,category,deck,body,author_id,status,created_at,updated_at,published_at,view_count,is_pick)
            VALUES (?,?,?,?,?, 'published', ?, ?, ?, ?, ?)""",
            (title, cat, deck, body, author_id, now, now, now, vc, is_pick),
        )

    for ev_data in UPCOMING_EVENTS:
        db.execute(
            "INSERT INTO events (title, event_date, location, created_at) VALUES (?,?,?,?)",
            (ev_data["title"], ev_data["date"], ev_data["place"], now),
        )
    for job in JOB_LISTINGS:
        db.execute(
            "INSERT INTO job_listings (company, position, experience_level, created_at) VALUES (?,?,?,?)",
            (job["company"], job["title"], job["tag"], now),
        )
    db.commit()


# ---------------------------------------------------------------- helpers
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("로그인이 필요합니다.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            flash("로그인이 필요합니다.", "error")
            return redirect(url_for("login", next=request.path))
        if user["role"] not in ("journalist", "editor"):
            flash("기자·편집국 계정만 접근할 수 있습니다.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def editor_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            flash("로그인이 필요합니다.", "error")
            return redirect(url_for("login", next=request.path))
        if user["role"] != "editor":
            flash("편집장 권한이 필요합니다.", "error")
            return redirect(url_for("admin_dashboard"))
        return view(*args, **kwargs)
    return wrapped


PALETTE = [
    ("#1B3A6B", "path"), ("#1A1A1A", "ring"), ("#8A8A8A", "dash"),
    ("#C8102E", "rect"),
]


def _mini_svg(seed):
    color, kind = PALETTE[seed % len(PALETTE)]
    if kind == "path":
        inner = f'<path d="M20 150 L80 90 L140 120 L200 60 L280 40" stroke="{color}" stroke-width="2" fill="none"/>'
    elif kind == "ring":
        inner = f'<circle cx="150" cy="95" r="45" fill="none" stroke="{color}" stroke-width="2"/><circle cx="150" cy="95" r="20" fill="none" stroke="#C8102E" stroke-width="2"/>'
    elif kind == "dash":
        inner = f'<path d="M40 40 L260 40 L260 150 L40 150 Z" fill="none" stroke="{color}" stroke-width="1.5" stroke-dasharray="6 4"/>'
    else:
        inner = f'<rect x="60" y="50" width="180" height="90" fill="none" stroke="{color}" stroke-width="2"/>'
    svg = f'<svg viewBox="0 0 300 190" width="100%" height="100%"><rect width="300" height="190" fill="{color}" opacity="0.1"/>{inner}</svg>'
    return Markup(svg)


def _hero_svg():
    # 약초의 잎맥(자연·전통)과 분자 구조(과학·근거)를 한 선으로 잇는 시그니처 일러스트.
    # 판화(engraving)에 가까운 단색 세밀 선화 — 브랜드 네이비 배경에 라이트블루 선화, 레드 포인트.
    svg = """<svg viewBox="0 0 600 420" width="100%" height="100%" style="position:absolute;inset:0;" preserveAspectRatio="xMidYMid slice">
      <rect width="600" height="420" fill="#1A1A1A"/>
      <g stroke="#C4C4C4" stroke-width="1" fill="none" opacity="0.6">
        <path d="M300 55 C 258 118, 258 182, 300 235 C 342 182, 342 118, 300 55 Z"/>
        <path d="M300 88 C 279 130, 279 172, 300 208"/>
        <path d="M300 108 L248 98 M300 128 L242 134 M300 148 L248 170 M300 168 L253 200"/>
        <path d="M300 108 L352 98 M300 128 L358 134 M300 148 L352 170 M300 168 L347 200"/>
      </g>
      <g stroke="#8A8A8A" stroke-width="1" fill="none" opacity="0.5">
        <line x1="108" y1="262" x2="200" y2="220"/><line x1="200" y1="220" x2="292" y2="262"/>
        <line x1="292" y1="262" x2="292" y2="332"/><line x1="292" y1="332" x2="200" y2="374"/>
        <line x1="200" y1="374" x2="108" y2="332"/><line x1="108" y1="332" x2="108" y2="262"/>
        <line x1="292" y1="262" x2="384" y2="230"/><line x1="384" y1="230" x2="464" y2="272"/>
        <line x1="200" y1="374" x2="222" y2="404"/>
      </g>
      <g fill="#C8102E"><circle cx="108" cy="262" r="4.5"/><circle cx="292" cy="262" r="4.5"/>
        <circle cx="200" cy="374" r="4.5"/><circle cx="384" cy="230" r="3.5"/></g>
      <g fill="#FBFAF6" opacity="0.9"><circle cx="200" cy="220" r="3.5"/><circle cx="292" cy="332" r="3.5"/>
        <circle cx="108" cy="332" r="3.5"/><circle cx="464" cy="272" r="3.5"/><circle cx="222" cy="404" r="3.5"/></g>
    </svg>"""
    return Markup(svg)


_home_cache = {"data": None, "ts": 0}
HOME_CACHE_TTL = 5  # 초 — 짧게 잡아 새로고침 시 최신 내용이 바로 반영되게 (발행·수정 시에는 캐시를 즉시 비우므로 실제 지연은 거의 없음)
_HOME_CACHE_REDIS_KEY = "home_cache:v1"


def _get_cached_home_data():
    import json
    now = time.time()

    if _redis_client is not None:
        try:
            cached = _redis_client.get(_HOME_CACHE_REDIS_KEY)
            if cached:
                data = json.loads(cached)
                return data["member_count"], data["ticker_rows"], data["ranking_rows"]
        except Exception:
            pass  # Redis 조회 실패 시 그냥 아래에서 새로 계산합니다

    if _home_cache["data"] is None or now - _home_cache["ts"] > HOME_CACHE_TTL:
        db = get_db()
        member_count = db.execute("SELECT COUNT(*) FROM users WHERE role='reader'").fetchone()[0]
        manual_ticker = [dict(r) for r in db.execute(
            "SELECT a.id, a.title, a.category FROM ticker_articles ta "
            "JOIN articles a ON a.id=ta.article_id AND a.status='published' "
            "ORDER BY ta.position"
        ).fetchall()]
        if manual_ticker:
            ticker_rows = manual_ticker
        else:
            ticker_category = get_setting("ticker_category", DEFAULT_TICKER_CATEGORY)
            if ticker_category:
                ticker_rows = [dict(r) for r in db.execute(
                    "SELECT id, title, category FROM articles WHERE status='published' AND category=? "
                    "ORDER BY published_at DESC LIMIT 8",
                    (ticker_category,),
                ).fetchall()]
            else:
                ticker_rows = [dict(r) for r in db.execute(
                    "SELECT id, title, category FROM articles WHERE status='published' ORDER BY published_at DESC LIMIT 8"
                ).fetchall()]
        ranking_rows = [dict(r) for r in db.execute(
            "SELECT id, title, category, view_count FROM articles WHERE status='published' "
            "ORDER BY view_count DESC, published_at DESC LIMIT 5"
        ).fetchall()]
        _home_cache["data"] = (member_count, ticker_rows, ranking_rows)
        _home_cache["ts"] = now
        if _redis_client is not None:
            try:
                _redis_client.set(
                    _HOME_CACHE_REDIS_KEY,
                    json.dumps({"member_count": member_count, "ticker_rows": ticker_rows, "ranking_rows": ranking_rows}),
                    ex=HOME_CACHE_TTL,
                )
            except Exception:
                pass
    return _home_cache["data"]


def _invalidate_home_cache():
    """발행 상태가 바뀌는 즉시 캐시를 비웁니다 — 긴급 비공개 등이 지연 없이 바로 반영되어야 하므로."""
    _home_cache["data"] = None
    if _redis_client is not None:
        try:
            _redis_client.delete(_HOME_CACHE_REDIS_KEY)
        except Exception:
            pass


@app.context_processor
def inject_globals():
    user = current_user()
    member_count, ticker_rows, ranking_rows = _get_cached_home_data()
    return dict(
        user=user,
        categories=CATEGORIES,
        today=now_kst().strftime("%Y.%m.%d (%a)"),
        member_count=member_count,
        role_label=lambda r: ROLE_LABELS.get(r, r),
        status_label=lambda s: STATUS_LABELS.get(s, s),
        mini_svg=_mini_svg,
        hero_svg=_hero_svg,
        ticker_items=ticker_rows,
        ticker_speed_sec=get_setting("ticker_speed_sec", str(DEFAULT_TICKER_SPEED_SEC)),
        ranking_items=ranking_rows,
        current_category=None,
        site_name=SITE_NAME,
        site_tagline=SITE_TAGLINE,
        ad_slots=_load_active_ad_slots(),
        ga_measurement_id=os.environ.get("GA_MEASUREMENT_ID", ""),
    )


@app.route("/healthz")
def healthz():
    """UptimeRobot 같은 모니터링 도구가 주기적으로 호출하는 상태 확인용 엔드포인트.
    서버 프로세스뿐 아니라 DB 연결까지 확인해야, '서버는 떠 있는데 DB가 죽은' 상황을 잡아낼 수 있습니다."""
    try:
        get_db().execute("SELECT 1").fetchone()
    except Exception as e:
        security_logger.info("헬스체크 실패 — DB 연결 오류: %s", e)
        return {"status": "error", "db": "down"}, 503
    return {"status": "ok", "db": "ok"}, 200


@app.route("/newsletter", methods=["POST"])
def newsletter_subscribe():
    email = (request.form.get("email") or "").strip()[:120]
    selected_categories = [category for category in request.form.getlist("categories") if category in CATEGORIES]
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")
    if "@" not in email or "." not in email.split("@")[-1]:
        message = "올바른 이메일 주소를 입력해 주세요."
        if wants_json:
            return jsonify({"ok": False, "message": message}), 400
        flash(message, "error")
        return redirect((request.referrer or url_for("index")) + "#newsletter")
    db = get_db()
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    try:
        db.execute(
            "INSERT INTO newsletter_subscribers (email, created_at) VALUES (?,?)", (email, now)
        )
        new_subscription = True
    except _INTEGRITY_ERRORS:
        db.rollback()
        new_subscription = False
    db.execute("DELETE FROM newsletter_preferences WHERE email=?", (email,))
    db.executemany(
        "INSERT INTO newsletter_preferences (email, category, created_at) VALUES (?,?,?)",
        [(email, category, now) for category in selected_categories],
    )
    db.commit()
    log_activity("newsletter", "뉴스레터 구독", f"email={email} · categories={','.join(selected_categories) or '전체'} · new={new_subscription}")
    message = "뉴스레터 구독이 완료됐습니다. 구독되었습니다! 편집국이 고른 소식을 보내드릴게요." if new_subscription else "이미 구독 중인 이메일입니다. 구독 분야 설정을 업데이트했습니다."
    if wants_json:
        return jsonify({"ok": True, "message": message, "new_subscription": new_subscription})
    flash(message, "msg")
    return redirect((request.referrer or url_for("index")) + "#newsletter")


# ---------------------------------------------------------------- public site
@app.route("/")
def index():
    db = get_db()
    category = request.args.get("category")

    if category:
        page = max(1, request.args.get("page", 1, type=int))
        offset = (page - 1) * PAGE_SIZE
        total = db.execute(
            "SELECT COUNT(*) FROM articles WHERE status='published' AND category=?", (category,)
        ).fetchone()[0]
        arts = db.execute(
            "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
            "WHERE a.status='published' AND a.category=? ORDER BY a.published_at DESC, a.id DESC "
            "LIMIT ? OFFSET ?",
            (category, PAGE_SIZE, offset),
        ).fetchall()
        has_more = offset + len(arts) < total
        return render_template(
            "index.html", articles=arts, current_category=category, page=page, has_more=has_more
        )

    published = db.execute(
        "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
        "WHERE a.status='published' ORDER BY a.published_at DESC, a.id DESC LIMIT 60"
    ).fetchall()

    hero = published[0] if published else None
    side_stories = published[1:5] if published else []

    pick = db.execute(
        "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
        "WHERE a.status='published' AND a.is_pick=1 ORDER BY a.published_at DESC LIMIT 1"
    ).fetchone()

    opinion_pick = db.execute(
        "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
        "WHERE a.status='published' AND a.category='오피니언' ORDER BY a.published_at DESC LIMIT 1"
    ).fetchone()

    sections = {}
    for cat in CATEGORIES:
        cat_articles = [a for a in published if a["category"] == cat][:3]
        sections[cat] = cat_articles

    return render_template(
        "index.html", hero=hero, side_stories=side_stories, sections=sections, pick=pick,
        opinion_pick=opinion_pick,
        events=db.execute("SELECT * FROM events ORDER BY event_date LIMIT 3").fetchall(),
        jobs=db.execute("SELECT * FROM job_listings ORDER BY created_at DESC LIMIT 3").fetchall(),
    )


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * PAGE_SIZE
    results = []
    has_more = False
    total = 0
    if q:
        like = f"%{_escape_like(q)}%"
        db = get_db()
        total = db.execute(
            "SELECT COUNT(*) FROM articles WHERE status='published' AND "
            "(title LIKE ? ESCAPE '\\' OR deck LIKE ? ESCAPE '\\')",
            (like, like),
        ).fetchone()[0]
        results = db.execute(
            "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
            "WHERE a.status='published' AND (a.title LIKE ? ESCAPE '\\' OR a.deck LIKE ? ESCAPE '\\') "
            "ORDER BY a.published_at DESC, a.id DESC LIMIT ? OFFSET ?",
            (like, like, PAGE_SIZE, offset),
        ).fetchall()
        has_more = offset + len(results) < total
    return render_template(
        "search.html", query=q, results=results, page=page, has_more=has_more, total=total
    )


@app.route("/article/<int:article_id>")
def article_detail(article_id):
    db = get_db()
    article = db.execute(
        "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id WHERE a.id=?",
        (article_id,),
    ).fetchone()
    if article and article["status"] == "published":
        # 조회수는 메모리에 모았다가 주기적으로 한 번에 반영합니다.
        # (기사를 볼 때마다 DB에 쓰면 트래픽이 늘수록 병목이 되므로)
        _bump_view_count(article_id)
        article = dict(article)
        article["view_count"] = article["view_count"] + _pending_views.get(article_id, 0)
    user = current_user()
    if not article or article["status"] != "published":
        if not (article and user and (user["role"] == "editor" or user["id"] == article["author_id"])):
            abort(404)

    related = []
    is_bookmarked = False
    follows_category = False
    prev_article = None
    next_article = None
    comments = []
    if article["status"] == "published":
        manual_related = db.execute(
            "SELECT a.id, a.title, a.category, a.published_at FROM article_related ar "
            "JOIN articles a ON a.id=ar.related_article_id "
            "WHERE ar.article_id=? AND a.status='published' ORDER BY ar.position",
            (article_id,),
        ).fetchall()
        if manual_related:
            related = manual_related
        else:
            related = db.execute(
                "SELECT id, title, category, published_at FROM articles "
                "WHERE status='published' AND category=? AND id!=? ORDER BY published_at DESC LIMIT 3",
                (article["category"], article_id),
            ).fetchall()
        prev_article = db.execute(
            "SELECT id, title FROM articles WHERE status='published' AND category=? "
            "AND (published_at, id) < (?, ?) ORDER BY published_at DESC, id DESC LIMIT 1",
            (article["category"], article["published_at"], article_id),
        ).fetchone()
        next_article = db.execute(
            "SELECT id, title FROM articles WHERE status='published' AND category=? "
            "AND (published_at, id) > (?, ?) ORDER BY published_at ASC, id ASC LIMIT 1",
            (article["category"], article["published_at"], article_id),
        ).fetchone()
        comments = db.execute(
            "SELECT c.*, u.name AS commenter_name, u.role AS commenter_role FROM comments c "
            "JOIN users u ON u.id=c.user_id WHERE c.article_id=? ORDER BY c.created_at ASC",
            (article_id,),
        ).fetchall()
        if user:
            is_bookmarked = db.execute(
                "SELECT 1 FROM bookmarks WHERE user_id=? AND article_id=?", (user["id"], article_id)
            ).fetchone() is not None
            follows_category = db.execute(
                "SELECT 1 FROM topic_follows WHERE user_id=? AND topic_type='category' AND topic_value=?",
                (user["id"], article["category"]),
            ).fetchone() is not None

    article_tags = _get_article_tags(db, article_id)
    documents = db.execute("SELECT * FROM article_documents WHERE article_id=? ORDER BY id", (article_id,)).fetchall()
    transparency = db.execute("SELECT * FROM article_transparency WHERE article_id=?", (article_id,)).fetchone()
    corrections = db.execute(
        "SELECT c.*, u.name AS corrected_by_name FROM article_corrections c JOIN users u ON u.id=c.corrected_by "
        "WHERE c.article_id=? ORDER BY c.created_at DESC, c.id DESC",
        (article_id,),
    ).fetchall()
    questions = db.execute(
        "SELECT q.*, u.name AS answered_by_name FROM editorial_questions q LEFT JOIN users u ON u.id=q.answered_by "
        "WHERE q.article_id=? AND q.status='answered' ORDER BY q.answered_at DESC LIMIT 8",
        (article_id,),
    ).fetchall()

    return render_template(
        "article.html", article=article, related=related, is_bookmarked=is_bookmarked,
        prev_article=prev_article, next_article=next_article, comments=comments,
        article_tags=article_tags, follows_category=follows_category, documents=documents,
        transparency=transparency, corrections=corrections, questions=questions,
    )


@app.route("/article/<int:article_id>/report-typo", methods=["POST"])
def report_typo(article_id):
    db = get_db()
    article = db.execute("SELECT id FROM articles WHERE id=? AND status='published'", (article_id,)).fetchone()
    if not article:
        abort(404)
    if _rate_limited("typo_report", 8, 600):
        flash("잠시 후 다시 시도해 주세요.", "error")
        return redirect(url_for("article_detail", article_id=article_id) + "#typo")
    message = (request.form.get("message") or "").strip()[:500]
    email = (request.form.get("email") or "").strip()[:120]
    if not message:
        flash("어떤 부분이 틀렸는지 간단히 적어주세요.", "error")
        return redirect(url_for("article_detail", article_id=article_id) + "#typo")
    _record_rate_hit("typo_report")
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "INSERT INTO typo_reports (article_id, reporter_email, message, status, created_at) VALUES (?,?,?, 'open', ?)",
        (article_id, email or None, message, now),
    )
    db.commit()
    flash("제보 감사합니다. 편집국이 확인 후 반영하겠습니다.", "msg")
    return redirect(url_for("article_detail", article_id=article_id) + "#typo")


@app.route("/article/<int:article_id>/comment", methods=["POST"])
@login_required
def add_comment(article_id):
    user = current_user()
    db = get_db()
    article = db.execute("SELECT id FROM articles WHERE id=? AND status='published'", (article_id,)).fetchone()
    if not article:
        abort(404)
    if _rate_limited("comment", 10, 300):
        flash("댓글을 너무 자주 작성하고 있습니다. 잠시 후 다시 시도해 주세요.", "error")
        return redirect(url_for("article_detail", article_id=article_id) + "#comments")
    body = (request.form.get("body") or "").strip()[:1000]
    pin = (request.form.get("delete_pin") or "").strip()
    if not body:
        flash("댓글 내용을 입력해 주세요.", "error")
        return redirect(url_for("article_detail", article_id=article_id) + "#comments")
    if not re.fullmatch(r"\d{4}", pin):
        flash("삭제용 비밀번호는 숫자 4자리로 입력해 주세요.", "error")
        return redirect(url_for("article_detail", article_id=article_id) + "#comments")
    _record_rate_hit("comment")
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "INSERT INTO comments (article_id, user_id, body, created_at, delete_pin_hash) VALUES (?,?,?,?,?)",
        (article_id, user["id"], body, now, generate_password_hash(pin)),
    )
    db.commit()
    return redirect(url_for("article_detail", article_id=article_id) + "#comments")


@app.route("/comment/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):
    user = current_user()
    db = get_db()
    comment = db.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    if not comment:
        abort(404)
    is_owner = comment["user_id"] == user["id"]
    is_editor = user["role"] == "editor"
    if not is_owner and not is_editor:
        flash("본인 댓글만 삭제할 수 있습니다.", "error")
        return redirect(url_for("article_detail", article_id=comment["article_id"]) + "#comments")
    if is_owner and not is_editor:
        pin = (request.form.get("delete_pin") or "").strip()
        if not comment["delete_pin_hash"] or not check_password_hash(comment["delete_pin_hash"], pin):
            flash("삭제 비밀번호가 일치하지 않습니다.", "error")
            return redirect(url_for("article_detail", article_id=comment["article_id"]) + "#comments")
    article_id = comment["article_id"]
    db.execute("DELETE FROM comments WHERE id=?", (comment_id,))
    db.execute("DELETE FROM comment_reports WHERE comment_id=?", (comment_id,))
    db.commit()
    flash("댓글을 삭제했습니다.", "msg")
    return redirect(url_for("article_detail", article_id=article_id) + "#comments")


@app.route("/comment/<int:comment_id>/report", methods=["POST"])
@login_required
def report_comment(comment_id):
    user = current_user()
    db = get_db()
    comment = db.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
    if not comment:
        abort(404)
    if _rate_limited("comment_report", 10, 600):
        flash("잠시 후 다시 시도해 주세요.", "error")
        return redirect(url_for("article_detail", article_id=comment["article_id"]) + "#comments")
    reason = (request.form.get("reason") or "").strip()[:300]
    _record_rate_hit("comment_report")
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "INSERT INTO comment_reports (comment_id, reporter_user_id, reason, created_at) VALUES (?,?,?,?)",
        (comment_id, user["id"], reason, now),
    )
    db.commit()
    flash("댓글을 신고했습니다. 편집국에서 검토합니다.", "msg")
    return redirect(url_for("article_detail", article_id=comment["article_id"]) + "#comments")


@app.route("/tag/<name>")
def tag_articles(name):
    db = get_db()
    tag = db.execute("SELECT id, name FROM tags WHERE name=?", (name,)).fetchone()
    if not tag:
        abort(404)
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * PAGE_SIZE
    total = db.execute(
        "SELECT COUNT(*) FROM articles a JOIN article_tags at ON at.article_id=a.id "
        "WHERE at.tag_id=? AND a.status='published'",
        (tag["id"],),
    ).fetchone()[0]
    articles = db.execute(
        "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a "
        "JOIN article_tags at ON at.article_id=a.id JOIN users u ON u.id=a.author_id "
        "WHERE at.tag_id=? AND a.status='published' ORDER BY a.published_at DESC, a.id DESC LIMIT ? OFFSET ?",
        (tag["id"], PAGE_SIZE, offset),
    ).fetchall()
    has_more = offset + len(articles) < total
    return render_template(
        "tag.html", tag=tag, articles=articles, total=total, page=page, has_more=has_more
    )


@app.route("/reporter/<username>")
def reporter_profile(username):
    db = get_db()
    reporter = db.execute(
        "SELECT id, name, username, role, created_at FROM users WHERE username=? AND role IN ('journalist','editor')",
        (username,),
    ).fetchone()
    if not reporter:
        abort(404)
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * PAGE_SIZE
    total = db.execute(
        "SELECT COUNT(*) FROM articles WHERE status='published' AND author_id=?", (reporter["id"],)
    ).fetchone()[0]
    articles = db.execute(
        "SELECT * FROM articles WHERE status='published' AND author_id=? "
        "ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
        (reporter["id"], PAGE_SIZE, offset),
    ).fetchall()
    has_more = offset + len(articles) < total
    profile = db.execute("SELECT * FROM reporter_profiles WHERE user_id=?", (reporter["id"],)).fetchone()
    return render_template(
        "reporter.html", reporter=reporter, articles=articles, total=total,
        page=page, has_more=has_more, profile=profile,
    )


@app.route("/admin/profile", methods=["GET", "POST"])
@staff_required
def admin_profile():
    user = current_user()
    db = get_db()
    if request.method == "POST":
        expertise = (request.form.get("expertise") or "").strip()[:300]
        bio = (request.form.get("bio") or "").strip()[:1200]
        contact_email = (request.form.get("contact_email") or "").strip()[:120]
        tip_url = (request.form.get("tip_url") or "").strip()[:500]
        verification_note = (request.form.get("verification_note") or "").strip()[:400]
        avatar_filename = None
        avatar_upload = request.files.get("avatar")
        remove_avatar = request.form.get("remove_avatar") == "1"
        if avatar_upload and avatar_upload.filename:
            try:
                avatar_filename, _, _ = _save_uploaded_image(avatar_upload)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("admin_profile"))
        if contact_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact_email):
            flash("연락 이메일 형식이 올바르지 않습니다.", "error")
            return redirect(url_for("admin_profile"))
        if tip_url and not _valid_external_url(tip_url):
            flash("제보 링크는 http:// 또는 https://로 시작해야 합니다.", "error")
            return redirect(url_for("admin_profile"))
        existing = db.execute("SELECT user_id FROM reporter_profiles WHERE user_id=?", (user["id"],)).fetchone()
        now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        old_profile = db.execute("SELECT avatar_filename FROM reporter_profiles WHERE user_id=?", (user["id"],)).fetchone()
        old_avatar = old_profile["avatar_filename"] if old_profile else None
        if remove_avatar and not avatar_filename:
            avatar_filename = None
        elif not avatar_filename:
            avatar_filename = old_avatar
        if existing:
            db.execute(
                "UPDATE reporter_profiles SET expertise=?,bio=?,contact_email=?,tip_url=?,verification_note=?,avatar_filename=?,updated_at=? WHERE user_id=?",
                (expertise, bio, contact_email or None, tip_url or None, verification_note, avatar_filename, now, user["id"]),
            )
        else:
            db.execute(
                "INSERT INTO reporter_profiles (user_id,expertise,bio,contact_email,tip_url,verification_note,avatar_filename,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (user["id"], expertise, bio, contact_email or None, tip_url or None, verification_note, avatar_filename, now),
            )
        if (remove_avatar or avatar_filename != old_avatar) and old_avatar:
            _delete_uploaded_image(old_avatar)
        db.commit()
        log_activity(user["name"], "기자 프로필 저장", f"avatar={'updated' if avatar_filename else 'none'} · expertise={expertise[:80]}")
        flash("기자 프로필을 저장했습니다.", "msg")
        return redirect(url_for("admin_profile"))
    profile = db.execute("SELECT * FROM reporter_profiles WHERE user_id=?", (user["id"],)).fetchone()
    return render_template("admin/profile.html", profile=profile, active="profile")


@app.route("/bookmark/<int:article_id>", methods=["POST"])
@login_required
def toggle_bookmark(article_id):
    user = current_user()
    db = get_db()
    article = db.execute("SELECT id FROM articles WHERE id=? AND status='published'", (article_id,)).fetchone()
    if not article:
        abort(404)
    existing = db.execute(
        "SELECT id FROM bookmarks WHERE user_id=? AND article_id=?", (user["id"], article_id)
    ).fetchone()
    if existing:
        db.execute("DELETE FROM bookmarks WHERE id=?", (existing["id"],))
        db.commit()
        flash("스크랩을 취소했습니다.", "msg")
    else:
        now = now_kst().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO bookmarks (user_id, article_id, created_at) VALUES (?,?,?)",
            (user["id"], article_id, now),
        )
        db.commit()
        flash("기사를 스크랩했습니다.", "msg")
    return redirect(request.referrer or url_for("article_detail", article_id=article_id))


@app.route("/mypage")
@login_required
def mypage():
    user = current_user()
    db = get_db()
    saved = db.execute(
        "SELECT a.*, u.name AS author_name, u.username AS author_username FROM bookmarks b "
        "JOIN articles a ON a.id=b.article_id JOIN users u ON u.id=a.author_id "
        "WHERE b.user_id=? ORDER BY b.created_at DESC",
        (user["id"],),
    ).fetchall()
    follows = db.execute(
        "SELECT topic_type, topic_value FROM topic_follows WHERE user_id=? ORDER BY topic_type, topic_value",
        (user["id"],),
    ).fetchall()
    followed_categories = [row["topic_value"] for row in follows if row["topic_type"] == "category"]
    followed_tags = [row["topic_value"] for row in follows if row["topic_type"] == "tag"]
    clauses, params = [], []
    if followed_categories:
        clauses.append("a.category IN (" + ",".join("?" for _ in followed_categories) + ")")
        params.extend(followed_categories)
    if followed_tags:
        clauses.append(
            "a.id IN (SELECT at.article_id FROM article_tags at JOIN tags t ON t.id=at.tag_id "
            "WHERE t.name IN (" + ",".join("?" for _ in followed_tags) + "))"
        )
        params.extend(followed_tags)
    recommendations = []
    if clauses:
        recommendations = db.execute(
            "SELECT DISTINCT a.*, u.name AS author_name FROM articles a JOIN users u ON u.id=a.author_id "
            "WHERE a.status='published' AND (" + " OR ".join(clauses) + ") "
            "ORDER BY a.published_at DESC, a.id DESC LIMIT 8",
            params,
        ).fetchall()
    industry_follows = db.execute(
        "SELECT topic_type, topic_value FROM industry_follows WHERE user_id=? ORDER BY topic_type, topic_value",
        (user["id"],),
    ).fetchall()
    industry_feed = _industry_feed(db, industry_follows)
    teams = db.execute(
        "SELECT o.*, m.role AS membership_role FROM team_organizations o JOIN team_members m ON m.organization_id=o.id "
        "WHERE m.user_id=? ORDER BY o.name",
        (user["id"],),
    ).fetchall()
    return render_template(
        "mypage.html", saved=saved, follows=follows, followed_categories=followed_categories,
        followed_tags=followed_tags, recommendations=recommendations, industry_follows=industry_follows,
        industry_feed=industry_feed, teams=teams,
    )


@app.route("/topics/toggle", methods=["POST"])
@login_required
def toggle_topic_follow():
    user = current_user()
    topic_type = request.form.get("topic_type", "")
    topic_value = request.form.get("topic_value", "").strip()[:80]
    if topic_type not in ("category", "tag") or not topic_value:
        abort(400)
    db = get_db()
    if topic_type == "category" and topic_value not in CATEGORIES:
        abort(400)
    if topic_type == "tag" and not db.execute("SELECT 1 FROM tags WHERE name=?", (topic_value,)).fetchone():
        abort(404)
    existing = db.execute(
        "SELECT id FROM topic_follows WHERE user_id=? AND topic_type=? AND topic_value=?",
        (user["id"], topic_type, topic_value),
    ).fetchone()
    if existing:
        db.execute("DELETE FROM topic_follows WHERE id=?", (existing["id"],))
        message = f"{topic_value} 팔로우를 해제했습니다."
    else:
        db.execute(
            "INSERT INTO topic_follows (user_id,topic_type,topic_value,created_at) VALUES (?,?,?,?)",
            (user["id"], topic_type, topic_value, now_kst().strftime("%Y-%m-%d %H:%M")),
        )
        message = f"{topic_value}을(를) 팔로우했습니다."
    db.commit()
    flash(message, "msg")
    return redirect(request.referrer or url_for("mypage"))


@app.route("/industry/toggle", methods=["POST"])
@login_required
def toggle_industry_follow():
    user = current_user()
    topic_type = request.form.get("topic_type", "")
    topic_value = (request.form.get("topic_value") or "").strip()[:80]
    if topic_type not in INDUSTRY_FOLLOW_TYPES or not topic_value:
        abort(400)
    db = get_db()
    existing = db.execute(
        "SELECT id FROM industry_follows WHERE user_id=? AND topic_type=? AND topic_value=?",
        (user["id"], topic_type, topic_value),
    ).fetchone()
    if existing:
        db.execute("DELETE FROM industry_follows WHERE id=?", (existing["id"],))
        message = f"{topic_value} 알림을 해제했습니다."
    else:
        db.execute(
            "INSERT INTO industry_follows (user_id,topic_type,topic_value,created_at) VALUES (?,?,?,?)",
            (user["id"], topic_type, topic_value, now_kst().strftime("%Y-%m-%d %H:%M")),
        )
        message = f"{topic_value} 알림을 추가했습니다. 최근 7일 기사는 마이페이지에서 확인할 수 있습니다."
    db.commit()
    flash(message, "msg")
    return redirect(request.referrer or url_for("mypage"))


@app.route("/teams", methods=["GET", "POST"])
@login_required
def teams():
    user = current_user()
    db = get_db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:100]
        domain = (request.form.get("domain") or "").strip().lower()[:120]
        seat_limit = max(2, min(100, request.form.get("seat_limit", 5, type=int)))
        if not name:
            flash("조직명을 입력해 주세요.", "error")
            return redirect(url_for("teams"))
        if domain and not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", domain):
            flash("조직 도메인 형식이 올바르지 않습니다.", "error")
            return redirect(url_for("teams"))
        now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            "INSERT INTO team_organizations (name,domain,owner_id,seat_limit,created_at) VALUES (?,?,?,?,?)",
            (name, domain or None, user["id"], seat_limit, now),
        )
        organization_id = cur.lastrowid
        db.execute(
            "INSERT INTO team_members (organization_id,user_id,role,joined_at) VALUES (?,?, 'owner', ?)",
            (organization_id, user["id"], now),
        )
        db.commit()
        flash("법인·팀 공간을 만들었습니다. 구성원을 추가하고 관심 키워드를 설정해 주세요.", "msg")
        return redirect(url_for("team_detail", organization_id=organization_id))
    organizations = db.execute(
        "SELECT o.*, m.role AS membership_role, (SELECT COUNT(*) FROM team_members tm WHERE tm.organization_id=o.id) AS member_count "
        "FROM team_organizations o JOIN team_members m ON m.organization_id=o.id WHERE m.user_id=? ORDER BY o.name",
        (user["id"],),
    ).fetchall()
    return render_template("teams.html", organizations=organizations)


@app.route("/teams/<int:organization_id>", methods=["GET", "POST"])
@login_required
def team_detail(organization_id):
    user = current_user()
    db = get_db()
    organization = _team_for_user(db, organization_id, user["id"])
    if not organization:
        abort(404)
    is_owner = organization["membership_role"] == "owner"
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "add_member" and is_owner:
            username = (request.form.get("username") or "").strip()[:40]
            target = db.execute("SELECT id,name FROM users WHERE username=?", (username,)).fetchone()
            seats = db.execute("SELECT COUNT(*) AS c FROM team_members WHERE organization_id=?", (organization_id,)).fetchone()["c"]
            if not target:
                flash("가입된 회원 아이디를 입력해 주세요.", "error")
            elif seats >= organization["seat_limit"]:
                flash("사용 가능한 좌석이 없습니다. 좌석 한도를 늘려 주세요.", "error")
            elif db.execute("SELECT 1 FROM team_members WHERE organization_id=? AND user_id=?", (organization_id, target["id"])).fetchone():
                flash("이미 팀에 참여한 회원입니다.", "error")
            else:
                db.execute(
                    "INSERT INTO team_members (organization_id,user_id,role,joined_at) VALUES (?,?, 'member', ?)",
                    (organization_id, target["id"], now_kst().strftime("%Y-%m-%d %H:%M:%S")),
                )
                db.commit()
                flash(f"{target['name']}님을 팀에 추가했습니다.", "msg")
        elif action == "remove_member" and is_owner:
            member_id = request.form.get("member_id", type=int)
            db.execute("DELETE FROM team_members WHERE id=? AND organization_id=? AND role!='owner'", (member_id, organization_id))
            db.commit()
            flash("구성원을 팀에서 제거했습니다.", "msg")
        elif action == "toggle_follow" and is_owner:
            topic_type = request.form.get("topic_type", "")
            topic_value = (request.form.get("topic_value") or "").strip()[:80]
            if topic_type not in INDUSTRY_FOLLOW_TYPES or not topic_value:
                abort(400)
            existing = db.execute(
                "SELECT id FROM team_follows WHERE organization_id=? AND topic_type=? AND topic_value=?",
                (organization_id, topic_type, topic_value),
            ).fetchone()
            if existing:
                db.execute("DELETE FROM team_follows WHERE id=?", (existing["id"],))
                flash("팀 관심 키워드를 해제했습니다.", "msg")
            else:
                db.execute(
                    "INSERT INTO team_follows (organization_id,topic_type,topic_value,created_at) VALUES (?,?,?,?)",
                    (organization_id, topic_type, topic_value, now_kst().strftime("%Y-%m-%d %H:%M:%S")),
                )
                flash("팀 관심 키워드를 추가했습니다.", "msg")
            db.commit()
        return redirect(url_for("team_detail", organization_id=organization_id))
    members = db.execute(
        "SELECT m.*, u.name,u.username FROM team_members m JOIN users u ON u.id=m.user_id WHERE m.organization_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 ELSE 1 END,u.name",
        (organization_id,),
    ).fetchall()
    follows = db.execute("SELECT * FROM team_follows WHERE organization_id=? ORDER BY topic_type,topic_value", (organization_id,)).fetchall()
    team_feed = _industry_feed(db, follows)
    return render_template(
        "team.html", organization=organization, members=members, follows=follows, team_feed=team_feed, is_owner=is_owner,
    )


@app.route("/questions", methods=["POST"])
def submit_editorial_question():
    if _rate_limited("editorial_question", 6, 600):
        flash("잠시 후 다시 시도해 주세요.", "error")
        return redirect(request.referrer or url_for("index"))
    article_id = request.form.get("article_id", type=int)
    if article_id and not get_db().execute("SELECT 1 FROM articles WHERE id=? AND status='published'", (article_id,)).fetchone():
        abort(404)
    user = current_user()
    name = (request.form.get("name") or (user["name"] if user else "")).strip()[:80]
    email = (request.form.get("email") or "").strip()[:120]
    question = (request.form.get("question") or "").strip()[:1000]
    if not name or not question or (email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        flash("이름과 질문을 입력하고, 이메일은 올바른 형식으로 입력해 주세요.", "error")
        return redirect(request.referrer or url_for("index"))
    db = get_db()
    db.execute(
        "INSERT INTO editorial_questions (article_id,asker_name,asker_email,question,status,asked_at) VALUES (?,?,?,?, 'open', ?)",
        (article_id, name, email or None, question, now_kst().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    _record_rate_hit("editorial_question")
    flash("질문이 편집국에 접수되었습니다. 답변 가능 여부를 검토합니다.", "msg")
    return redirect(request.referrer or url_for("index"))


@app.route("/article/<int:article_id>/trust-card.svg")
def article_trust_card(article_id):
    article = get_db().execute(
        "SELECT a.*, u.name AS author_name FROM articles a JOIN users u ON u.id=a.author_id WHERE a.id=? AND a.status='published'",
        (article_id,),
    ).fetchone()
    if not article:
        abort(404)
    title = str(escape((article["title"] or "")[:52]))
    deck = str(escape((article["deck"] or "")[:88]))
    category = str(escape(article["category"] or "MEDITALK"))
    date = str(escape((article["published_at"] or "")[:10]))
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#071c39"/><stop offset=".58" stop-color="#0b4ea2"/><stop offset="1" stop-color="#e83b4f"/></linearGradient></defs><rect width="1200" height="630" fill="url(#g)"/><circle cx="1020" cy="88" r="240" fill="#6bd7ff" opacity=".14"/><rect x="70" y="70" width="1060" height="490" rx="34" fill="#ffffff" opacity=".1" stroke="#ffffff" stroke-opacity=".35"/><text x="110" y="145" fill="#fff" font-size="30" font-family="Arial,sans-serif" font-weight="700">MEDITALK · {category}</text><text x="110" y="260" fill="#fff" font-size="64" font-family="Arial,sans-serif" font-weight="700">{title}</text><text x="110" y="334" fill="#dcecff" font-size="29" font-family="Arial,sans-serif">{deck}</text><line x1="110" y1="450" x2="1090" y2="450" stroke="#ffffff" stroke-opacity=".38"/><text x="110" y="510" fill="#fff" font-size="25" font-family="Arial,sans-serif">취재 {str(escape(article['author_name']))} · 발행 {date}</text><text x="900" y="510" fill="#fff" font-size="28" font-family="Arial,sans-serif" font-weight="700">MEDI<tspan fill="#ff6274">TALK</tspan></text></svg>'''
    response = app.response_class(svg, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.route("/rss.xml")
def rss_feed():
    db = get_db()
    arts = db.execute(
        "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
        "WHERE a.status='published' ORDER BY a.published_at DESC LIMIT 30"
    ).fetchall()
    items = []
    for a in arts:
        link = url_for("article_detail", article_id=a["id"], _external=True)
        items.append(
            f"<item><title><![CDATA[{a['title']}]]></title>"
            f"<link>{link}</link><guid>{link}</guid>"
            f"<description><![CDATA[{a['deck']}]]></description>"
            f"<category>{a['category']}</category>"
            f"<pubDate>{a['published_at']}</pubDate></item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>{SITE_NAME}</title>"
        f"<link>{url_for('index', _external=True)}</link>"
        f"<description>{SITE_TAGLINE}</description>"
        + "".join(items) +
        "</channel></rss>"
    )
    return app.response_class(xml, mimetype="application/rss+xml")


@app.route("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        "Disallow: /mypage",
        "Disallow: /search",
        f"Sitemap: {url_for('sitemap_xml', _external=True)}",
    ]
    return app.response_class("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    db = get_db()
    arts = db.execute(
        "SELECT id, published_at FROM articles WHERE status='published' ORDER BY published_at DESC"
    ).fetchall()
    static_paths = [
        url_for("index", _external=True),
        url_for("privacy_policy", _external=True),
        url_for("terms_of_service", _external=True),
        url_for("youth_policy", _external=True),
        url_for("contact", _external=True),
    ]
    urls = [f"<url><loc>{u}</loc></url>" for u in static_paths]
    for a in arts:
        loc = url_for("article_detail", article_id=a["id"], _external=True)
        lastmod = (a["published_at"] or "")[:10]
        urls.append(f"<url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(urls) +
        "</urlset>"
    )
    return app.response_class(xml, mimetype="application/xml")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:80]
        email = request.form.get("email", "").strip()[:120]
        company = request.form.get("company", "").strip()[:120]
        message = request.form.get("message", "").strip()[:2000]
        if not name or not email or not message:
            flash("이름, 이메일, 문의 내용을 모두 입력해 주세요.", "error")
            return render_template("contact.html")
        db = get_db()
        now = now_kst().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO inquiries (name, email, company, message, created_at) VALUES (?,?,?,?,?)",
            (name, email, company, message, now),
        )
        db.commit()
        flash("문의가 접수됐습니다. 빠른 시일 내 회신드리겠습니다.", "msg")
        return redirect(url_for("contact"))
    return render_template("contact.html")


@app.route("/admin/inquiries")
@editor_required
def admin_inquiries():
    db = get_db()
    inquiries = db.execute(
        "SELECT * FROM inquiries ORDER BY created_at DESC"
    ).fetchall()
    return render_template("admin/inquiries.html", inquiries=inquiries, active="inquiries")


@app.route("/admin/questions")
@editor_required
def admin_questions():
    questions = get_db().execute(
        "SELECT q.*, a.title AS article_title, u.name AS answered_by_name FROM editorial_questions q "
        "LEFT JOIN articles a ON a.id=q.article_id LEFT JOIN users u ON u.id=q.answered_by ORDER BY CASE q.status WHEN 'open' THEN 0 ELSE 1 END,q.asked_at DESC"
    ).fetchall()
    return render_template("admin/questions.html", questions=questions, active="questions")


@app.route("/admin/questions/<int:question_id>/answer", methods=["POST"])
@editor_required
def admin_answer_question(question_id):
    answer = (request.form.get("answer") or "").strip()[:2000]
    status = request.form.get("status", "answered")
    if status not in ("answered", "hidden") or (status == "answered" and not answer):
        flash("공개 답변을 입력하거나 비공개 처리를 선택해 주세요.", "error")
        return redirect(url_for("admin_questions"))
    db = get_db()
    question = db.execute("SELECT id FROM editorial_questions WHERE id=?", (question_id,)).fetchone()
    if not question:
        abort(404)
    now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE editorial_questions SET status=?,answer=?,answered_by=?,answered_at=? WHERE id=?",
        (status, answer if status == "answered" else None, current_user()["id"], now, question_id),
    )
    db.commit()
    log_activity(current_user()["name"], "편집국 Q&A 처리", f"질문 #{question_id} · {status}")
    flash("Q&A 상태를 저장했습니다.", "msg")
    return redirect(url_for("admin_questions"))


@app.route("/admin/ads", methods=["GET", "POST"])
@editor_required
def admin_ads():
    db = get_db()
    valid_slots = {slot["key"] for slot in AD_SLOT_CATALOG}
    now = now_kst().strftime("%Y-%m-%d %H:%M:%S")

    if request.method == "POST":
        action = request.form.get("action", "create")
        campaign_id = request.form.get("campaign_id", type=int)
        campaign = None
        if campaign_id:
            campaign = db.execute("SELECT * FROM ad_campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not campaign:
                abort(404)

        if action == "delete":
            db.execute("DELETE FROM ad_placements WHERE campaign_id=?", (campaign_id,))
            db.execute("DELETE FROM ad_events WHERE campaign_id=?", (campaign_id,))
            db.execute("DELETE FROM ad_campaigns WHERE id=?", (campaign_id,))
            db.commit()
            _delete_uploaded_image(campaign["image_filename"])
            log_activity(current_user()["name"], "광고 캠페인 삭제", campaign["name"])
            flash("광고 캠페인을 삭제했습니다.", "msg")
            return redirect(url_for("admin_ads"))

        if action == "toggle":
            db.execute("UPDATE ad_campaigns SET is_active=?, updated_at=? WHERE id=?", (request.form.get("is_active") == "1", now, campaign_id))
            db.commit()
            state = "활성화" if request.form.get("is_active") == "1" else "비활성화"
            log_activity(current_user()["name"], f"광고 캠페인 {state}", campaign["name"])
            flash(f"광고 캠페인을 {state}했습니다.", "msg")
            return redirect(url_for("admin_ads"))

        name = request.form.get("name", "").strip()[:100]
        sponsor = request.form.get("sponsor", "").strip()[:100]
        target_url = request.form.get("target_url", "").strip()[:500]
        starts_at = request.form.get("starts_at", "").strip()[:10] or None
        ends_at = request.form.get("ends_at", "").strip()[:10] or None
        selected_slots = [key for key in request.form.getlist("slot_keys") if key in valid_slots]
        upload = request.files.get("image")
        if not name or not selected_slots:
            flash("캠페인 이름과 한 곳 이상의 노출 위치를 선택해 주세요.", "error")
            return redirect(url_for("admin_ads"))
        if not _valid_external_url(target_url):
            flash("광고 연결 주소는 http:// 또는 https://로 시작해야 합니다.", "error")
            return redirect(url_for("admin_ads"))
        if starts_at and ends_at and starts_at > ends_at:
            flash("종료일은 시작일보다 빠를 수 없습니다.", "error")
            return redirect(url_for("admin_ads"))

        image_filename = campaign["image_filename"] if campaign else ""
        if upload and upload.filename:
            try:
                new_filename, _, _ = _save_uploaded_image(upload)
            except ImageUploadError as exc:
                flash(str(exc), "error")
                return redirect(url_for("admin_ads"))
            if new_filename:
                if image_filename:
                    _delete_uploaded_image(image_filename)
                image_filename = new_filename
        if not image_filename:
            flash("광고 이미지를 하나 첨부해 주세요.", "error")
            return redirect(url_for("admin_ads"))

        if campaign:
            db.execute(
                "UPDATE ad_campaigns SET name=?, sponsor=?, target_url=?, image_filename=?, starts_at=?, ends_at=?, updated_at=? WHERE id=?",
                (name, sponsor, target_url or None, image_filename, starts_at, ends_at, now, campaign_id),
            )
        else:
            cur = db.execute(
                "INSERT INTO ad_campaigns (name,sponsor,target_url,image_filename,starts_at,ends_at,is_active,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,1,?,?)",
                (name, sponsor or None, target_url or None, image_filename, starts_at, ends_at, now, now),
            )
            campaign_id = cur.lastrowid

        # 선택한 자리는 현재 캠페인으로 즉시 교체합니다. 한 슬롯에는 한 광고만 노출됩니다.
        for slot_key in selected_slots:
            db.execute("DELETE FROM ad_placements WHERE slot_key=?", (slot_key,))
        db.execute("DELETE FROM ad_placements WHERE campaign_id=?", (campaign_id,))
        db.executemany(
            "INSERT INTO ad_placements (campaign_id,slot_key,created_at) VALUES (?,?,?)",
            [(campaign_id, slot_key, now) for slot_key in selected_slots],
        )
        db.commit()
        log_activity(current_user()["name"], "광고 캠페인 저장", f"{name} · {', '.join(selected_slots)}")
        flash(f"{name} 캠페인을 저장했습니다. 선택한 {len(selected_slots)}개 위치에 즉시 교체 반영됩니다.", "msg")
        return redirect(url_for("admin_ads"))

    raw_campaigns = db.execute("SELECT * FROM ad_campaigns ORDER BY updated_at DESC, id DESC").fetchall()
    placements = db.execute("SELECT campaign_id, slot_key FROM ad_placements").fetchall()
    by_campaign = {}
    occupied = {}
    for row in placements:
        by_campaign.setdefault(row["campaign_id"], []).append(row["slot_key"])
        occupied[row["slot_key"]] = row["campaign_id"]
    campaigns = [{**dict(row), "slot_keys": by_campaign.get(row["id"], [])} for row in raw_campaigns]
    slots = [{**slot, "campaign_id": occupied.get(slot["key"])} for slot in AD_SLOT_CATALOG]
    return render_template("admin/ads.html", campaigns=campaigns, slots=slots, active="ads")


@app.route("/ad/click/<int:campaign_id>/<slot_key>")
def ad_click(campaign_id, slot_key):
    if not _slot_definition(slot_key):
        abort(404)
    db = get_db()
    campaign = db.execute(
        "SELECT c.target_url FROM ad_campaigns c JOIN ad_placements p ON p.campaign_id=c.id "
        "WHERE c.id=? AND p.slot_key=? AND c.is_active=1",
        (campaign_id, slot_key),
    ).fetchone()
    if not campaign:
        abort(404)
    db.execute(
        "INSERT INTO ad_events (campaign_id,slot_key,event_type,created_at) VALUES (?,?,?,?)",
        (campaign_id, slot_key, "click", now_kst().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    return redirect(campaign["target_url"] or url_for("index"))


@app.route("/ad/impression/<int:campaign_id>/<slot_key>.gif")
def ad_impression(campaign_id, slot_key):
    if not _slot_definition(slot_key):
        abort(404)
    db = get_db()
    campaign = db.execute(
        "SELECT c.id FROM ad_campaigns c JOIN ad_placements p ON p.campaign_id=c.id "
        "WHERE c.id=? AND p.slot_key=? AND c.is_active=1",
        (campaign_id, slot_key),
    ).fetchone()
    if not campaign:
        abort(404)
    db.execute(
        "INSERT INTO ad_events (campaign_id,slot_key,event_type,created_at) VALUES (?,?,?,?)",
        (campaign_id, slot_key, "impression", now_kst().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    return app.response_class(
        base64.b64decode("R0lGODlhAQABAAAAACwAAAAAAQABAAA="), status=200, mimetype="image/gif"
    )


@app.route("/admin/typos")
@editor_required
def admin_typos():
    db = get_db()
    reports = db.execute(
        "SELECT tr.*, a.title AS article_title FROM typo_reports tr "
        "JOIN articles a ON a.id=tr.article_id ORDER BY tr.status ASC, tr.created_at DESC"
    ).fetchall()
    return render_template("admin/typos.html", reports=reports, active="typos")


@app.route("/admin/typos/<int:report_id>/resolve", methods=["POST"])
@editor_required
def admin_resolve_typo(report_id):
    db = get_db()
    db.execute("UPDATE typo_reports SET status='resolved' WHERE id=?", (report_id,))
    db.commit()
    flash("처리 완료로 표시했습니다.", "msg")
    return redirect(url_for("admin_typos"))


@app.route("/admin/reports")
@editor_required
def admin_comment_reports():
    db = get_db()
    reports = db.execute(
        "SELECT cr.*, c.body AS comment_body, c.article_id, u.name AS reporter_name, "
        "cu.name AS commenter_name, a.title AS article_title "
        "FROM comment_reports cr "
        "JOIN comments c ON c.id=cr.comment_id "
        "JOIN users u ON u.id=cr.reporter_user_id "
        "JOIN users cu ON cu.id=c.user_id "
        "JOIN articles a ON a.id=c.article_id "
        "ORDER BY cr.created_at DESC"
    ).fetchall()
    return render_template("admin/reports.html", reports=reports, active="reports")


@app.route("/admin/reports/<int:report_id>/dismiss", methods=["POST"])
@editor_required
def admin_dismiss_report(report_id):
    db = get_db()
    db.execute("DELETE FROM comment_reports WHERE id=?", (report_id,))
    db.commit()
    flash("신고를 처리(기각)했습니다.", "msg")
    return redirect(url_for("admin_comment_reports"))


def _smtp_configured():
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))


@app.route("/admin/newsletter")
@editor_required
def admin_newsletter_preview():
    db = get_db()
    since = (now_kst() - timedelta(days=7)).strftime("%Y-%m-%d")
    recent = db.execute(
        "SELECT a.*, u.name AS author_name FROM articles a JOIN users u ON u.id=a.author_id "
        "WHERE a.status='published' AND a.published_at >= ? ORDER BY a.published_at DESC LIMIT 15",
        (since,),
    ).fetchall()
    sub_count = db.execute("SELECT COUNT(*) FROM newsletter_subscribers").fetchone()[0]
    preference_counts = {
        category: db.execute(
            "SELECT COUNT(DISTINCT email) FROM newsletter_preferences WHERE category=?", (category,)
        ).fetchone()[0]
        for category in CATEGORIES
    }
    return render_template(
        "admin/newsletter.html", recent=recent, sub_count=sub_count,
        preference_counts=preference_counts, smtp_ready=_smtp_configured(), active="newsletter",
    )


@app.route("/admin/newsletter/send", methods=["POST"])
@editor_required
def admin_newsletter_send():
    if not _smtp_configured():
        flash(
            "이메일 발송 서버(SMTP)가 아직 설정되지 않았습니다. "
            "Render 환경변수에 SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS를 설정하면 발송할 수 있습니다.",
            "error",
        )
        return redirect(url_for("admin_newsletter_preview"))

    db = get_db()
    since = (now_kst() - timedelta(days=7)).strftime("%Y-%m-%d")
    selected_category = request.form.get("category", "")
    if selected_category and selected_category not in CATEGORIES:
        abort(400)
    if selected_category:
        recent = db.execute(
            "SELECT title, deck, id FROM articles WHERE status='published' AND published_at >= ? AND category=? "
            "ORDER BY published_at DESC LIMIT 15", (since, selected_category),
        ).fetchall()
        subscribers = [row["email"] for row in db.execute(
            "SELECT ns.email FROM newsletter_subscribers ns WHERE NOT EXISTS "
            "(SELECT 1 FROM newsletter_preferences p WHERE p.email=ns.email) OR EXISTS "
            "(SELECT 1 FROM newsletter_preferences p WHERE p.email=ns.email AND p.category=?)", (selected_category,),
        ).fetchall()]
    else:
        recent = db.execute(
            "SELECT title, deck, id FROM articles WHERE status='published' AND published_at >= ? "
            "ORDER BY published_at DESC LIMIT 15", (since,),
        ).fetchall()
        subscribers = [row["email"] for row in db.execute("SELECT email FROM newsletter_subscribers").fetchall()]
    if not recent:
        flash("최근 7일 이내 발행된 기사가 없어 발송할 내용이 없습니다.", "error")
        return redirect(url_for("admin_newsletter_preview"))

    if not subscribers:
        flash("구독자가 아직 없습니다.", "error")
        return redirect(url_for("admin_newsletter_preview"))

    import smtplib
    from email.mime.text import MIMEText

    topic_label = f"{selected_category} 소식" if selected_category else "이번 주 소식"
    lines = [f"<h2>{SITE_NAME} {topic_label}</h2>"]
    for a in recent:
        link = url_for("article_detail", article_id=a["id"], _external=True)
        lines.append(f'<p><a href="{link}">{a["title"]}</a><br>{a["deck"]}</p>')
    html_body = "".join(lines)

    sent, failed = 0, 0
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            for email in subscribers:
                try:
                    msg = MIMEText(html_body, "html", "utf-8")
                    msg["Subject"] = f"{SITE_NAME} {topic_label}"
                    msg["From"] = smtp_user
                    msg["To"] = email
                    server.sendmail(smtp_user, [email], msg.as_string())
                    sent += 1
                except Exception:
                    failed += 1
    except Exception as e:
        security_logger.info("뉴스레터 발송 실패 — %s", e)
        flash(f"이메일 서버 연결에 실패했습니다: {e}", "error")
        return redirect(url_for("admin_newsletter_preview"))

    security_logger.info("뉴스레터 발송 — sent=%s failed=%s by=%s", sent, failed, current_user()["username"])
    flash(f"{topic_label}을 {sent}명에게 발송 완료 (실패 {failed}건).", "msg")
    return redirect(url_for("admin_newsletter_preview"))


@app.route("/admin/backup")
@editor_required
def admin_backup():
    from flask import send_file
    security_logger.info("DB 백업 다운로드 — user=%s ip=%s", current_user()["username"], _client_ip())
    ts = now_kst().strftime("%Y%m%d-%H%M")
    return send_file(
        DB_PATH, as_attachment=True, download_name=f"meditok-backup-{ts}.db"
    )


@app.route("/events")
def events_page():
    db = get_db()
    events = db.execute("SELECT * FROM events ORDER BY event_date").fetchall()
    return render_template("events.html", events=events)


@app.route("/jobs")
def jobs_page():
    db = get_db()
    jobs = db.execute("SELECT * FROM job_listings ORDER BY created_at DESC").fetchall()
    return render_template("jobs.html", jobs=jobs)


@app.route("/admin/events", methods=["GET", "POST"])
@editor_required
def admin_events():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            eid = request.form.get("event_id", type=int)
            db.execute("DELETE FROM events WHERE id=?", (eid,))
            db.commit()
            log_activity(current_user()["name"], "행사 삭제", f"id={eid}")
            flash("행사를 삭제했습니다.", "msg")
        else:
            title = request.form.get("title", "").strip()[:200]
            event_date = request.form.get("event_date", "").strip()[:50]
            location = request.form.get("location", "").strip()[:200]
            if not (title and event_date and location):
                flash("행사명, 일정, 장소를 모두 입력해 주세요.", "error")
            else:
                db.execute(
                    "INSERT INTO events (title, event_date, location, created_at) VALUES (?,?,?,?)",
                    (title, event_date, location, now_kst().strftime("%Y-%m-%d %H:%M")),
                )
                db.commit()
                log_activity(current_user()["name"], "행사 등록", title)
                flash("행사를 등록했습니다.", "msg")
        return redirect(url_for("admin_events"))

    events = db.execute("SELECT * FROM events ORDER BY event_date").fetchall()
    return render_template("admin/events.html", active="events", events=events)


@app.route("/admin/jobs", methods=["GET", "POST"])
@editor_required
def admin_jobs():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            jid = request.form.get("job_id", type=int)
            db.execute("DELETE FROM job_listings WHERE id=?", (jid,))
            db.commit()
            log_activity(current_user()["name"], "채용정보 삭제", f"id={jid}")
            flash("채용정보를 삭제했습니다.", "msg")
        else:
            company = request.form.get("company", "").strip()[:100]
            position = request.form.get("position", "").strip()[:200]
            experience_level = request.form.get("experience_level", "").strip()[:50]
            if not (company and position and experience_level):
                flash("회사명, 포지션, 경력 조건을 모두 입력해 주세요.", "error")
            else:
                db.execute(
                    "INSERT INTO job_listings (company, position, experience_level, created_at) VALUES (?,?,?,?)",
                    (company, position, experience_level, now_kst().strftime("%Y-%m-%d %H:%M")),
                )
                db.commit()
                log_activity(current_user()["name"], "채용정보 등록", f"{company} - {position}")
                flash("채용정보를 등록했습니다.", "msg")
        return redirect(url_for("admin_jobs"))

    jobs = db.execute("SELECT * FROM job_listings ORDER BY created_at DESC").fetchall()
    return render_template("admin/jobs.html", active="jobs", jobs=jobs)


@app.route("/about")
def about():
    return render_template("legal.html", page="about")


@app.route("/ethics")
def ethics_code():
    return render_template("legal.html", page="ethics")


@app.route("/subscribe-info")
def subscribe_info():
    return render_template("legal.html", page="subscribe")


@app.route("/correction-policy")
def correction_policy():
    return render_template("legal.html", page="correction")


@app.route("/privacy")
def privacy_policy():
    return render_template("legal.html", page="privacy")


@app.route("/terms")
def terms_of_service():
    return render_template("legal.html", page="terms")


@app.route("/youth-policy")
def youth_policy():
    return render_template("legal.html", page="youth")


# ---------------------------------------------------------------- auth
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        if _rate_limited("signup", 5, 600):
            flash("잠시 후 다시 시도해 주세요.", "error")
            return render_template("signup.html")
        name = request.form.get("name", "").strip()[:60]
        username = request.form.get("username", "").strip()[:40]
        password = request.form.get("password", "")
        if not name or not username or len(password) < 8:
            flash("이름, 아이디, 8자 이상의 비밀번호를 입력해 주세요.", "error")
            return render_template("signup.html")
        if not username.replace("_", "").replace("-", "").isalnum():
            flash("아이디는 영문, 숫자, '-', '_'만 사용할 수 있습니다.", "error")
            return render_template("signup.html")
        db = get_db()
        exists = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            _record_rate_hit("signup")
            flash("이미 사용 중인 아이디입니다.", "error")
            return render_template("signup.html")
        _record_rate_hit("signup")
        now = now_kst().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO users (username,password_hash,name,role,created_at) VALUES (?,?,?,?,?)",
            (username, generate_password_hash(password), name, "reader", now),
        )
        db.commit()
        flash("회원가입이 완료됐습니다. 로그인해 주세요.", "msg")
        return redirect(url_for("login"))
    return render_template("signup.html")


def _safe_next_url(raw_next):
    """next 파라미터로 오픈 리다이렉트 공격(외부 사이트로 유도)을 막습니다.
    우리 사이트 내부의 절대경로("/..."）만 허용하고, "//evil.com" 같은 프로토콜 상대 URL이나
    외부 도메인은 전부 거부합니다."""
    if not raw_next:
        return None
    if not raw_next.startswith("/") or raw_next.startswith("//"):
        return None
    return raw_next


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        client_ip = _client_ip()
        if _rate_limited("login", LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SEC, client_ip):
            flash("로그인 시도가 너무 많습니다. 5분 후 다시 시도해 주세요.", "error")
            return render_template("login.html")
        username = request.form.get("username", "").strip()[:40]
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row:
            password_ok = check_password_hash(row["password_hash"], password)
        else:
            # 존재하지 않는 아이디라도 해시 비교를 흉내 내어, 응답 시간으로 계정 존재 여부가 새지 않게 합니다.
            check_password_hash(_DUMMY_PASSWORD_HASH, password)
            password_ok = False
        if row and password_ok:
            session.clear()
            session["user_id"] = row["id"]
            flash(f"{row['name']}님, 환영합니다.", "msg")
            safe_next = _safe_next_url(request.args.get("next"))
            if safe_next:
                return redirect(safe_next)
            if row["role"] in ("journalist", "editor"):
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("index"))
        _record_rate_hit("login", client_ip)
        security_logger.info("로그인 실패 — ip=%s username=%s", client_ip, username)
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("로그아웃되었습니다.", "msg")
    return redirect(url_for("index"))


# ---------------------------------------------------------------- admin (journalist/editor) site
@app.route("/admin/ticker-settings", methods=["GET", "POST"])
@editor_required
def admin_ticker_settings():
    db = get_db()
    if request.method == "POST":
        speed = request.form.get("ticker_speed_sec", "").strip()
        category = request.form.get("ticker_category", "").strip()
        try:
            speed_val = int(speed)
            if not (5 <= speed_val <= 120):
                raise ValueError
        except ValueError:
            flash("스크롤 속도는 5~120 사이의 숫자로 입력해 주세요.", "error")
            return redirect(url_for("admin_ticker_settings"))
        if category and category not in CATEGORIES:
            flash("올바르지 않은 섹션입니다.", "error")
            return redirect(url_for("admin_ticker_settings"))

        selected_ids = request.form.getlist("ticker_article_ids")
        valid_ids = []
        for sid in selected_ids[:12]:
            try:
                aid = int(sid)
            except ValueError:
                continue
            row = db.execute("SELECT id FROM articles WHERE id=? AND status='published'", (aid,)).fetchone()
            if row:
                valid_ids.append(aid)

        set_setting("ticker_speed_sec", str(speed_val))
        set_setting("ticker_category", category)
        db.execute("DELETE FROM ticker_articles")
        for i, aid in enumerate(valid_ids, start=1):
            db.execute("INSERT INTO ticker_articles (article_id, position) VALUES (?,?)", (aid, i))
        db.commit()
        _invalidate_home_cache()
        log_activity(current_user()["name"], "속보 티커 설정 변경",
                     f"속도={speed_val}s, 섹션={category or '전체'}, 수동선택={len(valid_ids)}건")
        flash("속보 티커 설정을 저장했습니다.", "msg")
        return redirect(url_for("admin_ticker_settings"))

    published = db.execute(
        "SELECT id, title, category FROM articles WHERE status='published' ORDER BY category, published_at DESC"
    ).fetchall()
    selected_ids = {r["article_id"] for r in db.execute("SELECT article_id FROM ticker_articles").fetchall()}
    by_category = {}
    for a in published:
        by_category.setdefault(a["category"], []).append(a)

    return render_template(
        "admin/ticker_settings.html", active="ticker",
        by_category=by_category, selected_ids=selected_ids, categories=CATEGORIES,
        ticker_speed_sec=get_setting("ticker_speed_sec", str(DEFAULT_TICKER_SPEED_SEC)),
        ticker_category=get_setting("ticker_category", DEFAULT_TICKER_CATEGORY),
    )


@app.route("/admin")
@staff_required
def admin_dashboard():
    user = current_user()
    db = get_db()
    # 상태별 필터 (작성중/심사대기/예약발행/발행됨/반려됨 탭)
    status_filter = request.args.get("status")
    if status_filter not in ("draft", "pending", "scheduled", "published", "rejected"):
        status_filter = None

    if user["role"] == "editor":
        base_sql = (
            "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
        )
        if status_filter:
            arts = db.execute(base_sql + "WHERE a.status=? ORDER BY a.updated_at DESC", (status_filter,)).fetchall()
        else:
            arts = db.execute(base_sql + "ORDER BY a.updated_at DESC").fetchall()
        # 통계는 필터와 무관하게 항상 전체 기준으로 집계
        all_for_stats = db.execute("SELECT status FROM articles").fetchall()
    else:
        base_sql = (
            "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
            "WHERE a.author_id=? "
        )
        if status_filter:
            arts = db.execute(base_sql + "AND a.status=? ORDER BY a.updated_at DESC", (user["id"], status_filter)).fetchall()
        else:
            arts = db.execute(base_sql + "ORDER BY a.updated_at DESC", (user["id"],)).fetchall()
        all_for_stats = db.execute("SELECT status FROM articles WHERE author_id=?", (user["id"],)).fetchall()

    stats = {"draft": 0, "pending": 0, "published": 0, "rejected": 0, "scheduled": 0}
    for a in all_for_stats:
        stats[a["status"]] = stats.get(a["status"], 0) + 1

    chart_data = None
    if user["role"] == "editor":
        daily_counts = []
        for i in range(13, -1, -1):
            day = (now_kst() - timedelta(days=i)).strftime("%Y-%m-%d")
            count = db.execute(
                "SELECT COUNT(*) FROM articles WHERE status='published' AND published_at LIKE ?",
                (day + "%",),
            ).fetchone()[0]
            daily_counts.append({"label": day[5:], "count": count})
        cat_counts = db.execute(
            "SELECT category, COUNT(*) AS c FROM articles WHERE status='published' "
            "GROUP BY category ORDER BY c DESC"
        ).fetchall()
        max_daily = max([d["count"] for d in daily_counts] + [1])
        max_cat = max([c["c"] for c in cat_counts] + [1]) if cat_counts else 1
        chart_data = {
            "daily": daily_counts, "max_daily": max_daily,
            "categories": cat_counts, "max_cat": max_cat,
        }

    return render_template(
        "admin/dashboard.html", articles=arts, stats=stats, active="dashboard",
        chart_data=chart_data, status_filter=status_filter, categories=CATEGORIES,
        ticker_speed_sec=get_setting("ticker_speed_sec", str(DEFAULT_TICKER_SPEED_SEC)),
        ticker_category=get_setting("ticker_category", DEFAULT_TICKER_CATEGORY),
        ticker_manual_count=db.execute("SELECT COUNT(*) AS c FROM ticker_articles").fetchone()["c"],
    )


@app.route("/admin/review")
@editor_required
def admin_review():
    db = get_db()
    arts = db.execute(
        "SELECT a.*, u.name AS author_name, u.username AS author_username FROM articles a JOIN users u ON u.id=a.author_id "
        "WHERE a.status='pending' ORDER BY a.updated_at ASC"
    ).fetchall()
    return render_template("admin/review.html", articles=arts, active="review")


def _clean_article_fields():
    """폼에서 받은 기사 필드를 안전한 길이로 자르고 검증합니다."""
    title = request.form.get("title", "").strip()[:200]
    category = request.form.get("category", "")
    deck = request.form.get("deck", "").strip()[:500]
    body = request.form.get("body", "").strip()[:20000]
    if category not in CATEGORIES:
        category = CATEGORIES[0]
    return title, category, deck, body


def _parse_tags():
    """쉼표로 구분된 태그 입력값을 정리합니다. 최대 8개, 태그당 최대 20자."""
    raw = request.form.get("tags", "")
    seen = []
    for part in raw.split(","):
        tag = part.strip().lstrip("#")[:20]
        if tag and tag.lower() not in [s.lower() for s in seen]:
            seen.append(tag)
        if len(seen) >= 8:
            break
    return seen


def _save_article_tags(db, article_id, tag_names):
    db.execute("DELETE FROM article_tags WHERE article_id=?", (article_id,))
    for name in tag_names:
        row = db.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if row:
            tag_id = row["id"]
        else:
            cur = db.execute("INSERT INTO tags (name) VALUES (?)", (name,))
            tag_id = cur.lastrowid
        db.execute(
            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?,?)",
            (article_id, tag_id),
        )


def _get_article_tags(db, article_id):
    return db.execute(
        "SELECT t.id, t.name FROM tags t JOIN article_tags at ON at.tag_id=t.id "
        "WHERE at.article_id=? ORDER BY t.name",
        (article_id,),
    ).fetchall()


def _parse_scheduled_at():
    """<input type=datetime-local> 값을 파싱하고, 미래 시각인지 검증합니다.
    반환: (scheduled_at 문자열, 에러메시지) — 문제 없으면 에러메시지는 None."""
    raw = request.form.get("scheduled_at", "").strip()
    if not raw:
        return None, "예약 발행 시각을 입력해 주세요."
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M").replace(tzinfo=KST)
    except ValueError:
        return None, "예약 시각 형식이 올바르지 않습니다."
    if dt <= now_kst():
        return None, "예약 시각은 현재보다 미래여야 합니다."
    return dt.strftime("%Y-%m-%d %H:%M"), None


@app.route("/admin/write", methods=["GET", "POST"])
@staff_required
def admin_write():
    user = current_user()
    if request.method == "POST":
        title, category, deck, body = _clean_article_fields()
        if not title or not deck or not body:
            flash("제목, 요약, 본문을 모두 입력해 주세요.", "error")
            return render_template("admin/write.html", article=None, categories=CATEGORIES, active="write")

        try:
            image_filename, image_w, image_h = _save_uploaded_image(request.files.get("image"))
        except ImageUploadError as e:
            flash(str(e), "error")
            return render_template("admin/write.html", article=None, categories=CATEGORIES, active="write")
        image_caption = request.form.get("image_caption", "").strip()[:200]

        action = request.form.get("action", "save")
        now = now_kst().strftime("%Y-%m-%d %H:%M")
        scheduled_at = None
        published_at = None

        if action == "publish" and user["role"] == "editor":
            status = "published"
            published_at = now
        elif action == "schedule" and user["role"] == "editor":
            scheduled_at, err = _parse_scheduled_at()
            if err:
                flash(err, "error")
                return render_template("admin/write.html", article=None, categories=CATEGORIES, active="write")
            status = "scheduled"
        elif action == "submit_review":
            status = "pending"
        else:
            status = "draft"

        db = get_db()
        cur = db.execute(
            """INSERT INTO articles
               (title,category,deck,body,author_id,status,created_at,updated_at,published_at,
                image_filename,image_width,image_height,image_caption,scheduled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, category, deck, body, user["id"], status, now, now, published_at,
             image_filename, image_w, image_h, image_caption if image_filename else None, scheduled_at),
        )
        new_id = cur.lastrowid
        _save_article_tags(db, new_id, _parse_tags())
        body_captions = request.form.getlist("body_image_caption")
        _replace_article_images(db, new_id, request.files.getlist("body_images"), body_captions)
        try:
            _save_article_documents(db, new_id, request.form.getlist("document_label"), request.form.getlist("document_url"))
        except ValueError as exc:
            db.rollback()
            flash(str(exc), "error")
            return render_template("admin/write.html", article=None, categories=CATEGORIES, active="write")
        if user["role"] == "editor":
            _save_article_checklist(db, new_id, user["id"], request.form)
            _save_article_transparency(db, new_id, user["id"], request.form)
        if user["role"] == "editor":
            _save_related_articles(db, new_id, request.form.getlist("related_ids"))
        db.commit()
        if status == "published":
            _invalidate_home_cache()
            log_activity(user["name"], "기사 작성·즉시발행", title)
            flash("기사를 즉시 발행했습니다.", "msg")
        elif status == "scheduled":
            log_activity(user["name"], "기사 예약발행 등록", f"{title} ({scheduled_at})")
            flash(f"{scheduled_at}에 자동 발행되도록 예약했습니다.", "msg")
        else:
            if status == "pending":
                log_activity(user["name"], "기사 작성·심사요청", title)
            flash("기사가 심사 요청되었습니다." if status == "pending" else "초안이 저장되었습니다.", "msg")
        return redirect(url_for("admin_dashboard"))
    recent_published = get_db().execute(
        "SELECT id, title FROM articles WHERE status='published' ORDER BY published_at DESC LIMIT 50"
    ).fetchall()
    return render_template("admin/write.html", article=None, categories=CATEGORIES, active="write", recent_published=recent_published)


@app.route("/admin/edit/<int:article_id>", methods=["GET", "POST"])
@staff_required
def admin_edit(article_id):
    user = current_user()
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article:
        abort(404)
    is_owner = article["author_id"] == user["id"]
    is_editor = user["role"] == "editor"
    if not is_owner and not is_editor:
        flash("본인이 작성한 기사만 수정할 수 있습니다.", "error")
        return redirect(url_for("admin_dashboard"))
    # 기자는 초안·반려·심사대기 상태의 본인 기사만 수정 가능. 발행된 기사는 편집장만 수정 가능.
    if article["status"] == "published" and not is_editor:
        flash("발행된 기사는 편집장만 수정할 수 있습니다.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        # 낙관적 잠금(optimistic locking): 이 사람이 편집 화면을 열었을 때의 수정시각과
        # 지금 DB의 수정시각이 다르면, 그 사이에 다른 사람이 먼저 저장한 것입니다.
        # 조용히 덮어쓰지 않고, 사용자에게 알려서 최신 내용을 다시 불러오게 합니다.
        submitted_version = request.form.get("_version", "")
        if submitted_version and submitted_version != article["updated_at"]:
            flash(
                "다른 사람이 방금 이 기사를 먼저 수정했습니다. "
                "덮어쓰지 않도록 저장을 취소했으니, 최신 내용을 다시 불러온 뒤 수정해 주세요.",
                "error",
            )
            article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
            return render_template(
                "admin/write.html", article=article, categories=CATEGORIES, active="write",
                current_tags=", ".join(t["name"] for t in _get_article_tags(db, article_id)),
            )

        title, category, deck, body = _clean_article_fields()
        if not title or not deck or not body:
            flash("제목, 요약, 본문을 모두 입력해 주세요.", "error")
            return render_template("admin/write.html", article=article, categories=CATEGORIES, active="write")

        _record_article_version(db, article, user, request.form.get("change_note", "").strip())

        try:
            new_image_filename, new_image_w, new_image_h = _save_uploaded_image(request.files.get("image"))
        except ImageUploadError as e:
            flash(str(e), "error")
            return render_template("admin/write.html", article=article, categories=CATEGORIES, active="write")

        remove_image = request.form.get("remove_image") == "1"
        image_caption = request.form.get("image_caption", "").strip()[:200]

        if new_image_filename:
            _delete_uploaded_image(article["image_filename"])
            image_filename, image_w, image_h = new_image_filename, new_image_w, new_image_h
        elif remove_image:
            _delete_uploaded_image(article["image_filename"])
            image_filename, image_w, image_h = None, None, None
        else:
            image_filename = article["image_filename"]
            image_w, image_h = article["image_width"], article["image_height"]

        action = request.form.get("action", "save")
        # updated_at은 분 단위가 아니라 초 단위까지 기록합니다.
        # (동시편집 충돌 감지가 "같은 분 안에 저장된 두 번째 수정"을 구분할 수 있어야 하므로)
        now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        tag_names = _parse_tags()
        body_files = request.files.getlist("body_images")
        body_captions = request.form.getlist("body_image_caption")
        remove_body_images = request.form.get("remove_body_images") == "1"
        related_ids = request.form.getlist("related_ids")

        def _sync_extras():
            if remove_body_images:
                _replace_article_images(db, article_id, [], [])
            else:
                _keep_article_images(db, article_id, body_files, body_captions)
            _save_article_documents(db, article_id, request.form.getlist("document_label"), request.form.getlist("document_url"))
            if is_editor:
                _save_related_articles(db, article_id, related_ids)
                _save_article_checklist(db, article_id, user["id"], request.form)

        if article["status"] == "published":
            # 발행된 기사를 편집장이 고치는 경우 — 상태·발행일은 그대로 두고 내용만 갱신
            db.execute(
                "UPDATE articles SET title=?, category=?, deck=?, body=?, updated_at=?, "
                "image_filename=?, image_width=?, image_height=?, image_caption=? WHERE id=?",
                (title, category, deck, body, now,
                 image_filename, image_w, image_h, image_caption if image_filename else None, article_id),
            )
            _save_article_tags(db, article_id, tag_names)
            _sync_extras()
            try:
                _record_public_correction(db, article_id, user["id"], request.form)
            except ValueError as exc:
                db.rollback()
                flash(str(exc), "error")
                return render_template("admin/write.html", article=article, categories=CATEGORIES, active="write")
            db.commit()
            _invalidate_home_cache()
            flash("발행된 기사 내용을 수정했습니다.", "msg")
        elif action == "publish" and is_editor:
            db.execute(
                "UPDATE articles SET title=?, category=?, deck=?, body=?, status='published', "
                "updated_at=?, published_at=?, scheduled_at=NULL, review_note=NULL, "
                "image_filename=?, image_width=?, image_height=?, image_caption=? WHERE id=?",
                (title, category, deck, body, now, now,
                 image_filename, image_w, image_h, image_caption if image_filename else None, article_id),
            )
            _save_article_tags(db, article_id, tag_names)
            _sync_extras()
            db.commit()
            _invalidate_home_cache()
            log_activity(current_user()["name"], "기사 수정·즉시발행", title)
            flash("기사를 즉시 발행했습니다.", "msg")
        elif action == "schedule" and is_editor:
            scheduled_at, err = _parse_scheduled_at()
            if err:
                flash(err, "error")
                return render_template("admin/write.html", article=article, categories=CATEGORIES, active="write")
            db.execute(
                "UPDATE articles SET title=?, category=?, deck=?, body=?, status='scheduled', "
                "updated_at=?, scheduled_at=?, review_note=NULL, "
                "image_filename=?, image_width=?, image_height=?, image_caption=? WHERE id=?",
                (title, category, deck, body, now, scheduled_at,
                 image_filename, image_w, image_h, image_caption if image_filename else None, article_id),
            )
            _save_article_tags(db, article_id, tag_names)
            _sync_extras()
            db.commit()
            flash(f"{scheduled_at}에 자동 발행되도록 예약했습니다.", "msg")
        else:
            status = "pending" if action == "submit_review" else "draft"
            db.execute(
                "UPDATE articles SET title=?, category=?, deck=?, body=?, status=?, updated_at=?, review_note=NULL, "
                "image_filename=?, image_width=?, image_height=?, image_caption=? WHERE id=?",
                (title, category, deck, body, status, now,
                 image_filename, image_w, image_h, image_caption if image_filename else None, article_id),
            )
            _save_article_tags(db, article_id, tag_names)
            _sync_extras()
            db.commit()
            flash("기사가 심사 요청되었습니다." if status == "pending" else "수정 내용이 저장되었습니다.", "msg")
        return redirect(url_for("admin_dashboard"))

    body_images = db.execute(
        "SELECT * FROM article_images WHERE article_id=? ORDER BY position", (article_id,)
    ).fetchall()
    documents = db.execute("SELECT * FROM article_documents WHERE article_id=? ORDER BY id", (article_id,)).fetchall()
    checklist = db.execute("SELECT * FROM article_checklists WHERE article_id=?", (article_id,)).fetchone()
    transparency = db.execute("SELECT * FROM article_transparency WHERE article_id=?", (article_id,)).fetchone()
    related_current = db.execute(
        "SELECT related_article_id FROM article_related WHERE article_id=? ORDER BY position", (article_id,)
    ).fetchall()
    recent_published = db.execute(
        "SELECT id, title FROM articles WHERE status='published' AND id!=? ORDER BY published_at DESC LIMIT 50",
        (article_id,),
    ).fetchall()
    return render_template(
        "admin/write.html", article=article, categories=CATEGORIES, active="write",
        current_tags=", ".join(t["name"] for t in _get_article_tags(db, article_id)),
        body_images=body_images, related_current=[r["related_article_id"] for r in related_current],
        recent_published=recent_published, documents=documents, checklist=checklist, transparency=transparency,
    )


@app.route("/admin/articles/<int:article_id>/history")
@staff_required
def admin_article_history(article_id):
    user = current_user()
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article:
        abort(404)
    if user["role"] != "editor" and article["author_id"] != user["id"]:
        abort(403)
    versions = db.execute(
        "SELECT v.*, u.name AS changed_by_name FROM article_versions v JOIN users u ON u.id=v.changed_by "
        "WHERE v.article_id=? ORDER BY v.created_at DESC, v.id DESC",
        (article_id,),
    ).fetchall()
    return render_template("admin/history.html", article=article, versions=versions, active="write")


@app.route("/admin/articles/<int:article_id>/history/<int:version_id>/restore", methods=["POST"])
@editor_required
def admin_restore_article_version(article_id, version_id):
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    version = db.execute("SELECT * FROM article_versions WHERE id=? AND article_id=?", (version_id, article_id)).fetchone()
    if not article or not version:
        abort(404)
    _record_article_version(db, article, current_user(), f"복원 전 자동 보관 · 이력 #{version_id}")
    now = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    published_at = article["published_at"] if version["status"] == "published" else None
    db.execute(
        "UPDATE articles SET title=?,category=?,deck=?,body=?,status=?,updated_at=?,published_at=?,scheduled_at=?,"
        "image_filename=?,image_caption=?,review_note=NULL WHERE id=?",
        (version["title"], version["category"], version["deck"], version["body"], version["status"], now,
         published_at, version["scheduled_at"], version["image_filename"], version["image_caption"], article_id),
    )
    db.commit()
    _invalidate_home_cache()
    log_activity(current_user()["name"], "기사 이력 복원", f"{article['title']} · 이력 #{version_id}")
    flash("선택한 이력으로 기사를 복원했습니다.", "msg")
    return redirect(url_for("admin_edit", article_id=article_id))


@app.route("/admin/submit/<int:article_id>", methods=["POST"])
@staff_required
def admin_submit(article_id):
    user = current_user()
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article or article["author_id"] != user["id"]:
        abort(404)
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    db.execute("UPDATE articles SET status='pending', updated_at=? WHERE id=?", (now, article_id))
    db.commit()
    flash("심사 요청을 보냈습니다.", "msg")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/publish/<int:article_id>", methods=["POST"])
@editor_required
def admin_publish(article_id):
    db = get_db()
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    article = db.execute("SELECT title FROM articles WHERE id=?", (article_id,)).fetchone()
    db.execute(
        "UPDATE articles SET status='published', updated_at=?, published_at=?, scheduled_at=NULL WHERE id=?",
        (now, now, article_id),
    )
    db.commit()
    _invalidate_home_cache()
    log_activity(current_user()["name"], "기사 발행", article["title"] if article else f"id={article_id}")
    flash("기사가 홈페이지에 발행되었습니다.", "msg")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/unschedule/<int:article_id>", methods=["POST"])
@editor_required
def admin_unschedule(article_id):
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article or article["status"] != "scheduled":
        flash("예약 발행 상태의 기사만 취소할 수 있습니다.", "error")
        return redirect(request.referrer or url_for("admin_dashboard"))
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "UPDATE articles SET status='draft', scheduled_at=NULL, updated_at=? WHERE id=?",
        (now, article_id),
    )
    db.commit()
    flash("예약 발행을 취소하고 초안으로 되돌렸습니다.", "msg")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/unpublish/<int:article_id>", methods=["POST"])
@editor_required
def admin_unpublish(article_id):
    """법적 문제·오보 등으로 발행된 기사를 즉시 비공개로 전환합니다 (사고 대응용)."""
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article or article["status"] != "published":
        flash("발행된 기사만 비공개로 전환할 수 있습니다.", "error")
        return redirect(request.referrer or url_for("admin_dashboard"))
    note = (request.form.get("note") or "").strip()
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "UPDATE articles SET status='draft', is_pick=0, updated_at=?, review_note=? WHERE id=?",
        (now, note or "편집장에 의해 긴급 비공개 처리됨", article_id),
    )
    db.commit()
    _invalidate_home_cache()
    security_logger.info(
        "긴급 비공개 처리 — article_id=%s user=%s ip=%s", article_id, current_user()["username"], _client_ip()
    )
    log_activity(current_user()["name"], "긴급 비공개", f"{article['title']} (사유: {note or '미기재'})")
    flash(f"'{article['title']}' 기사를 비공개로 전환했습니다.", "msg")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/pick/<int:article_id>", methods=["POST"])
@editor_required
def admin_pick(article_id):
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article or article["status"] != "published":
        flash("발행된 기사만 '이주의 PICK'으로 지정할 수 있습니다.", "error")
        return redirect(request.referrer or url_for("admin_dashboard"))

    if article["is_pick"]:
        # 이미 PICK인 기사를 다시 누르면 해제
        db.execute("UPDATE articles SET is_pick=0 WHERE id=?", (article_id,))
        db.commit()
        flash(f"'{article['title']}' 기사의 이주의 PICK 지정을 해제했습니다.", "msg")
    else:
        db.execute("UPDATE articles SET is_pick=0")
        db.execute("UPDATE articles SET is_pick=1 WHERE id=?", (article_id,))
        db.commit()
        flash(f"'{article['title']}' 기사를 이주의 메디톡 PICK으로 지정했습니다.", "msg")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/reject/<int:article_id>", methods=["POST"])
@editor_required
def admin_reject(article_id):
    db = get_db()
    note = (request.form.get("note") or "").strip()
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    article = db.execute("SELECT title FROM articles WHERE id=?", (article_id,)).fetchone()
    db.execute(
        "UPDATE articles SET status='rejected', updated_at=?, review_note=? WHERE id=?",
        (now, note or None, article_id),
    )
    db.commit()
    log_activity(current_user()["name"], "기사 반려", f"{article['title'] if article else article_id} (사유: {note or '미기재'})")
    flash("기사를 반려했습니다.", "msg")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/delete/<int:article_id>", methods=["POST"])
@staff_required
def admin_delete(article_id):
    user = current_user()
    db = get_db()
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article:
        abort(404)
    if article["author_id"] != user["id"] and user["role"] != "editor":
        flash("삭제 권한이 없습니다.", "error")
        return redirect(url_for("admin_dashboard"))
    if article["status"] == "published":
        # 발행된 기사는 댓글·스크랩이 달려 있을 수 있어 바로 삭제하면 DB 오류가 납니다.
        flash("발행된 기사는 바로 삭제할 수 없습니다. 먼저 '긴급 비공개'로 내린 뒤 삭제해 주세요.", "error")
        return redirect(url_for("admin_dashboard"))
    db.execute("DELETE FROM comments WHERE article_id=?", (article_id,))
    db.execute("DELETE FROM bookmarks WHERE article_id=?", (article_id,))
    db.execute("DELETE FROM article_tags WHERE article_id=?", (article_id,))
    db.execute("DELETE FROM typo_reports WHERE article_id=?", (article_id,))
    db.execute("DELETE FROM articles WHERE id=?", (article_id,))
    db.commit()
    _delete_uploaded_image(article["image_filename"])
    _invalidate_home_cache()
    log_activity(user["name"], "기사 삭제", article["title"])
    flash("기사를 삭제했습니다.", "msg")
    return redirect(url_for("admin_dashboard"))


# 실제 DB 테이블을 안전하게 들여다볼 수 있는 화이트리스트.
# 원본 SQL을 그대로 실행하게 두면 위험하므로, 테이블/컬럼을 명시적으로 허용한 것만 보여줍니다.
# password_hash, delete_pin_hash 같은 민감 컬럼은 애초에 목록에서 뺐습니다(설령 요청해도 절대 안 보여줌).
DB_VIEWER_TABLES = {
    "users": ["id", "username", "name", "role", "created_at"],
    "articles": ["id", "title", "category", "status", "author_id", "view_count", "is_pick",
                 "created_at", "updated_at", "published_at", "scheduled_at"],
    "comments": ["id", "article_id", "user_id", "body", "created_at"],
    "comment_reports": ["id", "comment_id", "reporter_user_id", "reason", "created_at"],
    "bookmarks": ["id", "user_id", "article_id", "created_at"],
    "tags": ["id", "name"],
    "article_tags": ["article_id", "tag_id"],
    "article_images": ["id", "article_id", "filename", "width", "height", "caption", "position"],
    "article_related": ["article_id", "related_article_id", "position"],
    "typo_reports": ["id", "article_id", "reporter_email", "message", "status", "created_at"],
    "newsletter_subscribers": ["id", "email", "created_at"],
    "newsletter_preferences": ["id", "email", "category", "created_at"],
    "reporter_profiles": ["user_id", "expertise", "bio", "contact_email", "tip_url", "verification_note", "avatar_filename", "updated_at"],
    "inquiries": ["id", "name", "email", "company", "message", "created_at"],
    "events": ["id", "title", "event_date", "location", "created_at"],
    "job_listings": ["id", "company", "position", "experience_level", "created_at"],
    "activity_log": ["id", "actor_name", "action", "detail", "created_at"],
    "site_settings": ["key", "value"],
    "ticker_articles": ["article_id", "position"],
}
DB_VIEWER_PAGE_SIZE = 30


@app.route("/admin/db-viewer")
@editor_required
def admin_db_viewer():
    db = get_db()
    counts = {}
    for table in DB_VIEWER_TABLES:
        try:
            counts[table] = db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        except Exception:
            counts[table] = None
    return render_template("admin/db_viewer.html", active="dbviewer", tables=DB_VIEWER_TABLES, counts=counts)


@app.route("/admin/db-viewer/<table>")
@editor_required
def admin_db_viewer_table(table):
    if table not in DB_VIEWER_TABLES:
        abort(404)
    db = get_db()
    columns = DB_VIEWER_TABLES[table]
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * DB_VIEWER_PAGE_SIZE
    total = db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    # 컬럼명은 위 화이트리스트에서만 가져오므로 SQL 인젝션 걱정 없이 안전하게 조립할 수 있습니다.
    col_sql = ", ".join(columns)
    order_col = "id" if "id" in columns else columns[0]
    rows = db.execute(
        f"SELECT {col_sql} FROM {table} ORDER BY {order_col} DESC LIMIT ? OFFSET ?",
        (DB_VIEWER_PAGE_SIZE, offset),
    ).fetchall()
    has_more = offset + len(rows) < total
    return render_template(
        "admin/db_viewer_table.html", active="dbviewer", table=table, columns=columns,
        rows=rows, total=total, page=page, has_more=has_more,
    )


@app.route("/admin/overview")
@editor_required
def admin_overview():
    """사이트 전체 현황을 한눈에 보는 페이지 — 회원·기사·댓글·문의 등 주요 지표와 최근 관리 활동 로그."""
    db = get_db()

    def count(sql, params=()):
        return db.execute(sql, params).fetchone()["c"]

    stats = {
        "users_total": count("SELECT COUNT(*) AS c FROM users"),
        "readers": count("SELECT COUNT(*) AS c FROM users WHERE role='reader'"),
        "journalists": count("SELECT COUNT(*) AS c FROM users WHERE role='journalist'"),
        "editors": count("SELECT COUNT(*) AS c FROM users WHERE role='editor'"),
        "articles_published": count("SELECT COUNT(*) AS c FROM articles WHERE status='published'"),
        "articles_pending": count("SELECT COUNT(*) AS c FROM articles WHERE status='pending'"),
        "articles_draft": count("SELECT COUNT(*) AS c FROM articles WHERE status='draft'"),
        "comments_total": count("SELECT COUNT(*) AS c FROM comments"),
        "comment_reports_open": count("SELECT COUNT(*) AS c FROM comment_reports"),
        "bookmarks_total": count("SELECT COUNT(*) AS c FROM bookmarks"),
        "newsletter_subscribers": count("SELECT COUNT(*) AS c FROM newsletter_subscribers"),
        "inquiries_total": count("SELECT COUNT(*) AS c FROM inquiries"),
        "typos_open": count("SELECT COUNT(*) AS c FROM typo_reports WHERE status!='resolved'"),
        "events_total": count("SELECT COUNT(*) AS c FROM events"),
        "jobs_total": count("SELECT COUNT(*) AS c FROM job_listings"),
        "topic_follows_total": count("SELECT COUNT(*) AS c FROM topic_follows"),
        "article_versions_total": count("SELECT COUNT(*) AS c FROM article_versions"),
        "ad_campaigns_active": count("SELECT COUNT(*) AS c FROM ad_campaigns WHERE is_active=1"),
        "ad_clicks_total": count("SELECT COUNT(*) AS c FROM ad_events WHERE event_type='click'"),
    }

    recent_activity = db.execute(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT 20"
    ).fetchall()

    db_engine = "PostgreSQL" if USE_POSTGRES else "SQLite"
    db_size_mb = None
    if not USE_POSTGRES:
        try:
            db_size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2)
        except OSError:
            db_size_mb = None

    return render_template(
        "admin/overview.html", active="overview", stats=stats, recent_activity=recent_activity,
        db_engine=db_engine, db_size_mb=db_size_mb,
        redis_connected=_redis_client is not None, s3_connected=_s3_client is not None,
    )


@app.route("/admin/analytics")
@editor_required
def admin_analytics():
    db = get_db()
    article_metrics = db.execute(
        "SELECT a.id,a.title,a.category,a.view_count, "
        "(SELECT COUNT(*) FROM bookmarks b WHERE b.article_id=a.id) AS bookmark_count, "
        "(SELECT COUNT(*) FROM comments c WHERE c.article_id=a.id) AS comment_count "
        "FROM articles a WHERE a.status='published' ORDER BY a.view_count DESC, a.published_at DESC LIMIT 12"
    ).fetchall()
    campaign_metrics = db.execute(
        "SELECT c.*, COUNT(CASE WHEN e.event_type='click' THEN 1 END) AS clicks, "
        "COUNT(CASE WHEN e.event_type='impression' THEN 1 END) AS impressions "
        "FROM ad_campaigns c LEFT JOIN ad_events e ON e.campaign_id=c.id "
        "GROUP BY c.id ORDER BY c.updated_at DESC"
    ).fetchall()
    return render_template("admin/analytics.html", article_metrics=article_metrics, campaign_metrics=campaign_metrics, active="analytics")


@app.route("/admin/members")
@editor_required
def admin_members():
    """가입한 전체 회원(독자) 목록. 편집장만 볼 수 있고, 비밀번호 해시 등 민감정보는 노출하지 않습니다."""
    db = get_db()
    q = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * PAGE_SIZE
    if q:
        like = f"%{_escape_like(q)}%"
        total = db.execute(
            "SELECT COUNT(*) FROM users WHERE role='reader' AND (username LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')",
            (like, like),
        ).fetchone()[0]
        members = db.execute(
            "SELECT u.id, u.username, u.name, u.created_at, "
            "(SELECT COUNT(*) FROM bookmarks WHERE user_id=u.id) AS bookmark_count, "
            "(SELECT COUNT(*) FROM comments WHERE user_id=u.id) AS comment_count "
            "FROM users u WHERE u.role='reader' AND (u.username LIKE ? ESCAPE '\\' OR u.name LIKE ? ESCAPE '\\') "
            "ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
            (like, like, PAGE_SIZE, offset),
        ).fetchall()
    else:
        total = db.execute("SELECT COUNT(*) FROM users WHERE role='reader'").fetchone()[0]
        members = db.execute(
            "SELECT u.id, u.username, u.name, u.created_at, "
            "(SELECT COUNT(*) FROM bookmarks WHERE user_id=u.id) AS bookmark_count, "
            "(SELECT COUNT(*) FROM comments WHERE user_id=u.id) AS comment_count "
            "FROM users u WHERE u.role='reader' ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, offset),
        ).fetchall()
    has_more = offset + len(members) < total
    today = now_kst().strftime("%Y-%m-%d")
    today_count = db.execute(
        "SELECT COUNT(*) FROM users WHERE role='reader' AND created_at LIKE ?", (today + "%",)
    ).fetchone()[0]
    return render_template(
        "admin/members.html", members=members, total=total, page=page, has_more=has_more,
        q=q, today_count=today_count, active="members",
    )


@app.route("/admin/reporters", methods=["GET", "POST"])
@editor_required
def admin_reporters():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:60]
        username = request.form.get("username", "").strip()[:40]
        password = request.form.get("password", "")
        role = request.form.get("role", "journalist")
        if role not in ("journalist", "editor"):
            role = "journalist"
        if not name or not username or len(password) < 8:
            flash("이름, 아이디, 8자 이상의 비밀번호를 입력해 주세요.", "error")
            return redirect(url_for("admin_reporters"))
        if not username.replace("_", "").replace("-", "").isalnum():
            flash("아이디는 영문, 숫자, '-', '_'만 사용할 수 있습니다.", "error")
            return redirect(url_for("admin_reporters"))
        exists = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            flash("이미 사용 중인 아이디입니다.", "error")
        else:
            now = now_kst().strftime("%Y-%m-%d %H:%M")
            db.execute(
                "INSERT INTO users (username,password_hash,name,role,created_at) VALUES (?,?,?,?,?)",
                (username, generate_password_hash(password), name, role, now),
            )
            db.commit()
            flash(f"{name}님의 계정이 생성되었습니다. (아이디: {username})", "msg")
        return redirect(url_for("admin_reporters"))

    reporters = db.execute(
        """SELECT u.*, (SELECT COUNT(*) FROM articles WHERE author_id=u.id) AS article_count
           FROM users u WHERE u.role IN ('journalist','editor') ORDER BY u.created_at ASC"""
    ).fetchall()
    return render_template("admin/reporters.html", reporters=reporters, active="reporters")


@app.route("/admin/reporters/<int:user_id>/reset-password", methods=["POST"])
@editor_required
def admin_reset_password(user_id):
    target = get_db().execute(
        "SELECT * FROM users WHERE id=? AND role IN ('journalist','editor')", (user_id,)
    ).fetchone()
    if not target:
        abort(404)
    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("새 비밀번호는 8자 이상이어야 합니다.", "error")
        return redirect(url_for("admin_reporters"))
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(new_password), user_id),
    )
    db.commit()
    security_logger.info(
        "비밀번호 재설정 — target=%s by=%s ip=%s", target["username"], current_user()["username"], _client_ip()
    )
    flash(f"'{target['username']}' 계정의 비밀번호를 재설정했습니다.", "msg")
    return redirect(url_for("admin_reporters"))


@app.errorhandler(404)
def handle_404(e):
    return render_template(
        "error.html", code=404, heading="페이지를 찾을 수 없습니다",
        message="주소가 잘못됐거나, 삭제·비공개 처리된 기사일 수 있습니다."
    ), 404


@app.errorhandler(400)
def handle_400(e):
    return render_template(
        "error.html", code=400, heading="요청을 처리할 수 없습니다",
        message="페이지를 새로고침한 뒤 다시 시도해 주세요."
    ), 400


@app.errorhandler(403)
def handle_403(e):
    return render_template(
        "error.html", code=403, heading="접근 권한이 없습니다",
        message="이 페이지에 접근할 수 있는 권한이 없습니다."
    ), 403


@app.errorhandler(413)
def handle_413(e):
    return render_template(
        "error.html", code=413, heading="요청이 너무 큽니다",
        message="전송하신 내용이 허용된 용량을 초과했습니다."
    ), 413


@app.errorhandler(500)
def handle_500(e):
    security_logger.info("서버 내부 오류 — path=%s ip=%s", request.path, _client_ip())
    return render_template(
        "error.html", code=500, heading="일시적인 오류가 발생했습니다",
        message="잠시 후 다시 시도해 주세요. 문제가 계속되면 편집국으로 문의해 주세요."
    ), 500


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=DEBUG_MODE)

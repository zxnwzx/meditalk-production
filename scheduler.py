"""Single-process scheduler for reliable production publication of due articles."""
import os
import time

# app.py의 요청 훅은 건너뛰고, 이 프로세스만 예약 발행을 전담합니다.
os.environ.setdefault("SCHEDULE_WORKER_ENABLED", "1")

from app import app, _publish_due_scheduled_articles, security_logger  # noqa: E402

POLL_SECONDS = max(1.0, float(os.environ.get("PUBLISH_POLL_SECONDS", "2")))


def run():
    security_logger.info("예약 발행 워커 시작 — interval=%ss", POLL_SECONDS)
    while True:
        try:
            with app.app_context():
                _publish_due_scheduled_articles()
        except Exception:
            security_logger.exception("예약 발행 워커 오류")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()

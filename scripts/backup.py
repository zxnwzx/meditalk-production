#!/usr/bin/env python3
"""메디톡 DB 자동 백업 스크립트.

Render의 "Cron Job" 서비스(또는 아무 서버의 cron)에서 하루 한 번 이 스크립트를 실행하도록
등록해 두면, 사람이 매번 수동으로 /admin/backup 버튼을 누르지 않아도 자동으로 백업됩니다.

사용법:
    python3 scripts/backup.py

동작:
    - DATABASE_URL이 설정되어 있으면 pg_dump로 PostgreSQL을 덤프합니다.
      (배포 환경에 postgresql-client 패키지가 설치되어 있어야 pg_dump를 쓸 수 있습니다.)
    - 없으면 SQLite 파일을 그대로 복사합니다.
    - S3_BUCKET이 설정되어 있으면 백업 파일을 S3/R2에도 올립니다 (로컬 디스크는 재배포 시 사라지므로).
    - 로컬에도 최근 7개까지만 보관하고 오래된 백업은 자동으로 지웁니다.
"""
import os
import sys
import subprocess
import glob
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP_LOCAL_BACKUPS = 7

KST = timezone(timedelta(hours=9))


def _now_kst_str():
    return datetime.now(KST).strftime("%Y%m%d-%H%M%S")


def _upload_to_s3_if_configured(local_path, filename):
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        return False
    try:
        import boto3
        client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("S3_REGION") or "auto",
        )
        with open(local_path, "rb") as f:
            client.put_object(Bucket=bucket, Key=f"backups/{filename}", Body=f.read())
        print(f"✓ S3(backups/{filename})에 백업 업로드 완료")
        return True
    except Exception as e:
        print(f"⚠️  S3 업로드 실패 (로컬 백업은 유지됩니다): {e}", file=sys.stderr)
        return False


def _cleanup_old_backups():
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "meditok-backup-*")))
    for old_file in files[:-KEEP_LOCAL_BACKUPS]:
        os.remove(old_file)
        print(f"오래된 로컬 백업 삭제: {os.path.basename(old_file)}")


def backup_postgres(database_url, ts):
    filename = f"meditok-backup-{ts}.sql"
    out_path = os.path.join(BACKUP_DIR, filename)
    result = subprocess.run(
        ["pg_dump", database_url, "-f", out_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"❌ pg_dump 실패: {result.stderr}", file=sys.stderr)
        print("   (배포 환경에 postgresql-client가 설치되어 있는지 확인해 주세요.)", file=sys.stderr)
        sys.exit(1)
    print(f"✓ PostgreSQL 덤프 완료: {filename}")
    return out_path, filename


def backup_sqlite(db_path, ts):
    import shutil
    filename = f"meditok-backup-{ts}.db"
    out_path = os.path.join(BACKUP_DIR, filename)
    shutil.copy2(db_path, out_path)
    print(f"✓ SQLite 파일 복사 완료: {filename}")
    return out_path, filename


def main():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _now_kst_str()
    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        out_path, filename = backup_postgres(database_url, ts)
    else:
        db_path = os.environ.get("MEDITOK_DB_PATH") or os.path.join(BASE_DIR, "meditok.db")
        if not os.path.exists(db_path):
            print(f"❌ DB 파일을 찾을 수 없습니다: {db_path}", file=sys.stderr)
            sys.exit(1)
        out_path, filename = backup_sqlite(db_path, ts)

    _upload_to_s3_if_configured(out_path, filename)
    _cleanup_old_backups()
    print("백업 완료.")


if __name__ == "__main__":
    main()

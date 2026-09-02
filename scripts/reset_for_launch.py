#!/usr/bin/env python3
"""
실제 서비스 오픈 직전에 한 번 실행하는 정리 스크립트.

이 스크립트는:
  1. 데모용 예시 기사(한서바이오, 청안제약 등 가상 기업 기사)를 전부 삭제합니다.
  2. 편집장(editor) / 기자(reporter) 데모 계정의 비밀번호를 새로 입력받아 교체합니다.
  3. 뉴스레터 구독자·문의 내역은 그대로 둡니다 (실제 독자 데이터일 수 있으므로).

주의: 기사 삭제는 되돌릴 수 없습니다. 먼저 /admin/backup 에서 DB를 백업해두세요.

실행 방법 (앱과 같은 디렉터리에서):
    python scripts/reset_for_launch.py
"""
import os
import sqlite3
import sys
import getpass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "meditok.db")


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB 파일을 찾을 수 없습니다: {DB_PATH}")
        return

    print("=" * 60)
    print("메디톡 — 실서비스 오픈 준비 스크립트")
    print("=" * 60)
    print(f"대상 DB: {DB_PATH}")
    print()
    print("이 스크립트는 지금 저장된 모든 기사(데모 예시 포함)를 삭제하고,")
    print("편집장·기자 계정의 비밀번호를 새로 설정합니다.")
    print("계속하기 전에 /admin/backup 에서 백업을 받아두셨는지 확인하세요.")
    print()
    confirm = input("계속하려면 정확히 RESET 을 입력하세요: ").strip()
    if confirm != "RESET":
        print("취소되었습니다. 아무 것도 변경하지 않았습니다.")
        return

    db = sqlite3.connect(DB_PATH)

    article_count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    db.execute("DELETE FROM bookmarks")
    db.execute("DELETE FROM articles")
    db.commit()
    print(f"\n✓ 기사 {article_count}건과 관련 스크랩 기록을 삭제했습니다.")

    print("\n--- 계정 비밀번호 재설정 ---")
    for username, label in [("editor", "편집장"), ("reporter", "기자")]:
        row = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            continue
        while True:
            pw = getpass.getpass(f"[{label}] '{username}' 계정의 새 비밀번호 (8자 이상, Enter로 건너뛰기): ")
            if pw == "":
                print(f"  → '{username}' 비밀번호는 변경하지 않았습니다.")
                break
            if len(pw) < 8:
                print("  비밀번호는 8자 이상이어야 합니다.")
                continue
            db.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(pw), row[0]),
            )
            db.commit()
            print(f"  → '{username}' 비밀번호를 변경했습니다.")
            break

    db.close()
    print("\n완료됐습니다. 이제 실제 기사를 작성해 서비스를 시작하세요.")


if __name__ == "__main__":
    main()

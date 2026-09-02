# MEDITALK 프로덕션 배포 안내

이 구성은 Gunicorn 웹 프로세스와 별도의 예약 발행 워커를 하나의 컨테이너에서 함께 실행합니다. 예약 발행 워커는 2초 간격으로 도달 시각을 확인하므로, 방문자가 없어도 예약 기사가 공개됩니다.

## 빠른 실행

```bash
cp .env.production.example .env.production
# .env.production에서 SECRET_KEY를 긴 무작위 값으로 변경
docker compose -f docker-compose.production.yml up -d --build
```

서비스 상태는 `http://localhost:8000/healthz`에서 확인할 수 있습니다. 운영 환경에서는 TLS 종료 프록시를 두고 `SESSION_COOKIE_SECURE=1`을 유지해야 합니다.

## 배포 전 직접 검증

Docker 실행 환경에서는 `docker compose -f docker-compose.production.yml up -d --build` 후 `/healthz`를 확인하세요. 로컬에서는 아래 스크립트가 임시 SQLite DB와 Gunicorn·예약 발행 워커를 기동해 편집장 로그인, 기사 예약, 도달 시각 이후 자동 공개, 공개 지면 렌더링까지 확인합니다.

```bash
./scripts/verify_production.sh
```

검증은 별도 임시 DB와 업로드 폴더에서 실행되므로 운영 데이터에 영향을 주지 않습니다.

## 데이터 보존과 확장

기본 구성은 Docker 볼륨 `/data`에 SQLite DB와 업로드 파일을 보존합니다. SQLite를 쓸 때는 `GUNICORN_WORKERS=1`을 유지하세요. 동시 편집·트래픽이 커지면 `DATABASE_URL`로 PostgreSQL을, `REDIS_URL`로 다중 인스턴스 레이트리밋과 캐시를 연결한 뒤 워커 수를 늘리면 됩니다. 업로드 파일이 컨테이너 재배포 이후에도 남아야 한다면 S3 또는 R2 환경변수를 설정하세요.

## 운영 보안 점검

`SECRET_KEY`는 기본값을 사용할 수 없도록 컨테이너 시작 시 검사합니다. `.env.production`은 이미지와 패키지에서 제외됩니다. 공개 HTTPS 도메인 뒤에서 실행하고, 데이터베이스·Redis·S3 자격 증명은 배포 플랫폼의 비밀값 관리 기능에만 저장하세요.

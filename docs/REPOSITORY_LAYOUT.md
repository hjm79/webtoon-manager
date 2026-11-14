# 통합 저장소와 기존 설치 보존

수집·내 서재·읽기의 소스와 테스트는 `hjm79/webtoon-manager` 한 저장소에서 관리한다. 별도 `hjm79/webtoon-server` 포크는 이력 참조용으로 보관한다. 뷰어는 서브모듈이나 외부 체크아웃이 아니라 `viewer/`에 포함된 일반 파일이다.

| 위치 | 역할 |
| --- | --- |
| `app/`, `tests/`, `Dockerfile.fork` | 수집·검색·구독·관리 화면과 같은 주소의 뷰어 프록시 |
| `viewer/app/`, `viewer/static/`, `viewer/tests/` | 서재·이미지·읽기·이어 읽기·프로필·마이서포터 연동 |
| `viewer/LICENSE`, `viewer/IMPORT.md` | 원저작자 라이선스와 가져온 이력·미커밋 수정의 출처 |
| `compose.integrated.yml` | 한 체크아웃에서 두 구성요소를 빌드하는 단일 배포 구성 |
| `.github/workflows/` | 두 구성요소의 테스트와 이미지 빌드 |

관리기와 뷰어는 둘 다 `app`이라는 Python 패키지를 쓰고 의존성 버전도 다르다. 각자의 작업 디렉토리·Python 환경·실행 프로세스를 유지한다. 브라우저에는 관리기의 한 주소만 공개하고 뷰어는 `/viewer/` 경로로 전달한다. 컨테이너 이름을 유지해 기존 내부 주소도 바꾸지 않는다.

## 빌드와 테스트

```sh
docker build -f Dockerfile.fork -t webtoon-manager:fork .
docker build -f viewer/Dockerfile.fork -t webtoon-manager-viewer:fork viewer
python tests/run_backend.py
npm ci --prefix tests/js
npm test --prefix tests/js
node --test viewer/tests/js/*.cjs
```

뷰어 Python 테스트는 별도 환경에서 실행한다.

```sh
python3 -m venv viewer/.venv
viewer/.venv/bin/python -m pip install -r viewer/requirements-dev.txt
cd viewer
.venv/bin/python -m pytest tests/ -q
```

두 이미지의 기반은 원본 프로젝트의 고정 digest이며 앱 소스는 이 저장소에서 복사한다. 보관된 개인 뷰어 포크의 이미지나 체크아웃에 의존하지 않는다. 원본 프로젝트의 참고용 워크플로는 `viewer/.github/`에도 보존되지만 실제로 실행되는 통합 워크플로는 루트 `.github/workflows/`뿐이다.

## 설치와 업그레이드

`.env.integrated.example`을 `.env.integrated`로 복사하고 실제 경로·공개 주소·기존 연동 토큰을 채운다. 이 파일은 Git과 이미지 빌드에서 제외된다.

```sh
docker compose --env-file .env.integrated -f compose.integrated.yml config --quiet
docker compose --env-file .env.integrated -f compose.integrated.yml up -d --build
```

업그레이드에서는 다음을 그대로 유지한다.

- 관리기의 `/data`와 기존 `webtoons.db`, 암호화 키, 쿠키 폴더
- 뷰어의 `/data`와 기존 `progress.db` 및 프로필·읽음 기록
- `/library/naver`, `/library/kakao`, `/library/archive`의 기존 연결 이름과 파일 경로
- 뷰어 라이브러리의 읽기 전용 마운트, 공개 주소, 인증·연동 토큰
- 호스트의 rclone·GDS 읽기 전용 마운트·50G 캐시 설정

저장소 통합 때문에 운영 데이터 폴더를 옮기거나 이름을 바꿀 필요가 없다. 다운로드 중인 관리기를 강제로 교체하지 않는다. DB·설정·미디어 백업과 검증한 이미지의 롤백 참조를 준비한 뒤 유휴 상태에서 이미지만 교체한다. 롤백 때도 진행된 다운로드와 읽음 기록을 오래된 DB로 덮어쓰지 않는다.

이미 별도 Compose 파일로 운영 중이면 해당 파일의 경로·포트·환경변수·마운트는 유지하고, 여기서 빌드한 두 이미지 참조만 갱신해도 된다. 저장소 통합과 데이터 이전은 별개의 작업이다.

## 가져온 뷰어 이력

뷰어의 기존 Git 이력은 통합 커밋의 부모로 보존한다. 가져온 시점의 추적 파일과 미커밋·미추적 파일은 `viewer/IMPORT_MANIFEST.json`에 SHA-256으로 기록한다. 이 기록은 실행 소스의 출처를 검증하기 위한 것으로 사용자 DB·쿠키·세션·다운로드 파일을 포함하지 않는다.

뷰어의 MIT 라이선스는 해당 구성요소에 유지한다. 이 라이선스를 관리기 전체에 새로 적용하지 않는다.

# MCP Server

Authentik OAuth 인증이 적용된 MCP 서버. 인증된 유저 권한으로 bash 명령 실행 및 파일 I/O를 제공합니다.

## 제공 툴

| 툴 | 설명 |
|---|---|
| `bash` | bash 명령 실행 (rm, mv 등 위험 명령 차단) |
| `read_file` | 텍스트 파일 읽기 (라인 범위, 최대 길이 지정 가능) |
| `write_file` | 새 파일 쓰기 (기존 파일이면 에러) |
| `edit_file` | 기존 파일 수정 (자동 .bak 백업) |
| `append_file` | 파일 끝에 내용 추가 |
| `list_dir` | 디렉토리 목록 조회 (`max_entries=200`) |
| `file_stat` | 파일/디렉토리 메타데이터 조회 |
| `file_delete` | 파일/빈 디렉토리 삭제 (confirmed=True 필요) |
| `file_move` | 파일/디렉토리 이동 및 이름 변경 |
| `mkdir` | 디렉토리 생성 |
| `grep` | 파일에서 regex 패턴 검색 (`max_results=200`, `max_chars=500`) |
| `file_serve` | 파일을 `/dev/shm`에 복사하고 공개 URL 반환 (`max_size_mb=10.0`) |

## 요구사항

- Python 3.12+
- sudo 권한 (서버 초기 설정 시)

## 설치

```bash
cd mcp-server

# 가상환경 생성 (처음 한 번만)
python3 -m venv venv

# 패키지 설치
venv/bin/pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일 열어서 값 채우기
```

## 서버 초기 설정 (처음 한 번만)

```bash
# MCP 허용할 유저 그룹 생성
sudo groupadd mcp-users

# 허용할 유저 추가
sudo usermod -aG mcp-users <username>

# 서비스 실행 유저(예: mcp-server-user)에게 sudo 권한 부여
# 파일 툴이 인증된 유저 권한으로 동작하기 위해 필요
sudo visudo -f /etc/sudoers.d/mcp-server
# 아래 내용 추가:
# mcp-server-user ALL=(%mcp-users) NOPASSWD: /usr/bin/python3, /usr/bin/bash, /usr/bin/mv, /usr/bin/mkdir, /usr/bin/tee
```

## 실행

```bash
venv/bin/python server.py
```

## 인증 방식

두 가지 인증 방식을 동시에 지원합니다. Bearer 토큰을 먼저 확인하고, 없으면 OAuth로 fallback합니다.

### 1. Bearer 토큰 (간단)

`.env`에 `TOKEN_<username>=<token>` 형식으로 추가:

```
TOKEN_inrok=mysecrettoken
TOKEN_alice=anothetoken
```

Claude Desktop 등 MCP 클라이언트에서 해당 토큰으로 접속하면 해당 유저로 실행됩니다.

### 2. Authentik OAuth

Authentik이 구성된 환경에서 사용. 아래 환경변수 필요.

## 환경변수 (.env)

| 키 | 설명 |
|---|---|
| `PORT` | 서버 포트 (기본: 3000) |
| `TOKEN_<username>` | Bearer 토큰 인증용. 예: `TOKEN_inrok=mysecret` |
| `AUTHENTIK_ISSUER` | Authentik OIDC issuer URL |
| `AUTHENTIK_AUTHORIZE_URL` | 인증 엔드포인트 |
| `AUTHENTIK_TOKEN_URL` | 토큰 엔드포인트 |
| `AUTHENTIK_INTROSPECT_URL` | 토큰 검증 엔드포인트 |
| `AUTHENTIK_USERINFO_URL` | 유저 정보 엔드포인트 |
| `CLIENT_ID` | Authentik Provider Client ID |
| `CLIENT_SECRET` | Authentik Provider Client Secret |
| `MCP_GROUP` | 툴 사용 허용 Linux 그룹 (기본: `mcp-users`) |
| `ALLOWED_PATH_ROOTS` | 유저별 접근 허용 루트 경로, 콤마 구분 (기본: `/home`) |
| `SHARED_PATHS` | 모든 유저 공용 접근 경로, 콤마 구분 (기본: 없음) |

파일 접근 권한 예시: `ALLOWED_PATH_ROOTS=/home` 설정 시 유저 `inrok`은 `/home/inrok` 에만 접근 가능. `SHARED_PATHS=/storage/share` 설정 시 해당 경로는 모든 유저 접근 가능.

## Authentik 설정

1. Authentik Admin → **Providers** → Create → **OAuth2/OpenID Provider**
   - Redirect URIs: 클라이언트 콜백 주소
   - Signing Key: 기본값
2. **Applications** → Create → Provider 연결 (slug: `mcp-server`)
3. Application → **Policy Bindings** 에서 허용할 유저/그룹 지정

## 엔드포인트

| 경로 | 설명 |
|---|---|
| `/.well-known/oauth-protected-resource` | MCP 클라이언트가 Authentik 주소 발견 |
| `/.well-known/oauth-authorization-server` | Authentik AS 메타데이터 |
| `/mcp` | MCP 엔드포인트 (Bearer 토큰 필수) |
| `/files/{uuid}` | 파일 서빙 엔드포인트 (인증 없음, `file_serve` 툴로 등록된 파일만) |

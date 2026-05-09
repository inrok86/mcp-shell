"""Wiki.js MCP tools — pluggable module for the toscana MCP server.

환경 변수 (.env):
  WIKI_URL            Wiki.js 서버 주소 (e.g. https://wiki.cbpc.ewha.ac.kr)
  WIKI_API_KEY_FILE   관리자 API 키가 담긴 파일 경로 (e.g. ~/wiki_cbpc.key)
  WIKI_LOCALE         페이지 로케일 (default: en)
  WIKI_HOME_PREFIX    유저 홈 경로 접두사 (default: 없음 → wiki.xyz/{username})
  WIKI_COMMON_PATHS   공용 디렉토리 경로들, 콤마 구분 (e.g. cbpc,public)

권한 모델:
  - 읽기 (get_page, get_tags): 모든 인증 유저 가능
  - 쓰기 (modify_page, upload_asset):
      · 본인 홈: {WIKI_HOME_PREFIX}/{username}/... 또는 {username}/...
      · 공용:   WIKI_COMMON_PATHS 에 나열된 경로들
      · 그 외:  거부
"""

import mimetypes
import os
from pathlib import Path
from contextvars import ContextVar

import httpx


def register_wiki_tools(mcp, current_user: ContextVar[str], resolve_path, sudo_exec) -> bool:
    """Wiki.js 도구를 MCP 인스턴스에 등록한다.

    WIKI_URL / WIKI_API_KEY_FILE 이 설정되지 않으면 등록 없이 False 반환.
    """
    wiki_url = os.getenv("WIKI_URL", "").rstrip("/")
    key_file = os.getenv("WIKI_API_KEY_FILE", "")

    if not wiki_url or not key_file:
        return False

    try:
        api_key = Path(key_file).expanduser().read_text().strip()
    except OSError as e:
        print(f"[wiki_tools] API 키 파일 읽기 실패: {e}")
        return False

    graphql_url = f"{wiki_url}/graphql"
    upload_url = f"{wiki_url}/u"
    locale = os.getenv("WIKI_LOCALE", "en")

    # 스키마/컨벤션 힌트 로드 (선택)
    _schema_hint = ""
    _schema_file = os.getenv("WIKI_SCHEMA_FILE", "")
    if _schema_file:
        try:
            _schema_hint = "\n\n---\n" + Path(_schema_file).expanduser().read_text().strip()
        except OSError as e:
            print(f"[wiki_tools] WIKI_SCHEMA_FILE 읽기 실패: {e}")

    home_prefix = os.getenv("WIKI_HOME_PREFIX", "").strip().strip("/")
    common_paths: list[str] = [
        p.strip().strip("/")
        for p in os.getenv("WIKI_COMMON_PATHS", "").split(",")
        if p.strip()
    ]

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _headers() -> dict:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def _gql(query: str, variables: dict | None = None) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                graphql_url,
                json={"query": query, "variables": variables or {}},
                headers=_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    def _user_home(username: str) -> str:
        return f"{home_prefix}/{username}" if home_prefix else username

    def _can_write(path: str, username: str) -> bool:
        norm = path.strip("/")
        home = _user_home(username)
        if norm == home or norm.startswith(home + "/"):
            return True
        for common in common_paths:
            if norm == common or norm.startswith(common + "/"):
                return True
        return False

    def _write_denied_msg(username: str) -> str:
        home = _user_home(username)
        common_str = ", ".join(f"'{c}'" for c in common_paths) if common_paths else "(없음)"
        return (
            f"쓰기 권한 없음. 허용 경로: 본인 홈 '{home}/...' "
            f"또는 공용 경로 {common_str}"
        )

    async def _fetch_page(path: str) -> dict:
        """내부용 페이지 조회 (tool 등록 없음)."""
        query = """
        query($path: String!, $locale: String!) {
          pages {
            singleByPath(path: $path, locale: $locale) {
              id path title content
              tags { tag }
              createdAt updatedAt
            }
          }
        }
        """
        result = await _gql(query, {"path": path.strip("/"), "locale": locale})
        if "errors" in result:
            return {"error": result["errors"][0]["message"]}
        page = result.get("data", {}).get("pages", {}).get("singleByPath")
        if not page:
            return {"error": f"페이지를 찾을 수 없음: {path}"}
        return {
            "id": page["id"],
            "path": page["path"],
            "title": page["title"],
            "content": page["content"],
            "tags": [t["tag"] for t in (page.get("tags") or [])],
            "createdAt": page["createdAt"],
            "updatedAt": page["updatedAt"],
        }

    async def _create(path: str, title: str, content: str, tags: list[str]) -> dict:
        mutation = """
        mutation(
          $content: String!, $description: String!, $editor: String!,
          $isPrivate: Boolean!, $isPublished: Boolean!, $locale: String!,
          $path: String!, $tags: [String]!, $title: String!
        ) {
          pages {
            create(
              content: $content, description: $description, editor: $editor,
              isPrivate: $isPrivate, isPublished: $isPublished, locale: $locale,
              path: $path, tags: $tags, title: $title
            ) {
              responseResult { succeeded message }
              page { id path title }
            }
          }
        }
        """
        result = await _gql(mutation, {
            "content": content, "description": "", "editor": "markdown",
            "isPrivate": False, "isPublished": True, "locale": locale,
            "path": path, "tags": tags, "title": title,
        })
        if "errors" in result:
            return {"error": result["errors"][0]["message"]}
        r = result["data"]["pages"]["create"]
        if not r["responseResult"]["succeeded"]:
            return {"error": r["responseResult"]["message"]}
        return {"ok": True, "page": r["page"]}

    async def _update(
        page_id: int, path: str, title: str, content: str, tags: list[str]
    ) -> dict:
        mutation = """
        mutation(
          $id: Int!, $content: String!, $description: String!, $editor: String!,
          $isPrivate: Boolean!, $isPublished: Boolean!, $locale: String!,
          $path: String!, $tags: [String]!, $title: String!
        ) {
          pages {
            update(
              id: $id, content: $content, description: $description, editor: $editor,
              isPrivate: $isPrivate, isPublished: $isPublished, locale: $locale,
              path: $path, tags: $tags, title: $title
            ) {
              responseResult { succeeded message }
              page { id path title }
            }
          }
        }
        """
        result = await _gql(mutation, {
            "id": page_id, "content": content, "description": "", "editor": "markdown",
            "isPrivate": False, "isPublished": True, "locale": locale,
            "path": path, "tags": tags, "title": title,
        })
        if "errors" in result:
            return {"error": result["errors"][0]["message"]}
        r = result["data"]["pages"]["update"]
        if not r["responseResult"]["succeeded"]:
            return {"error": r["responseResult"]["message"]}
        return {"ok": True, "page": r["page"]}

    # ── MCP 도구 등록 ─────────────────────────────────────────────────────────

    async def wiki_get_page(path: str, save_to: str | None = None) -> dict:
        """Wiki.js 페이지를 경로로 조회한다. 제목, 마크다운 내용, 태그, 메타데이터를 반환.

        대규모 편집이 필요한 경우 save_to로 로컬 파일에 저장하고,
        편집 후 wiki_modify_page(content_file=...)로 업로드하는 워크플로를 권장한다.

        Args:
            path:    페이지 경로 (e.g. 'cbpc/protocols', 'inrok/notes')
            save_to: 마크다운 내용을 저장할 로컬 파일 경로 (선택, e.g. '/home/inrok/edit.md')
        """
        username = current_user.get()
        result = await _fetch_page(path)
        if "error" in result or save_to is None:
            return result

        try:
            target = resolve_path(save_to, username)
        except PermissionError as e:
            return {**result, "save_error": str(e)}

        script = """
import sys
from pathlib import Path
p = Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(sys.stdin.read(), encoding="utf-8")
print("ok")
"""
        content_to_save = f"# {result['title']}\n\n{result['content']}"
        stdout, stderr, rc = await sudo_exec(
            username, ["python3", "-c", script, str(target)],
            stdin_data=content_to_save.encode()
        )
        if rc != 0:
            return {**result, "save_error": stderr.decode().strip()}
        return {**result, "saved_to": str(target)}

    wiki_get_page.__doc__ += _schema_hint
    mcp.tool()(wiki_get_page)

    async def wiki_modify_page(
        path: str,
        mode: str,
        title: str | None = None,
        content: str | None = None,
        content_file: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Wiki.js 페이지를 생성하거나 수정한다.

        mode 종류:
          'create'      — 새 페이지 생성. title과 content(또는 content_file) 필수.
          'update'      — sed 방식 부분 수정. old_text → new_text 치환 (첫 번째만).
          'full_update' — 전체 내용 대치. content 또는 content_file 필수.
                          페이지가 없으면 생성(title도 필수), 있으면 덮어씀.

        대규모 편집 워크플로:
          1. wiki_get_page(path, save_to='/path/to/edit.md') 로 파일 저장
          2. 파일 편집 (edit_file 등으로)
          3. wiki_modify_page(path, mode='full_update', content_file='/path/to/edit.md')

        쓰기 권한:
          본인 홈 ({home_prefix}/{username}/...) 또는 WIKI_COMMON_PATHS 경로만 허용.

        Args:
            path:         페이지 경로 (e.g. 'inrok/my-note', 'cbpc/meeting-log')
            mode:         'create' | 'update' | 'full_update'
            title:        페이지 제목 (create 필수, full_update는 신규 생성 시 필수)
            content:      마크다운 전체 내용 (content_file과 택일)
            content_file: 마크다운 파일 경로 (content와 택일, e.g. '/home/inrok/edit.md')
            old_text:     찾을 텍스트 (update 필수)
            new_text:     교체할 텍스트 (update 필수)
            tags:         태그 목록 (optional; 미지정 시 기존 태그 유지)
        """
        username = current_user.get()
        norm_path = path.strip("/")

        # content_file 우선 처리
        if content_file is not None:
            try:
                cf_path = resolve_path(content_file, username)
            except PermissionError as e:
                return {"error": str(e)}
            script = """
import sys
from pathlib import Path
sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())
"""
            stdout, stderr, rc = await sudo_exec(
                username, ["python3", "-c", script, str(cf_path)]
            )
            if rc != 0:
                return {"error": f"content_file 읽기 실패: {stderr.decode().strip()}"}
            content = stdout.decode("utf-8")

        if not _can_write(norm_path, username):
            return {"error": _write_denied_msg(username)}

        existing = await _fetch_page(norm_path)
        page_exists = "error" not in existing

        if mode == "create":
            if not title or content is None:
                return {"error": "mode='create' 는 title과 content가 필요합니다."}
            if page_exists:
                return {"error": f"이미 존재하는 페이지: '{norm_path}'. 덮어쓰려면 mode='full_update' 사용."}
            result = await _create(norm_path, title, content, tags or [])
            if "error" in result:
                return result
            return {"ok": True, "mode": "create", "page": result["page"]}

        elif mode == "update":
            if old_text is None or new_text is None:
                return {"error": "mode='update' 는 old_text와 new_text가 필요합니다."}
            if not page_exists:
                return {"error": f"페이지를 찾을 수 없음: '{norm_path}'. 생성하려면 mode='create' 사용."}
            current_content: str = existing["content"]
            if old_text not in current_content:
                return {"error": f"old_text를 페이지에서 찾을 수 없습니다: {repr(old_text[:100])}"}
            new_content = current_content.replace(old_text, new_text, 1)
            result = await _update(
                existing["id"], norm_path,
                title or existing["title"],
                new_content,
                tags if tags is not None else existing["tags"],
            )
            if "error" in result:
                return result
            return {"ok": True, "mode": "update", "page": result["page"]}

        elif mode == "full_update":
            if content is None:
                return {"error": "mode='full_update' 는 content가 필요합니다."}
            if page_exists:
                result = await _update(
                    existing["id"], norm_path,
                    title or existing["title"],
                    content,
                    tags if tags is not None else existing["tags"],
                )
                if "error" in result:
                    return result
                return {"ok": True, "mode": "full_update", "action": "updated", "page": result["page"]}
            else:
                if not title:
                    return {"error": "신규 페이지 생성 시 title이 필요합니다."}
                result = await _create(norm_path, title, content, tags or [])
                if "error" in result:
                    return result
                return {"ok": True, "mode": "full_update", "action": "created", "page": result["page"]}

        else:
            return {"error": f"알 수 없는 mode: {mode!r}. 'create' | 'update' | 'full_update' 중 선택."}

    wiki_modify_page.__doc__ += _schema_hint
    mcp.tool()(wiki_modify_page)

    @mcp.tool()
    async def wiki_get_tags() -> dict:
        """Wiki.js 전체 페이지에서 사용 중인 태그 목록을 반환한다.

        태그 중복 방지를 위해 페이지 저장 전 이 도구로 기존 태그를 확인하고
        가장 유사한 태그를 재사용하는 것을 권장한다.
        """
        query = """
        query {
          pages {
            list(orderBy: TITLE) {
              tags
            }
          }
        }
        """
        result = await _gql(query)
        if "errors" in result:
            return {"error": result["errors"][0]["message"]}
        pages = result.get("data", {}).get("pages", {}).get("list", [])
        # 모든 페이지의 태그를 모아 중복 제거, 정렬
        tag_set: dict[str, int] = {}
        for page in pages:
            for tag in (page.get("tags") or []):
                tag_set[tag] = tag_set.get(tag, 0) + 1
        tags_sorted = sorted(tag_set.items(), key=lambda x: (-x[1], x[0]))
        return {
            "tags": [{"tag": t, "usage_count": c} for t, c in tags_sorted],
            "count": len(tag_set),
        }

    @mcp.tool()
    async def wiki_upload_asset(
        file_path: str,
        target_path: str,
        filename: str | None = None,
    ) -> dict:
        """서버의 파일을 Wiki.js 에셋으로 업로드한다.

        시뮬레이션 결과 이미지, 생성된 그래프 등 서버에 있는 파일을 직접 위키에 올릴 때 사용.
        파일 접근 권한(resolve_path)과 위키 쓰기 권한(_can_write)을 모두 확인한다.

        Args:
            file_path:   업로드할 파일의 서버 절대 경로 (e.g. '/home/inrok/results/plot.png')
            target_path: 위키 쓰기 권한 확인용 경로 (e.g. 'inrok/images', 'cbpc/assets')
            filename:    위키에 저장될 파일명 (미지정 시 원본 파일명 사용)
        """
        username = current_user.get()

        # 파일시스템 접근 권한 확인
        try:
            resolved = resolve_path(file_path, username)
        except PermissionError as e:
            return {"error": str(e)}

        # 위키 쓰기 권한 확인
        if not _can_write(target_path.strip("/"), username):
            return {"error": _write_denied_msg(username)}

        # 파일 읽기 (유저 권한으로, python3 사용)
        script = """
import sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    sys.stdout.buffer.write(p.read_bytes())
except Exception as e:
    sys.stderr.write(str(e))
    sys.exit(1)
"""
        stdout, stderr, rc = await sudo_exec(username, ["python3", "-c", script, str(resolved)])
        if rc != 0:
            return {"error": f"파일 읽기 실패: {stderr.decode().strip()}"}

        file_bytes = stdout
        upload_filename = filename or resolved.name
        mime_type, _ = mimetypes.guess_type(upload_filename)
        mime_type = mime_type or "application/octet-stream"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                upload_url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"mediaUpload": (upload_filename, file_bytes, mime_type)},
                data={"mediaUpload": '{"folderId":null}'},
                timeout=60,
            )

        if resp.status_code not in (200, 201):
            return {"error": f"업로드 실패: HTTP {resp.status_code} — {resp.text[:300]}"}

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:500]}

        return {"ok": True, "filename": upload_filename, "mime": mime_type, "response": data}

    return True

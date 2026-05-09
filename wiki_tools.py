"""Wiki.js MCP tools — pluggable module for the toscana MCP server.

Environment variables (.env):
  WIKI_URL            Wiki.js server base URL (e.g. https://wiki.cbpc.ewha.ac.kr)
  WIKI_API_KEY_FILE   Path to file containing the admin API key (e.g. ~/wiki_cbpc.key)
  WIKI_LOCALE         Page locale (default: en)
  WIKI_HOME_PREFIX    Prefix for user home paths (default: none → wiki.xyz/{username})
  WIKI_COMMON_PATHS   Comma-separated shared paths writable by all authenticated users
  WIKI_SCHEMA_FILE    Path to a markdown file with wiki conventions (injected into tool docstrings)

Permission model:
  - Read  (get_page, get_tags): all authenticated users
  - Write (modify_page, upload_asset):
      · User home: {WIKI_HOME_PREFIX}/{username}/... or {username}/...
      · Common:    paths listed in WIKI_COMMON_PATHS
      · Otherwise: denied
"""

import mimetypes
import os
from pathlib import Path
from contextvars import ContextVar

import httpx


def register_wiki_tools(mcp, current_user: ContextVar[str], resolve_path, sudo_exec) -> bool:
    """Register Wiki.js tools with the MCP instance.

    Returns False without registering if WIKI_URL or WIKI_API_KEY_FILE is not set.
    """
    wiki_url = os.getenv("WIKI_URL", "").rstrip("/")
    key_file = os.getenv("WIKI_API_KEY_FILE", "")

    if not wiki_url or not key_file:
        return False

    try:
        api_key = Path(key_file).expanduser().read_text().strip()
    except OSError as e:
        print(f"[wiki_tools] Failed to read API key file: {e}")
        return False

    graphql_url = f"{wiki_url}/graphql"
    upload_url = f"{wiki_url}/u"
    locale = os.getenv("WIKI_LOCALE", "en")

    # Load schema/convention hint (optional)
    _schema_hint = ""
    _schema_file = os.getenv("WIKI_SCHEMA_FILE", "")
    if _schema_file:
        try:
            _schema_hint = "\n\n---\n" + Path(_schema_file).expanduser().read_text().strip()
        except OSError as e:
            print(f"[wiki_tools] Failed to read WIKI_SCHEMA_FILE: {e}")

    home_prefix = os.getenv("WIKI_HOME_PREFIX", "").strip().strip("/")
    common_paths: list[str] = [
        p.strip().strip("/")
        for p in os.getenv("WIKI_COMMON_PATHS", "").split(",")
        if p.strip()
    ]

    # ── Internal helpers ──────────────────────────────────────────────────────

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
        common_str = ", ".join(f"'{c}'" for c in common_paths) if common_paths else "(none)"
        return (
            f"Write access denied. Allowed: user home '{home}/...' "
            f"or common paths {common_str}"
        )

    async def _fetch_page(path: str) -> dict:
        """Internal page fetch (not registered as a tool)."""
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
            return {"error": f"Page not found: {path}"}
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

    # ── MCP tool registration ─────────────────────────────────────────────────

    async def wiki_get_page(path: str, save_to: str | None = None) -> dict:
        """Retrieve a Wiki.js page by its path. Returns title, markdown content, tags, and metadata.

        For large edits, use save_to to write the content to a local file,
        edit it, then upload with wiki_modify_page(content_file=...).

        Args:
            path:    Page path (e.g. 'cbpc/protocols', 'inrok/notes')
            save_to: Local file path to save the markdown content (optional, e.g. '/home/inrok/edit.md')
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
        """Create or modify a Wiki.js page.

        Modes:
          'create'      — Create a new page. Requires title and content (or content_file).
                          Fails if the page already exists.
          'update'      — Sed-style partial edit. Replaces the first occurrence of old_text
                          with new_text. Requires old_text and new_text.
          'full_update' — Replace the entire content. Requires content or content_file.
                          Creates the page if it does not exist (title also required);
                          overwrites if it does.

        Large-edit workflow:
          1. wiki_get_page(path, save_to='/path/to/edit.md')
          2. Edit the file (e.g. with edit_file)
          3. wiki_modify_page(path, mode='full_update', content_file='/path/to/edit.md')

        Write access is limited to:
          User home ({home_prefix}/{username}/...) or paths in WIKI_COMMON_PATHS.

        Args:
            path:         Page path (e.g. 'inrok/my-note', 'cbpc/meeting-log')
            mode:         'create' | 'update' | 'full_update'
            title:        Page title (required for create; required for full_update on new pages)
            content:      Full markdown content (mutually exclusive with content_file)
            content_file: Path to a local markdown file to read content from
            old_text:     Text to find and replace (required for update)
            new_text:     Replacement text (required for update)
            tags:         List of tags (optional; existing tags kept if omitted)
        """
        username = current_user.get()
        norm_path = path.strip("/")

        # content_file takes priority over inline content
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
                return {"error": f"Failed to read content_file: {stderr.decode().strip()}"}
            content = stdout.decode("utf-8")

        if not _can_write(norm_path, username):
            return {"error": _write_denied_msg(username)}

        existing = await _fetch_page(norm_path)
        page_exists = "error" not in existing

        if mode == "create":
            if not title or content is None:
                return {"error": "mode='create' requires title and content."}
            if page_exists:
                return {"error": f"Page already exists: '{norm_path}'. Use mode='full_update' to overwrite."}
            result = await _create(norm_path, title, content, tags or [])
            if "error" in result:
                return result
            return {"ok": True, "mode": "create", "page": result["page"]}

        elif mode == "update":
            if old_text is None or new_text is None:
                return {"error": "mode='update' requires old_text and new_text."}
            if not page_exists:
                return {"error": f"Page not found: '{norm_path}'. Use mode='create' to create it."}
            current_content: str = existing["content"]
            if old_text not in current_content:
                return {"error": f"old_text not found in page content: {repr(old_text[:100])}"}
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
                return {"error": "mode='full_update' requires content or content_file."}
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
                    return {"error": "title is required when creating a new page."}
                result = await _create(norm_path, title, content, tags or [])
                if "error" in result:
                    return result
                return {"ok": True, "mode": "full_update", "action": "created", "page": result["page"]}

        else:
            return {"error": f"Unknown mode: {mode!r}. Choose from 'create' | 'update' | 'full_update'."}

    wiki_modify_page.__doc__ += _schema_hint
    mcp.tool()(wiki_modify_page)

    @mcp.tool()
    async def wiki_get_tags() -> dict:
        """List all tags used across Wiki.js pages, with usage counts.

        Call this before saving a page to reuse existing tags and avoid duplicates.
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
        # Collect and deduplicate tags across all pages
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
        """Upload a server-side file to Wiki.js as an asset.

        Intended for uploading simulation results, generated plots, and similar files
        directly from the server to the wiki. Checks both filesystem access (resolve_path)
        and wiki write permission (_can_write).

        Args:
            file_path:   Absolute server path of the file to upload (e.g. '/home/inrok/results/plot.png')
            target_path: Wiki path used for write permission check (e.g. 'inrok/images', 'cbpc/assets')
            filename:    Filename to use in the wiki (defaults to the original filename)
        """
        username = current_user.get()

        # Check filesystem access
        try:
            resolved = resolve_path(file_path, username)
        except PermissionError as e:
            return {"error": str(e)}

        # Check wiki write permission
        if not _can_write(target_path.strip("/"), username):
            return {"error": _write_denied_msg(username)}

        # Read file as the authenticated user
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
            return {"error": f"Failed to read file: {stderr.decode().strip()}"}

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
            return {"error": f"Upload failed: HTTP {resp.status_code} — {resp.text[:300]}"}

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:500]}

        return {"ok": True, "filename": upload_filename, "mime": mime_type, "response": data}

    return True

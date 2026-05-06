import asyncio
import json
import os
import pwd
import re
import subprocess
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from oauth import get_user

load_dotenv()

PORT = int(os.getenv("PORT", "3000"))
ISSUER = os.environ["AUTHENTIK_ISSUER"]
AUTHORIZE_URL = os.environ["AUTHENTIK_AUTHORIZE_URL"]
TOKEN_URL = os.environ["AUTHENTIK_TOKEN_URL"]
MCP_GROUP = os.getenv("MCP_GROUP", "mcp-users")

ALLOWED_PATH_ROOTS: list[Path] = [
    Path(p) for p in os.getenv("ALLOWED_PATH_ROOTS", "/home").split(",") if p.strip()
]
SHARED_PATHS: list[Path] = [
    Path(p) for p in os.getenv("SHARED_PATHS", "").split(",") if p.strip()
]

# Bearer 토큰 → 유저명 매핑 (.env의 TOKEN_<username>=<token> 항목)
TOKEN_MAP: dict[str, str] = {
    value: key.removeprefix("TOKEN_").lower()
    for key, value in os.environ.items()
    if key.startswith("TOKEN_")
}

current_user: ContextVar[str] = ContextVar("current_user")

# ── Path resolution ───────────────────────────────────────────────────────────

def get_allowed_paths(username: str) -> list[Path]:
    user_paths = [root / username for root in ALLOWED_PATH_ROOTS]
    return user_paths + SHARED_PATHS


def resolve_path(path: str, username: str) -> Path:
    target = Path(path).resolve()
    allowed = get_allowed_paths(username)
    for base in allowed:
        base = base.resolve()
        if target == base or target.is_relative_to(base):
            return target
    allowed_str = ", ".join(str(p) for p in allowed)
    raise PermissionError(f"Path '{path}' is outside allowed directories: {allowed_str}")


# ── sudo helper ───────────────────────────────────────────────────────────────

def _get_cmd(username: str, cmd: list[str]) -> list[str]:
    """Wrap cmd with sudo -u username if needed."""
    process_user = pwd.getpwuid(os.getuid()).pw_name
    return cmd if username == process_user else ["sudo", "-u", username] + cmd


async def _sudo_exec(
    username: str,
    cmd: list[str],
    stdin_data: bytes | None = None,
    timeout: int = 30,
) -> tuple[bytes, bytes, int]:
    """Run a command as username, return (stdout, stderr, returncode)."""
    full_cmd = _get_cmd(username, cmd)
    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=stdin_data), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return b"", b"timed out", -1
    return stdout, stderr, proc.returncode


async def _sudo_python(username: str, script: str, *args: str) -> dict:
    """Run a Python snippet as username with args, return parsed JSON dict."""
    stdout, stderr, rc = await _sudo_exec(username, ["python3", "-c", script, *args])
    if rc != 0:
        return {"error": stderr.decode().strip() or f"exit code {rc}"}
    try:
        return json.loads(stdout)
    except Exception:
        return {"error": stdout.decode().strip() or stderr.decode().strip()}


# ── MCP ──────────────────────────────────────────────────────────────────────

PUBLIC_HOST = os.getenv("PUBLIC_URL", "").removeprefix("https://").removeprefix("http://")

mcp = FastMCP("mcp-server")
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["localhost", f"localhost:{PORT}", PUBLIC_HOST],
    allowed_origins=["https://claude.ai", os.getenv("PUBLIC_URL", "")],
)

# ── Tools: bash ───────────────────────────────────────────────────────────────

FORBIDDEN_COMMANDS = {"rm", "rmdir", "mv", "chmod", "chown", "dd", "mkfs", "fdisk"}


@mcp.tool()
async def bash(command: str, timeout: int = 30) -> str:
    """Run a bash command as the authenticated user in their home directory.

    The following commands are forbidden and must never be used:
    rm, rmdir, mv, chmod, chown, dd, mkfs, fdisk, curl|bash, wget|bash.
    Use file_delete and file_move tools instead of rm/mv.

    Args:
        command: The bash command to execute
        timeout: Maximum execution time in seconds (default: 30)
    """
    username = current_user.get()

    first_token = command.strip().lstrip("sudo ").split()[0] if command.strip() else ""
    if first_token in FORBIDDEN_COMMANDS:
        return f"Error: command '{first_token}' is not allowed. Use the dedicated file tools instead."
    if re.search(r"(curl|wget).*(\||>).*\bsh\b", command):
        return "Error: piping remote content into a shell is not allowed."

    try:
        groups = subprocess.check_output(["id", "-nG", username], text=True).split()
    except subprocess.CalledProcessError:
        return f"Error: unknown user '{username}'"
    if MCP_GROUP not in groups:
        return f"Error: '{username}' is not in group '{MCP_GROUP}'"

    home = pwd.getpwnam(username).pw_dir
    cmd = _get_cmd(username, ["bash", "-c", command])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=home,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "Error: command timed out"

    return stdout.decode() or stderr.decode() or "(no output)"


# ── Tools: file ───────────────────────────────────────────────────────────────

@mcp.tool()
async def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int = 5000,
) -> dict:
    """Read the contents of a text file.

    Returns up to max_chars characters. If truncated, use start_line/end_line
    to read specific sections. Binary files are not supported.

    Args:
        path: Absolute path to the file
        start_line: First line to read, 1-indexed (default: beginning of file)
        end_line: Last line to read, inclusive (default: end of file)
        max_chars: Maximum characters to return (default: 5000)
    """
    username = current_user.get()
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    script = """
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
start = int(sys.argv[2]) if sys.argv[2] != "None" else None
end = int(sys.argv[3]) if sys.argv[3] != "None" else None
max_chars = int(sys.argv[4])
try:
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        print(json.dumps({"error": f"Cannot read binary file: {p.name}"})); sys.exit(0)
    lines = text.splitlines(keepends=True)
    total = len(lines)
    s = (start - 1) if start else 0
    e = end if end else total
    s = max(0, min(s, total)); e = max(s, min(e, total))
    selected = "".join(lines[s:e])
    truncated = len(selected) > max_chars
    content = selected[:max_chars] if truncated else selected
    result = {"content": content, "path": str(p), "total_lines": total, "returned_lines": f"{s+1}-{e}", "truncated": truncated}
    if truncated:
        result["message"] = f"Output truncated at {max_chars} chars. Use start_line/end_line to read specific sections."
    print(json.dumps(result))
except Exception as ex:
    print(json.dumps({"error": str(ex)}))
"""
    return await _sudo_python(username, script, str(target), str(start_line), str(end_line), str(max_chars))


@mcp.tool()
async def write_file(path: str, content: str) -> dict:
    """Write content to a NEW file. Fails if the file already exists.

    Do NOT use this to modify existing files — use edit_file instead.

    Args:
        path: Absolute path to the new file
        content: Full content to write
    """
    username = current_user.get()
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    script = """
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if p.exists():
    print(json.dumps({"error": f"File already exists: {p}. Use edit_file to modify existing files."})); sys.exit(0)
try:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sys.stdin.read(), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(p)}))
except Exception as ex:
    print(json.dumps({"error": str(ex)}))
"""
    stdout, stderr, rc = await _sudo_exec(username, ["python3", "-c", script, str(target)], stdin_data=content.encode())
    if rc != 0:
        return {"error": stderr.decode().strip()}
    try:
        return json.loads(stdout)
    except Exception:
        return {"error": stdout.decode().strip()}


@mcp.tool()
async def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
    """Edit an existing file by replacing an exact string with a new string.

    Automatically creates a backup at <filename>.bak before modifying.
    Only the targeted section is changed — the rest of the file is untouched.

    Args:
        path: Absolute path to the file
        old_string: Exact string to find and replace
        new_string: String to replace it with
        replace_all: Replace all occurrences (default: False, replace only first)
    """
    username = current_user.get()
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    payload = json.dumps({"old": old_string, "new": new_string, "replace_all": replace_all})
    script = """
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
args = json.loads(sys.stdin.read())
try:
    original = p.read_text(encoding="utf-8")
    if args["old"] not in original:
        print(json.dumps({"error": f"String not found in file: {repr(args['old'])}"})); sys.exit(0)
    backup = p.with_suffix(p.suffix + ".bak")
    backup.write_text(original, encoding="utf-8")
    count = original.count(args["old"])
    if args["replace_all"]:
        updated = original.replace(args["old"], args["new"])
        replaced = count
    else:
        updated = original.replace(args["old"], args["new"], 1)
        replaced = 1
    p.write_text(updated, encoding="utf-8")
    print(json.dumps({"ok": True, "replaced": replaced, "path": str(p), "backup": str(backup)}))
except Exception as ex:
    print(json.dumps({"error": str(ex)}))
"""
    stdout, stderr, rc = await _sudo_exec(username, ["python3", "-c", script, str(target)], stdin_data=payload.encode())
    if rc != 0:
        return {"error": stderr.decode().strip()}
    try:
        return json.loads(stdout)
    except Exception:
        return {"error": stdout.decode().strip()}


@mcp.tool()
async def append_file(path: str, content: str) -> dict:
    """Append content to the end of a file without overwriting existing content.

    Args:
        path: Absolute path to the file
        content: Content to append
    """
    username = current_user.get()
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    stdout, stderr, rc = await _sudo_exec(
        username, ["tee", "-a", str(target)],
        stdin_data=content.encode()
    )
    if rc != 0:
        return {"error": stderr.decode().strip()}
    return {"ok": True, "path": str(target)}


@mcp.tool()
async def list_dir(path: str) -> dict:
    """List the contents of a directory (one level deep).

    Args:
        path: Absolute path to the directory
    """
    username = current_user.get()
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    script = """
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    entries = []
    for e in sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name)):
        s = e.stat()
        entries.append({"name": e.name, "type": "file" if e.is_file() else "dir", "size": s.st_size if e.is_file() else None})
    print(json.dumps({"path": str(p), "entries": entries}))
except Exception as ex:
    print(json.dumps({"error": str(ex)}))
"""
    return await _sudo_python(username, script, str(target))


@mcp.tool()
async def file_stat(path: str) -> dict:
    """Get metadata for a file or directory (size, type, timestamps).

    Args:
        path: Absolute path to the file or directory
    """
    username = current_user.get()
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    script = """
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    s = p.stat()
    print(json.dumps({"path": str(p), "type": "file" if p.is_file() else "dir", "size": s.st_size, "modified": s.st_mtime, "created": s.st_ctime}))
except Exception as ex:
    print(json.dumps({"error": str(ex)}))
"""
    return await _sudo_python(username, script, str(target))


@mcp.tool()
async def file_delete(path: str, confirmed: bool = False) -> dict:
    """Delete a single file or an empty directory.

    This operation is irreversible. Always ask the user for explicit confirmation
    before calling this tool. Pass confirmed=True only after the user has agreed.

    Args:
        path: Absolute path to delete
        confirmed: Must be True to proceed. If False, returns a confirmation prompt.
    """
    username = current_user.get()
    if not confirmed:
        return {"error": f"Confirmation required. Ask the user to confirm deletion of '{path}' before proceeding."}
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    script = """
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    if p.is_dir():
        p.rmdir()
    else:
        p.unlink()
    print(json.dumps({"ok": True, "path": str(p)}))
except Exception as ex:
    print(json.dumps({"error": str(ex)}))
"""
    return await _sudo_python(username, script, str(target))


@mcp.tool()
async def file_move(src: str, dst: str) -> dict:
    """Move or rename a file or directory.

    Args:
        src: Absolute source path
        dst: Absolute destination path
    """
    username = current_user.get()
    try:
        src_path = resolve_path(src, username)
        dst_path = resolve_path(dst, username)
    except PermissionError as e:
        return {"error": str(e)}

    stdout, stderr, rc = await _sudo_exec(username, ["mv", str(src_path), str(dst_path)])
    if rc != 0:
        return {"error": stderr.decode().strip()}
    return {"ok": True, "src": str(src_path), "dst": str(dst_path)}


@mcp.tool()
async def mkdir(path: str) -> dict:
    """Create a directory and any missing parent directories.

    Args:
        path: Absolute path of the directory to create
    """
    username = current_user.get()
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    stdout, stderr, rc = await _sudo_exec(username, ["mkdir", "-p", str(target)])
    if rc != 0:
        return {"error": stderr.decode().strip()}
    return {"ok": True, "path": str(target)}


@mcp.tool()
async def grep(
    pattern: str,
    path: str,
    recursive: bool = True,
    ignore_case: bool = False,
    include: str | None = None,
    max_results: int = 200,
) -> dict:
    """Search for a regex pattern across files and return matching lines with line numbers.

    Args:
        pattern: Regex pattern to search for
        path: Absolute path to file or directory to search
        recursive: Search recursively in subdirectories (default: True)
        ignore_case: Case-insensitive search (default: False)
        include: Glob pattern to filter files, e.g. '*.py' (optional)
        max_results: Maximum number of matching lines to return (default: 200)
    """
    username = current_user.get()
    try:
        target = resolve_path(path, username)
    except PermissionError as e:
        return {"error": str(e)}

    payload = json.dumps({"pattern": pattern, "recursive": recursive, "ignore_case": ignore_case, "include": include, "max_results": max_results})
    script = """
import json, re, sys
from pathlib import Path
args = json.loads(sys.stdin.read())
target = Path(sys.argv[1])
flags = re.IGNORECASE if args["ignore_case"] else 0
try:
    compiled = re.compile(args["pattern"], flags)
except re.error as e:
    print(json.dumps({"error": f"Invalid regex: {e}"})); sys.exit(0)
results = []
truncated = False
def search_file(f):
    global truncated
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines, 1):
            if len(results) >= args["max_results"]:
                truncated = True; return
            if compiled.search(line):
                results.append({"file": str(f), "line": i, "content": line})
    except Exception:
        pass
if target.is_file():
    search_file(target)
elif args["recursive"]:
    g = f'**/{args["include"]}' if args["include"] else "**/*"
    for f in sorted(target.glob(g)):
        if f.is_file() and not truncated: search_file(f)
else:
    g = args["include"] or "*"
    for f in sorted(target.glob(g)):
        if f.is_file() and not truncated: search_file(f)
print(json.dumps({"matches": results, "count": len(results), "truncated": truncated}))
"""
    stdout, stderr, rc = await _sudo_exec(username, ["python3", "-c", script, str(target)], stdin_data=payload.encode())
    if rc != 0:
        return {"error": stderr.decode().strip()}
    try:
        return json.loads(stdout)
    except Exception:
        return {"error": stdout.decode().strip()}


# ── Middleware ────────────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            base = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer realm="mcp-server", resource_metadata_uri="{base}/.well-known/oauth-protected-resource"'},
            )

        token = auth.removeprefix("Bearer ")

        # 1. Bearer 토큰 맵에서 먼저 확인
        if token in TOKEN_MAP:
            current_user.set(TOKEN_MAP[token])
            return await call_next(request)

        # 2. OAuth introspect 시도
        user = await get_user(token)
        if not user:
            return JSONResponse({"error": "invalid_token"}, status_code=401)

        current_user.set(user["preferred_username"])
        return await call_next(request)


# ── Routes ───────────────────────────────────────────────────────────────────

async def protected_resource(request: Request):
    base = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
    return JSONResponse({
        "resource": base,
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    })


async def auth_server_meta(request: Request):
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZE_URL,
        "token_endpoint": TOKEN_URL,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
    })


mcp_http_app = mcp.streamable_http_app()

@asynccontextmanager
async def lifespan(app):
    async with mcp_http_app.router.lifespan_context(app):
        yield

app = Starlette(
    routes=[
        Route("/.well-known/oauth-protected-resource", protected_resource),
        Route("/.well-known/oauth-authorization-server", auth_server_meta),
        Mount("/", app=mcp_http_app),
    ],
    middleware=[Middleware(AuthMiddleware)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

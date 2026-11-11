"""Tests for the setup bundle: building on the PC, installing and applying on the server."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cli_subagents.remote import bundle as b

SECRET = "secret-login-value-123"


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def write_json(path: Path, value) -> Path:
    return write(path, json.dumps(value))


def read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def crafted_zip(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format_version": b.FORMAT_VERSION}))
        archive.writestr("settings.json", "{}")
        archive.writestr("mcp.json", '{"mcpServers": {}}')
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def small_config(root: Path, claude_md: str, skills: dict[str, str] | None = None,
                 agents: dict[str, str] | None = None) -> Path:
    config = root / "config"
    if config.exists():
        import shutil

        shutil.rmtree(config)
    config.mkdir(parents=True)
    if claude_md:
        write(config / "CLAUDE.md", claude_md)
    for name, text in (skills or {}).items():
        write(config / "skills" / name / "SKILL.md", text)
    for name, text in (agents or {}).items():
        write(config / "agents" / name, text)
    return config


class FullSetup:
    """A fake Claude config folder with every kind of content the bundle has to handle."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "home" / ".claude"
        self.state = root / "home" / ".claude.json"
        self.cwd = str(root / "work" / "app")
        c = self.config

        write(c / "skills" / "alpha" / "SKILL.md", "alpha skill\n")
        write(c / "skills" / "alpha" / "scripts" / "run.py", "print('hi')\n")
        write(c / "skills" / "alpha" / "__pycache__" / "run.cpython-312.pyc", b"\0\1")
        write(c / "skills" / "alpha" / "node_modules" / "pkg" / "index.js", "x")
        write(c / "skills" / "alpha" / ".git" / "config", "[core]")
        write(c / "skills" / "alpha" / ".env", f"TOKEN={SECRET}")
        write(c / "skills" / "alpha" / "server.pem", SECRET)
        write(c / "skills" / "alpha" / "big.bin", b"\0" * (b.MAX_FILE_BYTES + 1))
        write(c / "skills" / "beta" / "SKILL.md", "beta skill\n")
        write(c / "skills" / "synced" / "remote" / "SKILL.md", "account owned\n")
        write(c / "agents" / "reviewer.md", "reviewer\n")
        write(c / "agents" / "team" / "tester.md", "tester\n")
        write(c / "commands" / "deploy.md", "deploy\n")
        write(c / "rules" / "style.md", "style\n")
        write(c / "output-styles" / "terse.md", "terse\n")
        write(c / "workflows" / "release.md", "release\n")
        write(c / "CLAUDE.md", b"line one\r\nline two\r\n")

        write_json(c / ".credentials.json", {"claudeAiOauth": {"accessToken": SECRET}})
        write_json(c / ".claude.json", {"primaryApiKey": SECRET})
        write(c / "projects" / "app" / "session.jsonl", SECRET)
        write(c / "sessions" / "s.json", SECRET)
        write(c / "todos" / "t.json", SECRET)
        write(c / "history.jsonl", SECRET)

        self.plugin_dir = c / "plugins" / "cache" / "market" / "tool" / "1.0.0"
        write_json(self.plugin_dir / ".claude-plugin" / "plugin.json", {"name": "tool"})
        write(self.plugin_dir / "skills" / "plug" / "SKILL.md", "plugin skill\n")
        write(self.plugin_dir / "node_modules" / "dep" / "index.js", "x")
        write_json(c / "plugins" / "installed_plugins.json", {
            "version": 2,
            "plugins": {
                "tool@market": [{"scope": "user", "installPath": str(self.plugin_dir)}],
                "off@market": [{"scope": "user", "installPath": str(self.plugin_dir)}],
                "gone@market": [{"scope": "user", "installPath": str(root / "missing")}],
            },
        })

        write_json(c / "settings.json", {
            "model": "opus",
            "apiKeyHelper": "/usr/local/bin/get-key",
            "forceLoginMethod": "claudeai",
            "forceLoginOrgUUID": "org",
            "otelHeadersHelper": "/bin/headers",
            "awsAuthRefresh": "aws sso login",
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo"}]}]},
            "statusLine": {"type": "command", "command": "status.sh"},
            "enabledPlugins": {"tool@market": True, "off@market": False, "gone@market": True},
            "extraKnownMarketplaces": {"market": {"source": {"source": "github", "repo": "x/y"}}},
            "permissions": {"allow": ["Bash(ls:*)"], "additionalDirectories": ["C:\\Work"]},
            "env": {
                "ANTHROPIC_API_KEY": SECRET,
                "ANTHROPIC_BASE_URL": "https://gateway.example",
                "CLAUDE_CODE_OAUTH_TOKEN": SECRET,
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_BEARER_TOKEN_BEDROCK": SECRET,
                "CLAUDE_CONFIG_DIR": "C:\\x",
                "GITHUB_TOKEN": SECRET,
                "MY_SECRET": SECRET,
                "DB_PASSWORD": SECRET,
                "OPENAI_API_KEY": SECRET,
                "KEEP_ME": "1",
                "DISABLE_TELEMETRY": "1",
            },
        })

        project_key = self.cwd.replace("\\", "/")
        write_json(self.state, {
            "oauthAccount": {"emailAddress": "person@example.com"},
            "primaryApiKey": SECRET,
            "mcpServers": {
                "remote-http": {"type": "http", "url": "https://mcp.example.com/mcp",
                                "headers": {"Authorization": "Bearer own-mcp-key"}},
                "local-http": {"type": "http", "url": "http://localhost:3000/mcp"},
                "loopback-sse": {"type": "sse", "url": "http://127.0.0.1:9000/sse"},
                "ipv6-sse": {"type": "sse", "url": "http://[::1]:8080/sse"},
                "npx-wrapped": {"type": "stdio", "command": "cmd", "args": ["/c", "npx", "-y", "@scope/server"]},
                "npx-cmd": {"command": "npx.cmd", "args": ["-y", "@scope/other"]},
                "uvx-tool": {"command": "uvx", "args": ["some-mcp"], "env": {"TOOL_API_KEY": "own-key"}},
                "docker-ok": {"command": "docker", "args": ["run", "-i", "--rm", "ghcr.io/x/y"]},
                "abs-command": {"command": "C:\\Tools\\server.exe", "args": []},
                "drive-arg": {"command": "node", "args": ["C:\\Users\\me\\server.js"]},
                "home-arg": {"command": "python", "args": ["/home/me/srv.py"]},
                "users-arg": {"command": "python3", "args": ["--script=/Users/me/srv.py"]},
                "tilde-arg": {"command": "uv", "args": ["--directory", "~/proj", "run", "srv"]},
                "gitbash-arg": {"command": "docker", "args": ["run", "-v", "/c/data:/data", "img"]},
                "unc-arg": {"command": "node", "args": ["\\\\server\\share\\x.js"]},
                "ruby": {"command": "ruby", "args": ["server.rb"]},
            },
            "projects": {
                project_key: {"mcpServers": {"proj-deno": {"command": "deno", "args": ["run", "-A", "jsr:@x/y"]}}},
                str(root / "other"): {"mcpServers": {"other-proj": {"command": "npx", "args": ["x"]}}},
            },
        })

    def build(self, **kwargs) -> b.Bundle:
        return b.build_bundle(self.config, self.cwd, state_files=[self.state], **kwargs)


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.setup = FullSetup(Path(cls.tmp.name))
        cls.bundle = cls.setup.build()
        cls.files = read_zip(cls.bundle.data)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_layout(self) -> None:
        names = set(self.files)
        for name in (
            "claude/skills/alpha/SKILL.md", "claude/skills/alpha/scripts/run.py", "claude/skills/beta/SKILL.md",
            "claude/agents/reviewer.md", "claude/agents/team/tester.md", "claude/commands/deploy.md",
            "claude/rules/style.md", "claude/output-styles/terse.md", "claude/workflows/release.md",
            "claude/CLAUDE.md", "settings.json", "mcp.json", "manifest.json",
        ):
            self.assertIn(name, names)
        self.assertEqual(self.files["claude/CLAUDE.md"], b"line one\r\nline two\r\n")

    def test_login_data_and_junk_never_included(self) -> None:
        for name in self.files:
            self.assertNotIn("credentials", name)
            self.assertNotIn(".claude.json", name)
            for part in ("projects/", "sessions/", "todos/", "history", "skills/synced", "node_modules",
                         ".git/", "__pycache__", ".pyc", ".env", ".pem", "big.bin"):
                self.assertNotIn(part, name)
        for name, data in self.files.items():
            self.assertNotIn(SECRET.encode(), data, name)
            self.assertNotIn(b"person@example.com", data, name)
        self.assertTrue(any("big.bin" in w for w in self.bundle.warnings))
        self.assertTrue(any(".env" in w for w in self.bundle.warnings))

    def test_settings_filtered(self) -> None:
        settings = json.loads(self.files["settings.json"])
        self.assertEqual(settings["model"], "opus")
        for key in ("apiKeyHelper", "forceLoginMethod", "forceLoginOrgUUID", "otelHeadersHelper",
                    "awsAuthRefresh", "hooks", "statusLine", "enabledPlugins", "extraKnownMarketplaces"):
            self.assertNotIn(key, settings)
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls:*)"]})
        self.assertEqual(settings["env"], {"KEEP_ME": "1", "DISABLE_TELEMETRY": "1"})
        env_warning = next(w for w in self.bundle.warnings if "env settings" in w)
        self.assertIn("ANTHROPIC_API_KEY", env_warning)
        self.assertIn("GITHUB_TOKEN", env_warning)
        self.assertNotIn(SECRET, env_warning)
        self.assertTrue(any("hooks" in w and "statusLine" in w for w in self.bundle.warnings))

    def test_mcp_servers(self) -> None:
        servers = json.loads(self.files["mcp.json"])["mcpServers"]
        self.assertEqual(sorted(servers), ["docker-ok", "npx-cmd", "npx-wrapped", "proj-deno", "remote-http", "uvx-tool"])
        self.assertEqual(servers["npx-wrapped"]["command"], "npx")
        self.assertEqual(servers["npx-wrapped"]["args"], ["-y", "@scope/server"])
        self.assertEqual(servers["npx-cmd"]["command"], "npx")
        self.assertEqual(servers["remote-http"]["headers"], {"Authorization": "Bearer own-mcp-key"})
        self.assertEqual(servers["uvx-tool"]["env"], {"TOOL_API_KEY": "own-key"})
        skipped = self.bundle.manifest["mcp_servers"]["skipped"]
        self.assertEqual(sorted(skipped), sorted([
            "local-http", "loopback-sse", "ipv6-sse", "abs-command", "drive-arg", "home-arg", "users-arg",
            "tilde-arg", "gitbash-arg", "unc-arg", "ruby",
        ]))
        self.assertIn("PC itself", skipped["local-http"])
        self.assertIn("path on the PC", skipped["abs-command"])
        self.assertIn("path on the PC", skipped["drive-arg"])
        self.assertIn("ruby", skipped["ruby"])
        self.assertNotIn("other-proj", servers)
        self.assertTrue(any("'ruby'" in w for w in self.bundle.warnings))

    def test_plugins(self) -> None:
        self.assertEqual(self.bundle.manifest["plugins"], [{"id": "tool@market", "dir": "plugins/tool_market"}])
        self.assertIn("plugins/tool_market/.claude-plugin/plugin.json", self.files)
        self.assertIn("plugins/tool_market/skills/plug/SKILL.md", self.files)
        self.assertFalse(any("node_modules" in name for name in self.files))
        self.assertTrue(any("gone@market" in w for w in self.bundle.warnings))

    def test_manifest(self) -> None:
        manifest = json.loads(self.files["manifest.json"])
        self.assertEqual(manifest, self.bundle.manifest)
        self.assertEqual(manifest["format_version"], b.FORMAT_VERSION)
        self.assertEqual(manifest["skills"], ["alpha", "beta"])
        self.assertEqual(manifest["agents"], ["reviewer.md", "team/tester.md"])
        self.assertEqual(manifest["commands"], ["deploy.md"])
        self.assertEqual(manifest["rules"], ["style.md"])
        self.assertEqual(manifest["output_styles"], ["terse.md"])
        self.assertEqual(manifest["workflows"], ["release.md"])
        self.assertTrue(manifest["claude_md"])
        self.assertEqual(manifest["warnings"], self.bundle.warnings)
        self.assertIn("mcp_credentials", manifest)

    def test_deterministic(self) -> None:
        again = self.setup.build()
        self.assertEqual(again.sha256, self.bundle.sha256)
        self.assertEqual(again.data, self.bundle.data)
        self.assertEqual(self.bundle.size, len(self.bundle.data))
        self.assertEqual(self.bundle.sha256, hashlib.sha256(self.bundle.data).hexdigest())
        with zipfile.ZipFile(io.BytesIO(self.bundle.data)) as archive:
            infos = archive.infolist()
        self.assertEqual([i.filename for i in infos], sorted(i.filename for i in infos))
        self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in infos))

    def test_size_cap(self) -> None:
        with self.assertRaises(b.BundleTooLarge) as caught:
            self.setup.build(max_bytes=1000)
        self.assertIsInstance(caught.exception, ValueError)

    def test_default_state_files(self) -> None:
        config = Path(self.tmp.name) / "home" / ".claude"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            self.assertEqual(b.default_state_files(config), [config / ".claude.json"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(b.default_state_files(config), [config.parent / ".claude.json"])


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundles = self.root / "bundles"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def bundle(self, text: str, **kwargs) -> b.Bundle:
        config = small_config(self.root, text, **kwargs)
        return b.build_bundle(config, str(self.root), state_files=[])

    def assert_rejected(self, data: bytes) -> None:
        with self.assertRaises(ValueError):
            b.install_bundle(data, hashlib.sha256(data).hexdigest(), self.bundles)
        leftovers = list(self.bundles.iterdir()) if self.bundles.exists() else []
        self.assertEqual(leftovers, [])
        self.assertFalse((self.root / "evil").exists())

    def test_install_and_load_installed(self) -> None:
        bundle = self.bundle("hello\n", skills={"alpha": "a"})
        installed = b.install_bundle(bundle.data, bundle.sha256, self.bundles)
        self.assertEqual(installed.root, self.bundles / bundle.sha256)
        self.assertEqual((installed.claude_dir / "CLAUDE.md").read_text(), "hello\n")
        self.assertTrue(installed.settings_path.is_file())
        self.assertTrue(installed.mcp_config_path.is_file())
        self.assertEqual(installed.manifest, bundle.manifest)
        self.assertEqual(b.load_installed(installed.root), installed)

    def test_load_installed_rejects_missing_or_incomplete(self) -> None:
        with self.assertRaises(ValueError):
            b.load_installed(self.bundles / ("a" * 64))
        bundle = self.bundle("x\n")
        installed = b.install_bundle(bundle.data, bundle.sha256, self.bundles)
        installed.mcp_config_path.unlink()
        with self.assertRaises(ValueError):
            b.load_installed(installed.root)

    def test_plugin_dirs(self) -> None:
        setup = FullSetup(self.root / "full")
        bundle = setup.build()
        installed = b.install_bundle(bundle.data, bundle.sha256, self.bundles)
        self.assertEqual(installed.plugin_dirs, [installed.root / "plugins" / "tool_market"])

    def test_sha_mismatch(self) -> None:
        bundle = self.bundle("hello\n")
        with self.assertRaises(ValueError):
            b.install_bundle(bundle.data, "0" * 64, self.bundles)
        with self.assertRaises(ValueError):
            b.install_bundle(bundle.data, "../x", self.bundles)

    def test_rejects_parent_path(self) -> None:
        self.assert_rejected(crafted_zip([("claude/skills/../../../evil", b"x")]))
        self.assert_rejected(crafted_zip([("../evil", b"x")]))

    def test_rejects_absolute_and_drive_names(self) -> None:
        self.assert_rejected(crafted_zip([("/tmp/evil", b"x")]))
        self.assert_rejected(crafted_zip([("C:/evil", b"x")]))

    def test_rejects_link_entry(self) -> None:
        info = zipfile.ZipInfo("claude/skills/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assert_rejected(crafted_zip([(info, b"/etc/passwd")]))

    def test_rejects_unknown_top_level_and_oversize(self) -> None:
        self.assert_rejected(crafted_zip([("evil.sh", b"x")]))
        data = crafted_zip([("claude/skills/a/big", b"\0" * 2048)])
        with self.assertRaises(b.BundleTooLarge):
            b.install_bundle(data, hashlib.sha256(data).hexdigest(), self.bundles, max_bytes=1024)

    def test_reinstall_reuses(self) -> None:
        bundle = self.bundle("hello\n")
        first = b.install_bundle(bundle.data, bundle.sha256, self.bundles)
        marker = first.root / "claude" / "marker.txt"
        marker.write_text("still here")
        second = b.install_bundle(bundle.data, bundle.sha256, self.bundles)
        self.assertEqual(second, first)
        self.assertTrue(marker.exists())

    def test_prune_keeps_three(self) -> None:
        roots = []
        for number in range(5):
            bundle = self.bundle(f"version {number}\n")
            installed = b.install_bundle(bundle.data, bundle.sha256, self.bundles, keep=3)
            # Spread the times out, so the order doesn't depend on the clock's resolution.
            past = 1_000_000 + number * 100
            os.utime(installed.root, (past, past))
            roots.append(installed.root)
        remaining = sorted(p for p in self.bundles.iterdir())
        self.assertEqual(remaining, sorted(roots[2:]))

    def test_prune_never_removes_current(self) -> None:
        old = self.bundle("old\n")
        b.install_bundle(old.data, old.sha256, self.bundles, keep=1)
        new = self.bundle("new\n")
        installed = b.install_bundle(new.data, new.sha256, self.bundles, keep=1)
        os.utime(installed.root, (1, 1))
        again = b.install_bundle(new.data, new.sha256, self.bundles, keep=1)
        self.assertEqual([p.name for p in self.bundles.iterdir()], [again.root.name])


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundles = self.root / "bundles"
        self.home = self.root / "account"
        write(self.home / ".credentials.json", SECRET)
        write(self.home / ".claude.json", "{}")
        write(self.home / "settings.json", '{"theme": "dark"}')
        write(self.home / "skills" / "mine" / "SKILL.md", "own skill\n")
        write(self.home / "skills" / "synced" / "x" / "SKILL.md", "synced\n")
        write(self.home / "skills" / "gamma" / "SKILL.md", "own gamma\n")
        write(self.home / "agents" / "own.md", "own agent\n")
        write(self.home / "projects" / "p" / "s.jsonl", "session\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def install(self, **kwargs) -> b.InstalledBundle:
        config = small_config(self.root, **kwargs)
        bundle = b.build_bundle(config, str(self.root), state_files=[])
        return b.install_bundle(bundle.data, bundle.sha256, self.bundles)

    def snapshot(self) -> dict[str, bytes]:
        return {name: (self.home / name).read_bytes() for name in (
            ".credentials.json", ".claude.json", "settings.json", "skills/mine/SKILL.md",
            "skills/synced/x/SKILL.md", "skills/gamma/SKILL.md", "agents/own.md", "projects/p/s.jsonl",
        )}

    def test_apply_replaces_and_removes_managed_entries(self) -> None:
        before = self.snapshot()
        first = self.install(claude_md="v1\n", skills={"alpha": "alpha v1", "beta": "beta v1", "gamma": "bundle gamma"},
                             agents={"a.md": "agent v1"})
        warnings = b.apply_to_account(first, self.home)
        self.assertEqual((self.home / "CLAUDE.md").read_text(), "v1\n")
        self.assertEqual((self.home / "skills" / "alpha" / "SKILL.md").read_text(), "alpha v1")
        self.assertEqual((self.home / "skills" / "beta" / "SKILL.md").read_text(), "beta v1")
        self.assertEqual((self.home / "agents" / "a.md").read_text(), "agent v1")
        self.assertTrue(any("skills/gamma" in w for w in warnings))
        self.assertEqual(self.snapshot(), before)

        second = self.install(claude_md="", skills={"alpha": "alpha v2"}, agents={"a.md": "agent v2"})
        b.apply_to_account(second, self.home)
        self.assertEqual((self.home / "skills" / "alpha" / "SKILL.md").read_text(), "alpha v2")
        self.assertEqual((self.home / "agents" / "a.md").read_text(), "agent v2")
        self.assertFalse((self.home / "skills" / "beta").exists())
        self.assertFalse((self.home / "CLAUDE.md").exists())
        self.assertEqual(self.snapshot(), before)
        marker = json.loads((self.home / b.MARKER).read_text())
        self.assertEqual(sorted(marker["items"]), ["agents/a.md", "skills/alpha"])
        self.assertEqual(marker["bundle"], second.root.name)

    def test_apply_restores_changed_copy(self) -> None:
        installed = self.install(claude_md="v1\n", skills={"alpha": "alpha v1"})
        b.apply_to_account(installed, self.home)
        write(self.home / "skills" / "alpha" / "SKILL.md", "changed on the server")
        b.apply_to_account(installed, self.home)
        self.assertEqual((self.home / "skills" / "alpha" / "SKILL.md").read_text(), "alpha v1")

    def test_tampered_marker_cannot_reach_other_files(self) -> None:
        before = self.snapshot()
        write_json(self.home / b.MARKER, {"version": 1, "items": {
            "settings.json": {}, ".credentials.json": {}, "projects/p": {}, "skills/../settings.json": {},
            "skills/mine": "not a dict","skills/synced": {},
        }})
        installed = self.install(claude_md="v1\n", skills={"alpha": "a"})
        b.apply_to_account(installed, self.home)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

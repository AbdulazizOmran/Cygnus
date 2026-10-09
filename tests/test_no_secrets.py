"""Nothing in the source can give anyone access to the maintainer's accounts: no API keys, tokens, private keys or credential
files, in any file that is committed or would be (found by git when it is there, by walking the tree when it is not)."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".wrangler", "__pycache__", "build", "dist", ".pytest_cache", ".venv", ".claude", "src", "pkg"}
SECRETS = re.compile(
    r"AQ\.[A-Za-z0-9_-]{20,}"                 # Google API keys (new form)
    r"|AIza[0-9A-Za-z_-]{20,}"                # Google API keys (old form)
    r"|cfut_[A-Za-z0-9]{20,}|cfk_[A-Za-z0-9]{20,}"   # Cloudflare tokens and keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{15,}"   # GitHub and GitLab tokens
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}"                # Slack, OpenAI-style, AWS
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"                                               # private keys
    r"|\boauth_token\s*=|\brefresh_token\s*=|_authToken\s*=")                            # wrangler / npm credential files
CREDENTIAL_FILES = {".dev.vars", ".env", ".npmrc", ".netrc", "id_rsa", "id_ed25519", "credentials.json", "default.toml"}
BINARY = {".png", ".jpg", ".ico", ".svg", ".zst", ".gz", ".xz", ".woff", ".woff2", ".pyc", ".sqlite", ".db"}


def _files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / line for line in out.splitlines() if line and (ROOT / line).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts[:-1]) & SKIP_DIRS)]


def test_no_file_holds_a_key_a_token_or_a_private_key():
    found = []
    for path in _files():
        if path.suffix in BINARY or path.name == "package-lock.json":
            continue
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if SECRETS.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}")
    assert found == []


def test_no_credential_file_is_part_of_the_source():
    assert [str(p.relative_to(ROOT)) for p in _files() if p.name in CREDENTIAL_FILES] == []


def test_the_lockfile_only_points_at_the_public_npm_registry_and_carries_no_login():
    text = (ROOT / "server/ai_worker/package-lock.json").read_text()
    assert "_auth" not in text and "token" not in text.lower().replace("tokens", "")
    for url in re.findall(r'"resolved":\s*"([^"]+)"', text):
        assert url.startswith("https://registry.npmjs.org/"), url


def test_the_worker_configuration_names_no_account_and_the_key_is_only_ever_a_secret_binding():
    config = (ROOT / "server/ai_worker/wrangler.toml").read_text()
    assert not re.search(r"^\s*(account_id|api_token|zone_id)\s*=", config, re.M)
    assert "GEMINI_API_KEY" not in config.replace("`npx wrangler secret put GEMINI_API_KEY`", "")  # not even as a variable name
    assert "/.dev.vars" in "/" + (ROOT / "server/ai_worker/.gitignore").read_text().replace("\n", "\n/")  # a local key file is never committed

#!/usr/bin/env python3
"""Execution flow (4 phases):
1. Prepare filesystem and runtime defaults (migrate legacy layout, create dirs)
2. Resolve update plan and stage server files (local → backup → API priority)
3. Acquire auth tokens when enabled (OAuth2 device flow + game session)
4. Parse and exec startup command with signal forwarding to server process
"""
import os, sys, json, signal, subprocess, time, zipfile, hashlib, shutil, re, shlex
from pathlib import Path
from contextlib import suppress
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

C = {'R':'\033[91m', 'G':'\033[92m', 'Y':'\033[93m', 'B':'\033[94m', 'C':'\033[96m', 'N':'\033[0m'}

def log(color, msg):
    print(f"{color}{msg}{C['N']}", flush=True)

def die(msg):
    log(C['R'], msg)
    sys.exit(1)

ROOT_DIR = Path(os.getenv("ROOT_DIR", "/home/container"))
SERVER_DIR = ROOT_DIR / "Server"
SERVER_JAR = SERVER_DIR / "HytaleServer.jar"
TMP_BASE = ROOT_DIR / ".tmp"
BACKUP_BASE = ROOT_DIR / ".server-backups"

SERVER_VERSION = os.getenv("SERVER_VERSION", "latest")
AUTO_UPDATE = os.getenv("AUTO_UPDATE", "1") == "1"
PATCHLINE = os.getenv("PATCHLINE", "release")
TRANSPORT = os.getenv("TRANSPORT", "QUIC").upper()

FLAGS = {
    'auth': os.getenv("HYTALE_API_AUTH", "1") == "1",
    'aot': os.getenv("USE_AOT_CACHE", "1") == "1",
    'compact_headers': os.getenv("USE_COMPACT_HEADERS", "1") == "1",
    'allow_op': os.getenv("ALLOW_OP", "0") == "1",
    'early_plugins': os.getenv("ACCEPT_EARLY_PLUGINS", "0") == "1",
    'disable_sentry': os.getenv("DISABLE_SENTRY", "0") == "1",
    'ignore_broken_mods': os.getenv("IGNORE_BROKEN_MODS", "0") == "1",
    'world_backup': os.getenv("ENABLE_WORLD_BACKUP", "1") == "1",
    'server_backup': os.getenv("ENABLE_SERVER_BACKUP", "1") == "1",
}

HYTALE_PROFILE_UUID = os.getenv("HYTALE_PROFILE_UUID", "")
HYTALE_AUTH_STATE_PATH = Path(os.getenv("HYTALE_AUTH_STATE_PATH", str(ROOT_DIR / ".hytale-auth.json")))
SERVER_BACKUP_RETENTION = int(os.getenv("SERVER_BACKUP_RETENTION", "2"))

HYTALE_ASSETS_API = "https://account-data.hytale.com/game-assets"
MAVEN_BASE_URL = "https://maven.hytale.com"
HYTALE_DEVICE_AUTH_URL = "https://oauth.accounts.hytale.com/oauth2/device/auth"
HYTALE_TOKEN_URL = "https://oauth.accounts.hytale.com/oauth2/token"
HYTALE_PROFILES_URL = "https://account-data.hytale.com/my-account/get-profiles"
HYTALE_SESSION_URL = "https://sessions.hytale.com/game-session/new"
HYTALE_SESSION_LOGOUT_URL = "https://sessions.hytale.com/game-session"
OAUTH_CLIENT_ID = "hytale-server"
OAUTH_SCOPE = "openid offline auth:server"

VERSION_PATTERN = r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$'
VERSION_FILE = ".version"
PATCHLINE_FILE = ".patchline"
BACKUP_SERVER_FILES = ["HytaleServer.jar", "HytaleServer.aot", ".version", ".patchline"]
USER_CONFIG_FILES = ["config.json", "bans.json", "whitelist.json", "permissions.json"]
BACKUP_ROOT_FILES = ["Assets.zip"]

auth_state = None
server_process = None
shutting_down = False

@dataclass
class AuthState:
    access_token: str = ""
    refresh_token: str = ""
    access_expires: int = 0
    refresh_expires: int = 0
    session_token: str = ""
    identity_token: str = ""
    session_expires: int = 0
    profile_uuid: str = ""
    profile_name: str = ""

    def save(self, path):
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(asdict(self), indent=2))
        temp.chmod(0o600)
        temp.rename(path)

    @classmethod
    def load(cls, path):
        if not path.exists():
            return None
        try:
            path.chmod(0o600)
            data = json.loads(path.read_text())
            expected = set(cls.__dataclass_fields__)
            return cls(**{k: v for k, v in data.items() if k in expected})
        except (TypeError, KeyError) as e:
            log(C['Y'], f"[auth] Auth state schema mismatch, resetting: {e}")
            return None
        except Exception as e:
            log(C['Y'], f"[auth] Failed to load auth state: {e}")
            return None

class UpdatePlan(Enum):
    NONE = "none"
    PATCHLINE = "patchline"
    BACKUP = "backup"
    API = "api"

class AuthManager:
    def __init__(self, session, state_path):
        self.session = session
        self.state_path = state_path
        self.state = AuthState.load(state_path) or AuthState()

    def ensure_authenticated(self):
        if self.state.access_token and time.time() + 60 < self.state.access_expires:
            return True
        if self.state.refresh_token and self.state.refresh_expires > time.time():
            if self._refresh():
                return True
        return self._device_flow()

    def _refresh(self):
        try:
            resp = self.session.post(HYTALE_TOKEN_URL, data={
                'client_id': OAUTH_CLIENT_ID, 'grant_type': 'refresh_token',
                'refresh_token': self.state.refresh_token}, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                now = int(time.time())
                self.state.access_token = data['access_token']
                self.state.access_expires = now + int(data.get('expires_in', 3600))
                if 'refresh_token' in data:
                    self.state.refresh_token = data['refresh_token']
                    self.state.refresh_expires = now + int(data.get('refresh_expires_in', 2592000))
                self.state.save(self.state_path)
                expires_in_min = (self.state.access_expires - now) // 60
                log(C['G'], f"[auth] ✓ Token refreshed (expires in {expires_in_min}m)")
                return True
            log(C['Y'], f"[auth] Refresh failed (HTTP {resp.status_code}): {resp.text[:300]}")
            if resp.status_code == 400:
                with suppress(Exception):
                    error = resp.json().get('error', '')
                    if error in ('invalid_grant', 'invalid_client', 'unauthorized_client'):
                        self.state.access_token = ''
                        self.state.access_expires = 0
                        self.state.refresh_token = ''
                        self.state.refresh_expires = 0
                        self.state_path.unlink(missing_ok=True)
        except Exception as e:
            log(C['Y'], f"[auth] Refresh error: {e}")
        return False

    def _device_flow(self):
        try:
            resp = self.session.post(HYTALE_DEVICE_AUTH_URL,
                data={'client_id': OAUTH_CLIENT_ID, 'scope': OAUTH_SCOPE}, timeout=30)
            if resp.status_code != 200:
                log(C['R'], f"[auth] Device auth failed (HTTP {resp.status_code})")
                return False
            data = resp.json()
            device_code = data['device_code']
            log(C['C'], "  ═══════════════════════════════════════════════════════════")
            log(C['C'], f"  Please visit: {data.get('verification_uri_complete', data.get('verification_uri'))}")
            if 'user_code' in data:
                log(C['C'], f"  Or enter code: {data['user_code']} at https://accounts.hytale.com/device")
            expires_in = int(data.get('expires_in', 900))
            log(C['C'], f"  Waiting for authorization (expires in {expires_in}s)...")
            log(C['C'], "  ═══════════════════════════════════════════════════════════")
            interval = int(data.get('interval', 5))
            poll_start = time.time()
            while (time.time() - poll_start) < expires_in:
                time.sleep(interval)
                try:
                    token_resp = self.session.post(HYTALE_TOKEN_URL, data={
                        'client_id': OAUTH_CLIENT_ID,
                        'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                        'device_code': device_code}, timeout=30)
                    if token_resp.status_code == 200:
                        token_data = token_resp.json()
                        now = int(time.time())
                        self.state.access_token = token_data['access_token']
                        self.state.refresh_token = token_data.get('refresh_token', '')
                        self.state.access_expires = now + int(token_data.get('expires_in', 3600))
                        self.state.refresh_expires = now + int(token_data.get('refresh_expires_in', 2592000))
                        self.state.save(self.state_path)
                        expires_in_min = (self.state.access_expires - now) // 60
                        log(C['G'], f"[auth] ✓ Authenticated (token expires in {expires_in_min}m)")
                        return True
                    try:
                        error_data = token_resp.json()
                    except (ValueError, json.JSONDecodeError):
                        log(C['R'], f"[auth] Non-JSON error response (HTTP {token_resp.status_code}): {token_resp.text[:200]}")
                        return False
                    error = error_data.get('error', '')
                    if error == 'authorization_pending':
                        continue
                    if error == 'slow_down' or token_resp.status_code in (429, 500, 502, 503):
                        interval = min(interval + 5, 60)
                        continue
                    if error in ('access_denied', 'expired_token'):
                        log(C['R'], f"[auth] Authorization {error}")
                        return False
                except Exception as e:
                    log(C['Y'], f"[auth] Token poll error: {e}")
                    continue
            log(C['R'], "[auth] Authorization timeout")
        except Exception as e:
            log(C['R'], f"[auth] Device flow error: {e}")
        return False

    def authed_request(self, method, url, **kwargs):
        extra = kwargs.pop('headers', {})
        def send():
            return self.session.request(method, url,
                headers={**extra, 'Authorization': f'Bearer {self.state.access_token}'}, **kwargs)
        resp = send()
        if resp.status_code == 401 and (self._refresh() or self._device_flow()):
            resp = send()
        return resp

    def ensure_session(self):
        if self.state.session_token and not (self.state.session_expires > 0 and time.time() + 60 >= self.state.session_expires):
            return True
        if self.state.session_token:
            try:
                self.session.delete(HYTALE_SESSION_LOGOUT_URL,
                    headers={'Authorization': f'Bearer {self.state.session_token}'}, timeout=10)
            except Exception as e:
                log(C['Y'], f"[auth] Session cleanup error: {e}")
                pass
        if not self.state.profile_uuid:
            try:
                resp = self.authed_request('GET', HYTALE_PROFILES_URL, timeout=30)
                if resp.status_code == 200:
                    profiles = resp.json().get('profiles', [])
                    if profiles:
                        self.state.profile_uuid = profiles[0].get('uuid', '')
                        self.state.profile_name = profiles[0].get('username', '')
                else:
                    log(C['Y'], f"[auth] Profile fetch failed (HTTP {resp.status_code})")
                    return False
            except Exception as e:
                log(C['Y'], f"[auth] Profile error: {e}")
                return False
        try:
            resp = self.authed_request('POST', HYTALE_SESSION_URL, json={'uuid': self.state.profile_uuid}, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                self.state.session_token = data.get('sessionToken', '')
                self.state.identity_token = data.get('identityToken', '')
                expires_at = data.get('expiresAt', '')
                if expires_at:
                    try:
                        self.state.session_expires = int(datetime.fromisoformat(expires_at.replace('Z', '+00:00')).timestamp())
                    except Exception as e:
                        log(C['Y'], f"[auth] Failed to parse session expiry: {e}")
                        self.state.session_expires = int(time.time()) + 3600
                self.state.save(self.state_path)
                return bool(self.state.session_token)
            log(C['Y'], f"[auth] Session request failed (HTTP {resp.status_code}): {resp.text[:300]}")
        except Exception as e:
            log(C['Y'], f"[auth] Session error: {e}")
        return False

    def cleanup(self):
        if not self.state.session_token:
            return
        log(C['B'], "[auth] Cleaning up game session")
        try:
            resp = requests.delete(HYTALE_SESSION_LOGOUT_URL,
                headers={'Authorization': f'Bearer {self.state.session_token}'}, timeout=3)
            if resp.status_code in (200, 204):
                log(C['G'], "[auth] ✓ Session terminated")
            else:
                log(C['Y'], f"[auth] Session cleanup returned HTTP {resp.status_code} (non-fatal)")
        except Exception as e:
            log(C['Y'], f"[auth] Session cleanup error: {e} (non-fatal)")
        fresh = AuthState.load(self.state_path) or self.state
        fresh.session_token = fresh.identity_token = ""
        fresh.session_expires = 0
        fresh.save(self.state_path)
        self.state = fresh

def parse_jar_version(jar_path):
    """Extract version and patchline from JAR manifest for metadata tracking."""
    if not jar_path.exists():
        return None, None
    try:
        with zipfile.ZipFile(jar_path) as zf:
            manifest = zf.read("META-INF/MANIFEST.MF").decode('utf-8')
            version = patchline = None
            for line in manifest.split('\n'):
                lower = line.lower()
                if lower.startswith('implementation-version:'):
                    version = line.split(':', 1)[1].strip()
                elif lower.startswith('implementation-patchline:'):
                    patchline = line.split(':', 1)[1].strip()
            return version, patchline
    except Exception as e:
        log(C['Y'], f"[version] Failed to parse JAR manifest: {e}")
        return None, None

def backup_current_version(server_dir, backup_base, patchline, retention):
    if not FLAGS['server_backup']:
        log(C['C'], "[backup] Server backup disabled (ENABLE_SERVER_BACKUP=0)")
        return
    version_file = server_dir / VERSION_FILE
    if not version_file.exists() or not (server_dir / "HytaleServer.jar").exists():
        log(C['C'], "[backup] Skipped (no existing server files)")
        return
    version = version_file.read_text().strip()
    if not re.match(VERSION_PATTERN, version):
        log(C['C'], "[backup] Skipped (invalid version)")
        return
    backup_dir = backup_base / patchline / version
    backup_dir.mkdir(parents=True, exist_ok=True)
    server_backup_dir = backup_dir / "Server"
    server_backup_dir.mkdir(exist_ok=True)
    for files, base, dest in [(BACKUP_SERVER_FILES, server_dir, server_backup_dir), (BACKUP_ROOT_FILES, ROOT_DIR, backup_dir)]:
        for f in files:
            if (src := base / f).exists(): shutil.copy2(src, dest / f)
    # Cleanup old backups: keep only N most recent versions per patchline
    patchline_dir = backup_base / patchline
    if patchline_dir.exists():
        if retention > 0:
            backups = sorted([d for d in patchline_dir.iterdir() if d.is_dir() and d != backup_dir])
            for old in backups[:-retention]:
                shutil.rmtree(old, ignore_errors=True)
    log(C['G'], f"[backup] ✓ .server-backups/{patchline}/{version}/ (retention: {retention})")

def restore_from_backup(backup_dir, server_dir):
    backup_server_dir = backup_dir / "Server"
    if not (backup_server_dir / "HytaleServer.jar").exists():
        log(C['Y'], f"[backup] Restore failed: {backup_server_dir / 'HytaleServer.jar'} not found")
        return False
    log(C['C'], f"[backup] Restoring from {backup_dir}")
    (server_dir / "HytaleServer.aot").unlink(missing_ok=True)
    for f in BACKUP_SERVER_FILES:
        if (src := backup_server_dir / f).exists():
            log(C['C'], f"[backup] Copying {f}")
            shutil.copy2(src, server_dir / f)
    for f in BACKUP_ROOT_FILES:
        if (src := backup_dir / f).exists():
            shutil.copy2(src, ROOT_DIR / f)
        else:
            log(C['Y'], f"[backup] Warning: {f} missing from backup")
    return True

def install_from_extract(extract_dir, server_dir):
    """Install server files from extracted download, preserving user configs."""
    src_server = extract_dir / "Server"
    if not src_server.exists() or not (src_server / "HytaleServer.jar").exists():
        return False
    (server_dir / "HytaleServer.aot").unlink(missing_ok=True)
    for item in src_server.iterdir():
        # Preserve existing user config files during updates
        if item.name in USER_CONFIG_FILES and (server_dir / item.name).exists():
            continue
        if item.name.endswith('.aot'):
            continue
        dest = server_dir / item.name
        if item.is_dir():
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    if (assets := extract_dir / "Assets.zip").exists():
        shutil.copy2(assets, ROOT_DIR / "Assets.zip")
    return True

def apply_staged_update():
    if not (jar := ROOT_DIR / "updater" / "staging" / "Server" / "HytaleServer.jar").exists():
        return False
    log(C['B'], "[update] Applying staged update")
    old_patchline = (SERVER_DIR / PATCHLINE_FILE).read_text().strip() if (SERVER_DIR / PATCHLINE_FILE).exists() else PATCHLINE
    backup_current_version(SERVER_DIR, BACKUP_BASE, old_patchline, SERVER_BACKUP_RETENTION)
    if install_from_extract(ROOT_DIR / "updater" / "staging", SERVER_DIR):
        shutil.rmtree(ROOT_DIR / "updater", ignore_errors=True)
        v, p = parse_jar_version(SERVER_JAR)
        if v:
            (SERVER_DIR / VERSION_FILE).write_text(v)
            log(C['G'], f"[update] ✓ Applied {v}")
        if p:
            (SERVER_DIR / PATCHLINE_FILE).write_text(p)
        return True
    return False

def check_disk_space(path, required_bytes, margin=1.5):
    stat = shutil.disk_usage(path)
    needed = int(required_bytes * margin)
    if stat.free < needed:
        return False, stat.free / (1024**2), needed / (1024**2)
    return True, 0, 0

def get_maven_metadata(session, patchline):
    try:
        resp = session.get(f"{MAVEN_BASE_URL}/{patchline}/com/hypixel/hytale/Server/maven-metadata.xml", timeout=30)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        log(C['Y'], f"[maven] Failed to fetch metadata: {e}")
        pass
    return None

def api_latest(session, auth_mgr, patchline):
    if not auth_mgr.ensure_authenticated():
        return None
    try:
        resp = auth_mgr.authed_request('GET', f"{HYTALE_ASSETS_API}/version/{patchline}.json", timeout=15)
        if resp.status_code != 200:
            return None
        version = session.get(resp.json()['url'], timeout=15).json().get('version')
        if version:
            log(C['C'], f"[update] Latest {patchline}: {version}")
        return version
    except Exception:
        return None

def maven_version_exists(session, patchline, version):
    if metadata := get_maven_metadata(session, patchline):
        versions = re.findall(r'<version>\s*([^<]+)', metadata)
        return version in versions
    return False

def is_valid_backup(backup_path):
    return backup_path.exists() and (backup_path / "Server" / "HytaleServer.jar").exists()

def api_download(session, auth_mgr, patchline, target_dir):
    for attempt in range(3):
        try:
            resp = auth_mgr.authed_request('GET', f"{HYTALE_ASSETS_API}/version/{patchline}.json", timeout=30)
            if resp.status_code != 200:
                continue
            manifest_data = session.get(resp.json()['url'], timeout=30).json()
            version, download_url, sha256_expected = manifest_data['version'], manifest_data['download_url'], manifest_data.get('sha256')
            log(C['C'], f"[api] Remote: {version}")
            signed_dl = auth_mgr.authed_request('GET', f"{HYTALE_ASSETS_API}/{download_url}", timeout=30).json()['url']
            zip_path = target_dir / "server.zip"
            target_dir.mkdir(parents=True, exist_ok=True)
            resp = session.get(signed_dl, stream=True, timeout=900)
            total = int(resp.headers.get('content-length', 0))
            if total > 0:
                ok, free_mb, needed_mb = check_disk_space(target_dir, total)
                if not ok:
                    log(C['R'], f"[api] Insufficient space: {free_mb:.0f} MB free, {needed_mb:.0f} MB needed")
                    return False
            downloaded = 0
            with open(zip_path, 'wb') as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        print(f"\r[api] {downloaded/(1024*1024):.1f}/{total/(1024*1024):.1f} MB ({100*downloaded/total:.0f}%)", end='', file=sys.stderr)
            if total > 0:
                print(file=sys.stderr)
            if not sha256_expected:
                log(C['Y'], "[api] No SHA-256 in manifest, refusing download")
                zip_path.unlink()
                continue
            h = hashlib.sha256()
            with open(zip_path, 'rb') as hf:
                while blk := hf.read(65536):
                    h.update(blk)
            if h.hexdigest() != sha256_expected:
                log(C['Y'], "[api] SHA-256 mismatch, retrying")
                zip_path.unlink()
                continue
            log(C['G'], "[api] ✓ Verified")
            log(C['B'], "[api] Extracting...")
            with zipfile.ZipFile(zip_path) as zf:
                target_resolved = target_dir.resolve()
                for member in zf.infolist():
                    dest = (target_dir / member.filename).resolve()
                    if not dest.is_relative_to(target_resolved):
                        log(C['Y'], f"[api] Skipping unsafe zip entry: {member.filename}")
                        continue
                    if member.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(dest, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
            zip_path.unlink()
            return (target_dir / "Server" / "HytaleServer.jar").exists()
        except Exception as e:
            log(C['Y'], f"[api] Attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    return False

def plan_update(session, auth_mgr, server_version, patchline, local_version, local_patchline, staged_applied):
    """Determine update strategy based on SERVER_VERSION.

    Priority order: local files → backups → API download
    Returns: (UpdatePlan, target_version, backup_path)
    """
    backup_dir = BACKUP_BASE / patchline
    has_jar = SERVER_JAR.exists()
    if staged_applied:
        return UpdatePlan.NONE, "", None
    if server_version == "latest":
        if not AUTO_UPDATE and has_jar:
            if local_patchline and local_patchline != patchline and backup_dir.exists():
                if backups := sorted([d for d in backup_dir.iterdir() if d.is_dir() and (d / "Server" / "HytaleServer.jar").exists()], reverse=True):
                    return UpdatePlan.BACKUP, backups[0].name, backups[0]
            return UpdatePlan.NONE, "", None
        if remote := api_latest(session, auth_mgr, patchline):
            if local_version == remote and (local_patchline == patchline or not local_patchline):
                return UpdatePlan.NONE, "", None
            if local_version == remote and local_patchline != patchline:
                return UpdatePlan.PATCHLINE, remote, None
            backup_path = backup_dir / remote
            if is_valid_backup(backup_path):
                return UpdatePlan.BACKUP, remote, backup_path
            return UpdatePlan.API, remote, None
        if not has_jar:
            return UpdatePlan.API, "", None
        log(C['Y'], "[update] API check failed, running existing server")
        return UpdatePlan.NONE, "", None
    elif server_version == "previous":
        if not backup_dir.exists():
            die(f"[backup] No backups in {patchline}")
        backups = sorted([d for d in backup_dir.iterdir() if d.is_dir() and d.name != local_version and (d / "Server" / "HytaleServer.jar").exists()], reverse=True)
        if not backups:
            die("[backup] No previous backup")
        return (UpdatePlan.NONE, "", None) if local_version == backups[0].name and has_jar else (UpdatePlan.BACKUP, backups[0].name, backups[0])
    else:
        if local_version == server_version and has_jar:
            return UpdatePlan.NONE, "", None
        backup_path = backup_dir / server_version
        if is_valid_backup(backup_path):
            return UpdatePlan.BACKUP, server_version, backup_path
        if not maven_version_exists(session, patchline, server_version):
            if has_jar:
                log(C['Y'], f"[update] Version {server_version} not found, running existing server")
                return UpdatePlan.NONE, "", None
            die(f"Version {server_version} not available")
        if remote := api_latest(session, auth_mgr, patchline):
            if remote != server_version:
                log(C['Y'], f"[update] Version {server_version} exists but API only serves latest ({remote})")
                if has_jar:
                    log(C['Y'], "[update] Running existing server")
                    return UpdatePlan.NONE, "", None
                die(f"Version {server_version} not downloadable via API")
        return UpdatePlan.API, server_version, None

def migrate_legacy_layout():
    if not (root_jar := ROOT_DIR / "HytaleServer.jar").exists():
        return
    if SERVER_JAR.exists():
        root_jar.unlink()
    else:
        shutil.move(str(root_jar), str(SERVER_JAR))
    (ROOT_DIR / "HytaleServer.aot").unlink(missing_ok=True)
    for file in [VERSION_FILE, PATCHLINE_FILE]:
        if (src := ROOT_DIR / file).exists():
            shutil.move(str(src), str(SERVER_DIR / file))
    for dir_name in ["Licenses", "logs", "universe", "earlyplugins", "builtin", "worlds", "mods"]:
        if not (src_dir := ROOT_DIR / dir_name).is_dir():
            continue
        dest_dir = SERVER_DIR / dir_name
        if dest_dir.exists():
            try:
                shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True, copy_function=shutil.move)
                shutil.rmtree(src_dir, ignore_errors=True)
            except Exception as e:
                log(C['Y'], f"[migrate] Failed to merge {dir_name}: {e}")
        else:
            shutil.move(str(src_dir), str(dest_dir))

def handle_signal(signum, frame):
    global shutting_down, server_process
    if not shutting_down and server_process:
        shutting_down = True
        try:
            os.killpg(os.getpgid(server_process.pid), signum)
        except Exception as e:
            log(C['Y'], f"[signal] Failed to kill process group: {e}")
            server_process.send_signal(signum)

def main():
    global auth_state, server_process, PATCHLINE
    os.umask(0o077)
    os.environ.setdefault('TZ', 'UTC')
    start_time = time.time()
    os.chdir(ROOT_DIR)
    TMP_BASE.mkdir(exist_ok=True)
    SERVER_DIR.mkdir(exist_ok=True)
    # Server jar expects start.sh to exist when using /update command
    (ROOT_DIR / "start.sh").touch(mode=0o644, exist_ok=True)
    for item in TMP_BASE.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    migrate_legacy_layout()
    staged_applied = apply_staged_update()
    # Override patchline from config.json if set (takes precedence over env var)
    if (config_file := SERVER_DIR / "config.json").exists():
        with suppress(Exception):
            if (pl := json.loads(config_file.read_text()).get("Update", {}).get("Patchline", "")) in ("release", "pre-release"):
                PATCHLINE = pl
    # Toggle early plugins directory
    early_dir = SERVER_DIR / "earlyplugins"
    early_off = SERVER_DIR / "earlyplugins.disabled"
    src = early_off if FLAGS['early_plugins'] else early_dir
    dst = early_dir if FLAGS['early_plugins'] else early_off
    if src.exists():
        if dst.exists():
            try:
                shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.move)
                shutil.rmtree(src, ignore_errors=True)
            except Exception as e:
                log(C['Y'], f"[migrate] Failed to merge directories: {e}")
        else:
            shutil.move(str(src), str(dst))
    if FLAGS['early_plugins']:
        early_dir.mkdir(exist_ok=True)
    # Warn if host UDP buffers are smaller than recommended for QUIC traffic to improve latency
    if TRANSPORT == "QUIC":
        for buf_type, buf_path in [("receive", "/proc/sys/net/core/rmem_max"), ("send", "/proc/sys/net/core/wmem_max")]:
            with suppress(Exception):
                if (current := int(Path(buf_path).read_text().strip())) < 2097152:
                    log(C['Y'], f"[quic] UDP {buf_type} buffer low ({current} bytes)")
    # Load version info
    version_file = SERVER_DIR / VERSION_FILE
    patchline_file = SERVER_DIR / PATCHLINE_FILE
    local_version = version_file.read_text().strip() if version_file.exists() else ""
    local_patchline = patchline_file.read_text().strip() if patchline_file.exists() else ""
    if SERVER_JAR.exists() and (not local_version or not local_patchline):
        v, p = parse_jar_version(SERVER_JAR)
        if v:
            local_version = v
            version_file.write_text(v)
        if p:
            local_patchline = p
            patchline_file.write_text(p)
    log(C['C'], f"Current version : {local_version or 'none'} ({local_patchline or 'unknown'})")
    log(C['C'], f"Active patchline: {PATCHLINE}")
    log(C['C'], f"Requested build : {SERVER_VERSION}")
    # Create HTTP session
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "HytaleServerLauncher/1.0"})
    auth_mgr = AuthManager(session, HYTALE_AUTH_STATE_PATH)
    plan, target, backup_path = plan_update(session, auth_mgr, SERVER_VERSION, PATCHLINE, local_version, local_patchline, staged_applied)
    if plan != UpdatePlan.NONE:
        log(C['C'], f"[update] Plan: {plan.value}" + (f" (target {target})" if target else ""))
    # Execute plan
    if plan == UpdatePlan.PATCHLINE:
        backup_current_version(SERVER_DIR, BACKUP_BASE, local_patchline, SERVER_BACKUP_RETENTION)
        patchline_file.write_text(PATCHLINE)
        log(C['G'], "[update] ✓ Patchline updated")
    elif plan == UpdatePlan.BACKUP:
        log(C['B'], f"[backup] Restoring {target or 'version'} from backup")
        backup_current_version(SERVER_DIR, BACKUP_BASE, local_patchline, SERVER_BACKUP_RETENTION)
        if restore_from_backup(backup_path, SERVER_DIR):
            v, p = parse_jar_version(SERVER_JAR)
            if v:
                version_file.write_text(v)
            if p:
                patchline_file.write_text(p)
            log(C['G'], "[backup] ✓ Restored")
        elif not SERVER_JAR.exists():
            die(f"[backup] Restore failed and no server files exist")
        else:
            log(C['Y'], "[backup] Restore failed, running existing server")
    elif plan == UpdatePlan.API:
        if auth_mgr.ensure_authenticated():
            download_dir = TMP_BASE / "api-download"
            shutil.rmtree(download_dir, ignore_errors=True)
            if api_download(session, auth_mgr, PATCHLINE, download_dir):
                backup_current_version(SERVER_DIR, BACKUP_BASE, local_patchline, SERVER_BACKUP_RETENTION)
                if install_from_extract(download_dir, SERVER_DIR):
                    v, p = parse_jar_version(SERVER_JAR)
                    if v:
                        version_file.write_text(v)
                        log(C['G'], f"[api] ✓ Updated to {v}")
                    if p:
                        patchline_file.write_text(p)
                shutil.rmtree(download_dir, ignore_errors=True)
            elif not SERVER_JAR.exists():
                die("[update] Download failed")
            else:
                log(C['Y'], "[update] Download failed, running existing server")
        elif SERVER_JAR.exists():
            log(C['Y'], "[auth] Auth failed, running existing server")
        else:
            die("[auth] Authentication required")
    # Authentication for server startup
    if FLAGS['auth']:
        if auth_mgr.ensure_authenticated() and auth_mgr.ensure_session():
            os.environ.update({
                'HYTALE_SERVER_SESSION_TOKEN': auth_mgr.state.session_token,
                'HYTALE_SERVER_IDENTITY_TOKEN': auth_mgr.state.identity_token,
                'HYTALE_SERVER_SESSION_EXPIRES': str(auth_mgr.state.session_expires),
                'HYTALE_SERVER_OAUTH_ACCESS_TOKEN': auth_mgr.state.access_token,
                'HYTALE_SERVER_OAUTH_REFRESH_TOKEN': auth_mgr.state.refresh_token,
                'HYTALE_SERVER_OAUTH_ACCESS_EXPIRES': str(auth_mgr.state.access_expires),
                'HYTALE_PROFILE_UUID': auth_mgr.state.profile_uuid,
                'HYTALE_PROFILE_NAME': auth_mgr.state.profile_name})
            now = int(time.time())
            a_exp = f"{(auth_mgr.state.access_expires - now) // 60}m" if auth_mgr.state.access_expires > now else "expired"
            s_exp = f"{(auth_mgr.state.session_expires - now) // 60}m" if auth_mgr.state.session_expires > now else "expired"
            r_exp = f"{(auth_mgr.state.refresh_expires - now) // 3600}h" if auth_mgr.state.refresh_expires > now else "expired"
            log(C['G'], f"[auth] ✓ Tokens ready (access in {a_exp}, session in {s_exp}, refresh in {r_exp})")
            auth_state = auth_mgr
        else:
            log(C['Y'], "[auth] Continuing without pre-acquired tokens")
    # Build JVM and server flags for automatic injection
    jvm_flags = ["-Djava.io.tmpdir=/home/container/.tmp", "-Dterminal.jline=false", "-Dterminal.ansi=true"]
    aot_file = SERVER_DIR / "HytaleServer.aot"

    # AOT cache: create on first run, use on subsequent runs
    if FLAGS['aot']:
        jvm_flags.append("-Xlog:aot")
        if FLAGS['compact_headers']:
            jvm_flags.append("-XX:+UseCompactObjectHeaders")
        if not aot_file.exists():
            jvm_flags.append(f"-XX:AOTCacheOutput={aot_file}")
            compact_status = "with" if FLAGS['compact_headers'] else "without"
            log(C['C'], f"[aot] Creating AOT cache {compact_status} CompactObjectHeaders (first run will be slower)")
        else:
            jvm_flags.append(f"-XX:AOTCache={aot_file}")
            log(C['C'], "[aot] Using AOT cache")
    elif FLAGS['compact_headers']:
        jvm_flags.append("-XX:+UseCompactObjectHeaders")

    server_flags = ["--transport", TRANSPORT]
    if FLAGS['allow_op']: server_flags.append("--allow-op")
    if FLAGS['early_plugins']: server_flags.append("--accept-early-plugins")
    if FLAGS['disable_sentry']: server_flags.append("--disable-sentry")
    if FLAGS['ignore_broken_mods']: server_flags.append("--ignore-broken-mods")
    if FLAGS['world_backup']:
        server_flags.extend(["--backup", f"--backup-dir", f"../{os.getenv('BACKUP_DIR', 'Backups')}",
            "--backup-frequency", os.getenv('BACKUP_FREQUENCY', '30'),
            "--backup-max-count", os.getenv('BACKUP_MAX_COUNT', '5'),
            "--backup-archive-max-count", os.getenv('BACKUP_ARCHIVE_MAX_COUNT', '5')])

    startup = os.getenv("STARTUP", "")
    startup_json = os.getenv("STARTUP_JSON", "")

    if startup_json:
        cmd = json.loads(startup_json)
        if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
            die("[startup] STARTUP_JSON must be JSON array of strings")
    else:
        startup = re.sub(r'\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}', lambda m: os.getenv(m.group(1), ""), startup)
        startup = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', lambda m: os.getenv(m.group(1), ""), startup)
        if not startup.strip(): die("[startup] Empty STARTUP")
        cmd = shlex.split(startup)

    # Inject flags if this is a Java command with -jar
    if cmd and any("java" in str(arg).lower() for arg in cmd[:3]) and "-jar" in cmd:
        jar_flag_idx = cmd.index("-jar")
        jar_idx = jar_flag_idx + 1
        pre_jar, post_jar = cmd[:jar_flag_idx], cmd[jar_idx+1:]
        # Drop injections the user already specified to avoid duplicate-arg crashes (joptsimple rejects repeated value options)
        pre_names = {a.split("=", 1)[0] for a in pre_jar}
        jvm_flags = [f for f in jvm_flags if f.split("=", 1)[0] not in pre_names]
        deduped, i = [], 0
        while i < len(server_flags):
            pair = i+1 < len(server_flags) and not server_flags[i+1].startswith("-")
            if server_flags[i] in post_jar:
                i += 2 if pair else 1
            else:
                deduped.append(server_flags[i])
                if pair: deduped.append(server_flags[i+1]); i += 2
                else: i += 1
        cmd = pre_jar + jvm_flags + cmd[jar_flag_idx:jar_idx+1] + deduped + post_jar
    os.chdir(SERVER_DIR)
    # Final version confirmation before launch
    final_version = version_file.read_text().strip() if version_file.exists() else "unknown"
    final_patchline = patchline_file.read_text().strip() if patchline_file.exists() else "unknown"
    log(C['G'], f"[startup] Launching: {final_version} ({final_patchline})")
    log(C['G'], f"[startup] ✓ Ready in {int(time.time() - start_time)}s")
    log(C['C'], f"[startup] Command: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, handle_signal)
    try:
        server_process = subprocess.Popen(cmd, preexec_fn=os.setsid)
        sys.exit(server_process.wait())
    finally:
        if auth_state:
            auth_state.cleanup()

if __name__ == "__main__":
    main()

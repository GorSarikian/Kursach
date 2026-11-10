"""Create local secrets once and produce the realm file for first startup."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".secrets"

def write_secret(name, value):
    path = PRIVATE / name
    if not path.exists():
        path.write_bytes(value)
    # Compose bind-mounts these files for container users with different UIDs.
    # The private parent directory limits host access.
    path.chmod(0o644)
    return path

def main():
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        PRIVATE.chmod(0o700)
    for name in ("db_password", "admin_password", "oidc_secret"):
        write_secret(name, secrets.token_urlsafe(32).encode())
    write_secret("cookie_secret", secrets.token_bytes(32))
    realm = json.loads((ROOT / "config/realm.json").read_text(encoding="utf-8"))
    realm["clients"][0]["secret"] = (PRIVATE / "oidc_secret").read_text()
    (PRIVATE / "iam-realm.json").write_text(json.dumps(realm), encoding="utf-8")
    (PRIVATE / "iam-realm.json").chmod(0o644)
    if all((PRIVATE / f).is_file() for f in ("ca_cert", "tls_cert", "tls_key")):
        return
    openssl = os.environ.get("IAM_OPENSSL") or shutil.which("openssl")
    if not openssl:
        raise SystemExit("OpenSSL is required. On Windows set IAM_OPENSSL to openssl.exe.")
    def run(*args):
        subprocess.run([openssl, *args], cwd=PRIVATE, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)
    run("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "365", "-sha256",
        "-subj", "/CN=IAM Course Lab CA", "-keyout", "ca_key", "-out", "ca_cert",
        "-addext", "basicConstraints=critical,CA:TRUE")
    run("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=iam.test",
        "-keyout", "tls_key", "-out", "server.csr")
    (PRIVATE / "server.ext").write_text("subjectAltName=DNS:iam.test,DNS:portal.test\nextendedKeyUsage=serverAuth\n")
    run("x509", "-req", "-in", "server.csr", "-CA", "ca_cert", "-CAkey", "ca_key",
        "-CAcreateserial", "-days", "180", "-sha256", "-extfile", "server.ext", "-out", "tls_cert")
    (PRIVATE / "ca_key").chmod(0o600)
    (PRIVATE / "tls_key").chmod(0o600)

if __name__ == "__main__":
    main()

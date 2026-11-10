"""Deploy the tested image to a manager, then verify both Keycloak replicas."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
DIND = os.getenv("IAM_DIND")
STACK = os.getenv("IAM_STACK", "iam")
TARGET = os.getenv("IAM_TARGET", "127.0.0.1")
HOST = "iam.test"
PORTAL = "portal.test"
ISSUER = "https://iam.test:8443/realms/iam"
CONFIGS = {
    "IAM_NGINX_REV": "config/nginx.conf",
    "IAM_AUTH_REV": "config/oauth2.cfg",
    "IAM_PORTAL_REV": "config/portal.html",
}

def docker(*args, env=None, quiet=False, check=True):
    command = ["docker"]
    settings = env or {}
    if DIND:
        command += ["exec", "-w", "/opt/iam"]
        for key, value in settings.items():
            command += ["-e", f"{key}={value}"]
        command += [DIND, "docker"]
    return subprocess.run(command + list(args), cwd=ROOT, env={**os.environ, **settings},
                          text=True, capture_output=quiet, check=check)

class LabConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection((TARGET, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

def get(host, route):
    ctx = ssl.create_default_context(cafile=str(ROOT / ".secrets/ca_cert"))
    conn = LabConnection(host, 8443, timeout=4, context=ctx)
    try:
        conn.request("GET", route)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()

def wait_for(predicate, label):
    for _ in range(75):
        try:
            if predicate():
                return
        except (OSError, http.client.HTTPException, ValueError):
            pass
        time.sleep(4)
    raise RuntimeError(f"Timed out: {label}")

def discovery():
    status, data = get(HOST, "/realms/iam/.well-known/openid-configuration")
    return status == 200 and json.loads(data)["issuer"] == ISSUER

def converged():
    current = docker("service", "ls", "--filter", f"name={STACK}_",
                     "--format", "{{.Replicas}}", quiet=True).stdout.splitlines()
    return sorted(line.split()[0] for line in current if line.strip()) == ["1/1", "1/1", "1/1", "1/1", "2/2"]

def main():
    image = os.environ["IAM_IMAGE"]
    resolve = os.getenv("IAM_RESOLVE", "always")
    revisions = {key: hashlib.sha256((ROOT / file).read_bytes()).hexdigest()[:12]
                 for key, file in CONFIGS.items()}
    def deploy(replicas):
        docker("stack", "deploy", "--resolve-image", resolve, "--with-registry-auth",
               "-c", "stack.yaml", STACK,
               env={"IAM_IMAGE":image, "IAM_REPLICAS":str(replicas),
                    "IAM_STACK":STACK, **revisions})
    exists = docker("service", "inspect", f"{STACK}_keycloak", quiet=True, check=False)
    if exists.returncode:
        deploy(1)
        wait_for(discovery, "initial realm import")
    deploy(2)
    wait_for(converged, "five services and six tasks")
    wait_for(discovery, "OIDC discovery")
    wait_for(lambda: get(PORTAL, "/ping")[0] == 200, "OAuth2 Proxy")
    expected = docker("service", "inspect", f"{STACK}_keycloak", "--format",
                      "{{.Spec.TaskTemplate.ContainerSpec.Image}}", quiet=True).stdout.strip()
    if image.split("@", 1)[0] != expected.split("@", 1)[0] or ("@" in image and image != expected):
        raise RuntimeError(f"Image did not converge: {expected}")
    state = docker("service", "inspect", f"{STACK}_keycloak", "--format",
                   "{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}", quiet=True).stdout.strip()
    if state in {"paused", "rollback_started", "rollback_paused", "rollback_completed"}:
        raise RuntimeError(f"Update entered state {state}")
    docker("stack", "services", STACK)

if __name__ == "__main__":
    main()

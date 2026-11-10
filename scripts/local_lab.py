"""Start or update a disposable two-node Swarm inside Docker Desktop."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
NET = "iam-course-dind"
MANAGER = "iam-course-manager"
WORKER = "iam-course-worker"
IMAGE = "docker:29-dind"
LABEL = "org.iam-course.node=true"
STACK_SECRETS = ("db_password","admin_password","oidc_secret","cookie_secret",
                 "ca_cert","tls_cert","tls_key","iam-realm.json")

def docker(*args, capture=False, check=True, stdin=None):
    return subprocess.run(["docker", *args], cwd=ROOT, text=stdin is None,
                          capture_output=capture, check=check, stdin=stdin)

def ask(*args):
    return docker(*args, capture=True).stdout.strip()

def inner(node, *args, **kwargs):
    return docker("exec", node, "docker", *args, **kwargs)

def wait_engine(node):
    for _ in range(60):
        if inner(node,"info","--format","{{.ServerVersion}}",capture=True,check=False).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"Engine did not start on {node}")

def start_node(node, hostname, ports=()):
    existing=docker("container","inspect",node,capture=True,check=False)
    if existing.returncode:
        docker("run","-d","--privileged","--name",node,"--hostname",hostname,
               "--label",LABEL,"--network",NET,"-e","DOCKER_TLS_CERTDIR=",*ports,IMAGE)
    else:
        label=ask("inspect",node,"--format",'{{index .Config.Labels "org.iam-course.node"}}')
        if label != "true":
            raise RuntimeError(f"Container {node} already exists and lacks the lab label")
        docker("start",node)
    wait_engine(node)

def main():
    env=os.environ.copy()
    subprocess.run([sys.executable,"scripts/prepare.py"],cwd=ROOT,check=True,env=env)
    base=env.pop("IAM_IMAGE",None)
    if base:
        docker("pull",base)
    else:
        subprocess.run(["docker","compose","build","keycloak"],cwd=ROOT,check=True,env=env)
        base="iam-course/keycloak:local"
    image_id=ask("image","inspect",base,"--format","{{.Id}}").split(":",1)[1][:16]
    image=f"iam-course/keycloak:{image_id}"
    docker("tag",base,image)
    other=("postgres:17-alpine","nginx:1.28-alpine","quay.io/oauth2-proxy/oauth2-proxy:v7.15.5")
    for name in other:
        if docker("image","inspect",name,capture=True,check=False).returncode:
            docker("pull",name)
    if docker("network","inspect",NET,capture=True,check=False).returncode:
        docker("network","create",NET)
    start_node(MANAGER,"iam-manager",("-p","127.0.0.1:8443:8443"))
    start_node(WORKER,"iam-worker")
    manager_ip=ask("inspect",MANAGER,"--format","{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")
    def swarm_state(node):
        return inner(node,"info","--format","{{.Swarm.LocalNodeState}}",capture=True).stdout.strip()
    if swarm_state(MANAGER) == "inactive":
        inner(MANAGER,"swarm","init","--advertise-addr",manager_ip,capture=True)
    if swarm_state(WORKER) == "inactive":
        token=inner(MANAGER,"swarm","join-token","-q","worker",capture=True).stdout.strip()
        result=inner(WORKER,"swarm","join","--token",token,f"{manager_ip}:2377",capture=True,check=False)
        if result.returncode:
            raise RuntimeError("Worker could not join Swarm")
    inner(MANAGER,"node","update","--label-add","iam_db=true","iam-manager")
    with tempfile.TemporaryDirectory(prefix="iam-course-") as temporary:
        tar=Path(temporary)/"images.tar"
        docker("image","save","-o",str(tar),image,*other)
        for node in (MANAGER,WORKER):
            with tar.open("rb") as source:
                docker("exec","-i",node,"docker","load",stdin=source)
    docker("exec",MANAGER,"mkdir","-p","/opt/iam/.secrets")
    docker("cp","stack.yaml",f"{MANAGER}:/opt/iam/stack.yaml")
    docker("cp","config",f"{MANAGER}:/opt/iam/")
    for name in STACK_SECRETS:
        docker("cp",f".secrets/{name}",f"{MANAGER}:/opt/iam/.secrets/{name}")
    env.update(IAM_DIND=MANAGER,IAM_IMAGE=image,IAM_RESOLVE="never")
    subprocess.run([sys.executable,"scripts/deploy.py"],cwd=ROOT,check=True,env=env)

if __name__ == "__main__":
    main()

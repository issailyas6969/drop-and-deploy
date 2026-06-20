"""
LangGraph-based CI/CD workflow for auto-deploying to AWS EC2 on Git push.
"""

import os
import subprocess
import tarfile
import paramiko
from typing import TypedDict, Literal
from langgraph.graph import StateGraph, END


# ─────────────────────────────────────────
# State schema shared across all nodes
# ─────────────────────────────────────────
class DeployState(TypedDict):
    repo_path: str          # local path to the cloned repo
    branch: str             # branch that was pushed
    commit_sha: str         # latest commit SHA
    artifact_path: str      # path to the built tarball
    ec2_host: str           # EC2 public IP or hostname
    ec2_user: str           # SSH user (e.g. "ubuntu")
    ec2_key_path: str       # path to .pem key
    app_dir: str            # directory on EC2 where app lives
    restart_cmd: str        # command to restart the app on EC2
    status: str             # "success" | "failure"
    logs: list[str]         # running log of messages


# ─────────────────────────────────────────
# Node 1 — Validate
# ─────────────────────────────────────────
def validate(state: DeployState) -> DeployState:
    logs = state.get("logs", [])
    logs.append("🔍 [validate] Checking environment and config...")

    required = ["ec2_host", "ec2_user", "ec2_key_path", "repo_path"]
    missing = [k for k in required if not state.get(k)]

    if missing:
        logs.append(f"❌ [validate] Missing config keys: {missing}")
        return {**state, "status": "failure", "logs": logs}

    if not os.path.exists(state["ec2_key_path"]):
        logs.append(f"❌ [validate] PEM key not found: {state['ec2_key_path']}")
        return {**state, "status": "failure", "logs": logs}

    if not os.path.isdir(state["repo_path"]):
        logs.append(f"❌ [validate] Repo path not found: {state['repo_path']}")
        return {**state, "status": "failure", "logs": logs}

    logs.append("✅ [validate] All checks passed.")
    return {**state, "status": "success", "logs": logs}


# ─────────────────────────────────────────
# Node 2 — Build
# ─────────────────────────────────────────
def build(state: DeployState) -> DeployState:
    logs = state["logs"]
    logs.append("🔨 [build] Installing dependencies and running tests...")

    repo = state["repo_path"]

    # Install dependencies if requirements.txt exists
    req_file = os.path.join(repo, "requirements.txt")
    if os.path.exists(req_file):
        result = subprocess.run(
            ["pip", "install", "-r", req_file, "-q"],
            cwd=repo, capture_output=True, text=True
        )
        if result.returncode != 0:
            logs.append(f"❌ [build] pip install failed:\n{result.stderr}")
            return {**state, "status": "failure", "logs": logs}
        logs.append("✅ [build] Dependencies installed.")

    # Run tests if pytest is available
    test_result = subprocess.run(
        ["python", "-m", "pytest", "--tb=short", "-q"],
        cwd=repo, capture_output=True, text=True
    )
    if test_result.returncode != 0:
        logs.append(f"❌ [build] Tests failed:\n{test_result.stdout}\n{test_result.stderr}")
        return {**state, "status": "failure", "logs": logs}

    logs.append("✅ [build] Tests passed.")
    return {**state, "status": "success", "logs": logs}


# ─────────────────────────────────────────
# Node 3 — Package
# ─────────────────────────────────────────
def package(state: DeployState) -> DeployState:
    logs = state["logs"]
    logs.append("📦 [package] Creating deployment artifact...")

    repo = state["repo_path"]
    sha = state.get("commit_sha", "latest")[:8]
    artifact_path = f"/tmp/deploy_{sha}.tar.gz"

    # Exclude common junk
    EXCLUDE = {".git", "__pycache__", ".env", "node_modules", "*.pyc", ".DS_Store"}

    def exclude_filter(tarinfo):
        for pattern in EXCLUDE:
            if pattern.lstrip("*") in tarinfo.name:
                return None
        return tarinfo

    try:
        with tarfile.open(artifact_path, "w:gz") as tar:
            tar.add(repo, arcname="app", filter=exclude_filter)
        logs.append(f"✅ [package] Artifact created: {artifact_path}")
        return {**state, "artifact_path": artifact_path, "status": "success", "logs": logs}
    except Exception as e:
        logs.append(f"❌ [package] Failed to create artifact: {e}")
        return {**state, "status": "failure", "logs": logs}


# ─────────────────────────────────────────
# Node 4 — Deploy
# ─────────────────────────────────────────
def deploy(state: DeployState) -> DeployState:
    logs = state["logs"]
    logs.append(f"🚀 [deploy] Connecting to EC2 at {state['ec2_host']}...")

    host = state["ec2_host"]
    user = state["ec2_user"]
    key_path = state["ec2_key_path"]
    artifact = state["artifact_path"]
    app_dir = state.get("app_dir", "/home/ubuntu/app")
    restart_cmd = state.get("restart_cmd", "sudo systemctl restart myapp")

    try:
        key = paramiko.RSAKey.from_private_key_file(key_path)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host, username=user, pkey=key, timeout=30)

        # Upload artifact via SFTP
        sftp = ssh.open_sftp()
        remote_artifact =f"/tmp/remote_{os.path.basename(artifact)}"
        logs.append(f"📤 [deploy] Uploading artifact to {remote_artifact}...")
        sftp.put(artifact, remote_artifact)
        sftp.close()

        # Extract and restart
        commands = [
            f"mkdir -p {app_dir}",
            f"tar -xzf {remote_artifact} -C {app_dir} --strip-components=1",
            f"rm {remote_artifact}",
            restart_cmd,
        ]

        for cmd in commands:
            logs.append(f"  ▶ {cmd}")
            stdin, stdout, stderr = ssh.exec_command(cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode()
                logs.append(f"❌ [deploy] Command failed (exit {exit_code}): {err}")
                ssh.close()
                return {**state, "status": "failure", "logs": logs}

        ssh.close()
        logs.append("✅ [deploy] Deployment complete!")
        return {**state, "status": "success", "logs": logs}

    except Exception as e:
        logs.append(f"❌ [deploy] SSH/deploy error: {e}")
        return {**state, "status": "failure", "logs": logs}


# ─────────────────────────────────────────
# Node 5 — Notify
# ─────────────────────────────────────────
def notify(state: DeployState) -> DeployState:
    logs = state["logs"]
    status = state["status"]
    sha = state.get("commit_sha", "unknown")[:8]
    branch = state.get("branch", "unknown")

    if status == "success":
        msg = f"🎉 [notify] Deployment SUCCEEDED — branch={branch} sha={sha} host={state['ec2_host']}"
    else:
        msg = f"🔥 [notify] Deployment FAILED — branch={branch} sha={sha}. Check logs above."

    logs.append(msg)
    print("\n" + "=" * 60)
    for line in logs:
        print(line)
    print("=" * 60 + "\n")

    return {**state, "logs": logs}


# ─────────────────────────────────────────
# Conditional edge — stop on failure
# ─────────────────────────────────────────
def should_continue(state: DeployState) -> Literal["continue", "abort"]:
    return "continue" if state["status"] == "success" else "abort"


# ─────────────────────────────────────────
# Build the LangGraph
# ─────────────────────────────────────────
def build_graph():
    g = StateGraph(DeployState)

    g.add_node("validate", validate)
    g.add_node("build", build)
    g.add_node("package", package)
    g.add_node("deploy", deploy)
    g.add_node("notify", notify)

    g.set_entry_point("validate")

    # Each node checks status; abort goes straight to notify
    for src, dst in [("validate", "build"), ("build", "package"), ("package", "deploy")]:
        g.add_conditional_edges(src, should_continue, {"continue": dst, "abort": "notify"})

    g.add_edge("deploy", "notify")
    g.add_edge("notify", END)

    return g.compile()


deploy_graph = build_graph()

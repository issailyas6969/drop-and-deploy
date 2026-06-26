"""
upload_server.py — tiny Flask app that lets you drop in app.py files
and automatically deploys + starts each one on its own port on EC2,
without overwriting previous deployments.

Run with:  python upload_server.py
Then open: http://localhost:9000

Requires the same EC2 access you already set up for webhook_server.py
(same .pem key, same host).
"""

import os
import re
import json
import time
import subprocess
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=".")

# ── Config — reuse the same values as your webhook_server.py ──
EC2_HOST     = os.environ.get("EC2_HOST", "")
EC2_USER     = os.environ.get("EC2_USER", "ubuntu")
EC2_KEY_PATH = os.path.expanduser(os.environ.get("EC2_KEY_PATH", "~/.ssh/yoo.pem"))
REMOTE_BASE  = os.environ.get("UPLOAD_APP_DIR", "/home/ubuntu/uploaded_apps")
START_PORT   = int(os.environ.get("UPLOADED_APP_START_PORT", 5000))

# Registry file on EC2 that tracks every app that's been deployed:
# { "apps": [ {"name": "app-1", "port": 5000, "filename": "app.py"}, ... ] }
REGISTRY_PATH = f"{REMOTE_BASE}/registry.json"


def ssh_run(cmd, timeout=30):
    """Run a command on EC2 over SSH and return the completed process."""
    full_cmd = [
        "ssh", "-i", EC2_KEY_PATH, "-o", "StrictHostKeyChecking=no",
        f"{EC2_USER}@{EC2_HOST}", cmd
    ]
    return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)


def ssh_start_service(cmd, unit_name, timeout=15):
    """
    Start a long-lived process on EC2 as a transient systemd unit.
    This is the robust way to launch a fully independent process over SSH —
    unlike nohup/setsid/disown tricks, systemd-run detaches the process from
    the SSH session's cgroup entirely, so it survives the SSH connection
    closing and isn't reaped when later SSH sessions start or end.
    """
    wrapped = (
        f"sudo systemctl stop {unit_name} 2>/dev/null; "
        f"sudo systemd-run --unit={unit_name} --collect "
        f"bash -c '{cmd}'"
    )
    full_cmd = [
        "ssh", "-i", EC2_KEY_PATH, "-o", "StrictHostKeyChecking=no",
        f"{EC2_USER}@{EC2_HOST}", wrapped
    ]
    return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)


def scp_file(local_path, remote_path, timeout=30):
    full_cmd = [
        "scp", "-i", EC2_KEY_PATH, "-o", "StrictHostKeyChecking=no",
        local_path, f"{EC2_USER}@{EC2_HOST}:{remote_path}"
    ]
    return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)


def get_registry():
    """Fetch the current registry of deployed apps from EC2. Returns a list."""
    result = ssh_run(f"cat {REGISTRY_PATH} 2>/dev/null || echo '{{\"apps\": []}}'", timeout=15)
    try:
        data = json.loads(result.stdout.strip())
        return data.get("apps", [])
    except (json.JSONDecodeError, ValueError):
        return []


def save_registry(apps):
    """Write the registry back to EC2 via scp (avoids SSH quoting issues)."""
    data = json.dumps({"apps": apps}, indent=2)
    local_tmp = "/tmp/_registry_tmp.json"
    with open(local_tmp, "w") as f:
        f.write(data)
    ssh_run(f"mkdir -p {REMOTE_BASE}", timeout=15)
    scp_file(local_tmp, REGISTRY_PATH, timeout=15)


def next_slot(apps):
    """Compute the next app name and port given existing deployments."""
    used_ports = {a["port"] for a in apps}
    used_nums = []
    for a in apps:
        m = re.match(r"app-(\d+)$", a["name"])
        if m:
            used_nums.append(int(m.group(1)))

    n = (max(used_nums) + 1) if used_nums else 1
    name = f"app-{n}"

    port = START_PORT
    while port in used_ports:
        port += 1

    return name, port


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/apps", methods=["GET"])
def list_apps():
    """Return the current registry so the frontend can render live links."""
    if not EC2_HOST:
        return jsonify(apps=[])
    apps = get_registry()
    return jsonify(apps=apps)


@app.route("/deploy", methods=["POST"])
def deploy():
    log_lines = []

    if "file" not in request.files:
        return jsonify(success=False, log="No file received."), 400

    file = request.files["file"]
    if not file.filename.endswith(".py"):
        return jsonify(success=False, log="Only .py files are accepted."), 400

    if not EC2_HOST:
        return jsonify(success=False, log="EC2_HOST is not set on the server."), 500

    local_path = f"/tmp/{file.filename}"
    file.save(local_path)
    log_lines.append(f"Saved upload: {file.filename}")

    try:
        # 1. Work out which slot (name + port) this new app gets
        t0 = time.time()
        apps = get_registry()
        log_lines.append(f"[{time.time()-t0:.1f}s] read registry")
        name, port = next_slot(apps)
        remote_dir = f"{REMOTE_BASE}/{name}"
        log_lines.append(f"Assigned slot: {name} → port {port}")

        # 2. Create its own directory on EC2
        t0 = time.time()
        log_lines.append(f"Connecting to {EC2_HOST}...")
        mk = ssh_run(f"mkdir -p {remote_dir}", timeout=15)
        log_lines.append(f"[{time.time()-t0:.1f}s] mkdir")
        if mk.returncode != 0:
            raise RuntimeError(mk.stderr)

        # 3. Copy the file over into its own folder
        t0 = time.time()
        log_lines.append(f"Uploading {file.filename} to {remote_dir}/app.py...")
        cp = scp_file(local_path, f"{remote_dir}/app.py", timeout=15)
        log_lines.append(f"[{time.time()-t0:.1f}s] scp app.py")
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr)

        # 4. Make sure a shared venv with Flask exists (created once, reused
        #    by every app, so we don't reinstall Flask per app)
        t0 = time.time()
        log_lines.append("Checking environment on EC2...")
        venv_python = f"{REMOTE_BASE}/venv/bin/python3"
        setup_cmd = (
            f"cd {REMOTE_BASE} && "
            f"if [ ! -f {venv_python} ]; then "
            f"python3 -m venv venv && {venv_python} -m pip install --quiet flask; "
            f"fi"
        )
        setup = ssh_run(setup_cmd, timeout=90)
        log_lines.append(f"[{time.time()-t0:.1f}s] venv check/setup")
        if setup.returncode != 0:
            raise RuntimeError(setup.stderr)

        # 5. Kill anything already on this port (quick blocking call)
        t0 = time.time()
        ssh_run(f"fuser -k {port}/tcp || true", timeout=10)
        log_lines.append(f"[{time.time()-t0:.1f}s] free port {port}")

        # 6. Start the app on its assigned port as a transient systemd unit.
        #    This fully detaches it from the SSH session — it keeps running
        #    independent of any SSH connection, including ones that come later.
        t0 = time.time()
        log_lines.append(f"Starting {name} on port {port}...")
        unit_name = f"uploadedapp-{name}"
        inner_cmd = (
            f"cd {remote_dir} && PORT={port} {venv_python} app.py "
            f">> app.log 2>&1"
        )
        start = ssh_start_service(inner_cmd, unit_name, timeout=15)
        log_lines.append(f"[{time.time()-t0:.1f}s] start app")
        if start.returncode != 0:
            raise RuntimeError(start.stderr)

        # 7. Save this app into the registry
        t0 = time.time()
        apps.append({"name": name, "port": port, "filename": file.filename})
        save_registry(apps)
        log_lines.append(f"[{time.time()-t0:.1f}s] save registry")

        log_lines.append(f"✅ Deployed as {name}! Visit http://{EC2_HOST}:{port}")
        return jsonify(success=True, log="\n".join(log_lines), port=port, name=name)

    except subprocess.TimeoutExpired:
        log_lines.append("❌ Timed out talking to EC2.")
        return jsonify(success=False, log="\n".join(log_lines)), 500
    except Exception as e:
        log_lines.append(f"❌ Error: {e}")
        return jsonify(success=False, log="\n".join(log_lines)), 500


if __name__ == "__main__":
    print(f"\n🌐 Upload UI running at http://localhost:9000")
    print(f"   Deploying to: {EC2_USER}@{EC2_HOST or '(EC2_HOST not set)'}")
    print(f"   Remote base:  {REMOTE_BASE}")
    print(f"   Start port:   {START_PORT}")
    print(f"   Each upload gets its own folder + port automatically.\n")
    app.run(host="0.0.0.0", port=9000)

#!/usr/bin/env python3
"""
Wall-clock anchored Kaggle MC server.
Pushes a tiny bootstrap notebook that pulls the real code from GitHub.
"""

import os, sys, json, time, subprocess, tempfile, shutil, csv, io
from datetime import datetime, timezone

API_TOKEN = os.environ["KAGGLE_API_TOKEN"]
NOTEBOOK_SLUG = os.environ.get("NOTEBOOK_SLUG", "mc-server-scheduled")
DATASET_SLUG = os.environ.get("DATASET_SLUG", "system.pliki/mc-server-full-backup")
BOOT_WINDOW = int(os.environ.get("BOOT_WINDOW_SECONDS", "900"))
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. "system.pliki/kaggle-mc"
USERNAME = DATASET_SLUG.split("/")[0] if "/" in DATASET_SLUG else "user"


def run_cmd(cmd, **kwargs):
    env = {**os.environ, "KAGGLE_API_TOKEN": API_TOKEN}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kwargs)


def get_current_anchor():
    now = datetime.now(timezone.utc)
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return noon if now >= noon else midnight


def get_latest_session():
    try:
        r = run_cmd(["kaggle", "kernels", "list", "--user", USERNAME,
                      "--page-size", "10", "--sort-by", "dateCreated", "-v"])
        if r.returncode != 0:
            print(f"⚠️  List failed: {r.stderr[:200]}")
            return None, None, None
        lines = r.stdout.strip().split("\n")
        if len(lines) < 2:
            return None, None, None
        reader = csv.DictReader(io.StringIO(r.stdout))
        for row in reader:
            slug = row.get("slug", "")
            title = row.get("title", "").lower().replace(" ", "-")
            if NOTEBOOK_SLUG in slug or NOTEBOOK_SLUG in title:
                created_str = row.get("creationDate", row.get("lastRunTime", ""))
                dt = None
                if created_str:
                    try:
                        cs = created_str.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(cs)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                status = row.get("lastRunStatus", row.get("status", "unknown")).lower()
                ref = row.get("ref", slug)
                return status, ref, dt
    except Exception as e:
        print(f"⚠️  List error: {e}")
    return None, None, None


def cleanup_old_sessions(keep=2):
    try:
        r = run_cmd(["kaggle", "kernels", "list", "--user", USERNAME,
                      "--page-size", "50", "--sort-by", "dateCreated", "-v"])
        if r.returncode != 0:
            return
        reader = csv.DictReader(io.StringIO(r.stdout))
        matching = []
        for row in reader:
            slug = row.get("slug", "")
            title = row.get("title", "").lower().replace(" ", "-")
            if NOTEBOOK_SLUG in slug or NOTEBOOK_SLUG in title:
                matching.append(row)
        to_delete = matching[keep:]
        for row in to_delete:
            ref = row.get("ref", row.get("slug", ""))
            dr = run_cmd(["kaggle", "kernels", "delete", ref, "-y"])
            if dr.returncode == 0:
                print(f"  🗑️  Deleted: {row.get('title')}")
            else:
                print(f"  ⚠️  Delete failed {ref}: {dr.stderr[:100]}")
        if to_delete:
            print(f"  Cleaned up {len(to_delete)} old sessions, kept {keep}")
    except Exception as e:
        print(f"  ⚠️  Cleanup error: {e}")


def create_session(anchor_label):
    """Push a tiny bootstrap notebook that pulls real code from GitHub."""
    ts = anchor_label
    kernel_id = f"{USERNAME}/{NOTEBOOK_SLUG}-{ts}"

    # This is the ONLY code in the notebook — just a bootstrap
    # It downloads the real scripts from GitHub raw URLs
    bootstrap_lines = [
        "import subprocess, os, sys",
        "",
        "# Set environment variables",
        f'os.environ["KAGGLE_API_TOKEN"] = "{API_TOKEN}"',
        f'os.environ["PLAYIT_AUTH_TOKEN"] = "{os.environ.get("PLAYIT_AUTH_TOKEN", "")}"',
        f'os.environ["MINECRAFT_VERSION"] = "{os.environ.get("MINECRAFT_VERSION", "1.21.1")}"',
        f'os.environ["SERVER_MOTD"] = "{os.environ.get("SERVER_MOTD", "Martin Kaggle MC Server")}"',
        f'os.environ["MAX_PLAYERS"] = "{os.environ.get("MAX_PLAYERS", "20")}"',
        f'os.environ["DATASET_SLUG"] = "{DATASET_SLUG}"',
        f'os.environ["MC_DIR"] = "/kaggle/working/mc-server"',
        f'os.environ["SYNC_ROOT"] = "/kaggle/working/.mc_dataset_sync"',
        f'os.environ["SYNC_INTERVAL_SECONDS"] = "120"',
        f'os.environ["JAVA_MEM"] = "24G"',
        "",
        "# Download bootstrap script from GitHub",
        f'REPO = "{GITHUB_REPO}"',
        'BRANCH = "main"',
        'RAW = f"https://raw.githubusercontent.com/{{REPO}}/{{BRANCH}}"',
        "",
        'subprocess.run(["apt-get", "update", "-qq"], capture_output=True)',
        'subprocess.run(["apt-get", "install", "-y", "-qq", "curl"], capture_output=True)',
        "",
        '# Download and run the startup script',
        'subprocess.run(["curl", "-sL", f"{{RAW}}/scripts/mc_startup.sh", "-o", "/kaggle/working/mc_startup.sh"], check=True)',
        'os.chmod("/kaggle/working/mc_startup.sh", 0o755)',
        "",
        '# Download sync daemon',
        'subprocess.run(["curl", "-sL", f"{{RAW}}/scripts/mc_sync_daemon.py", "-o", "/kaggle/working/mc_sync_daemon.py"], check=True)',
        "",
        '# Run startup',
        'proc = subprocess.Popen(',
        '    ["/bin/bash", "/kaggle/working/mc_startup.sh"],',
        '    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,',
        '    text=True, env=os.environ.copy(), start_new_session=True',
        ')',
        'for line in proc.stdout:',
        '    print(line.rstrip())',
        '    if "STARTUP_COMPLETE" in line: break',
        'while True:',
        '    if proc.poll() is not None: print("SERVER_DIED"); sys.exit(1)',
        '    import time; time.sleep(60)',
    ]

    notebook = {
        "cells": [{"cell_type": "code", "execution_count": None, "metadata": {},
                    "outputs": [], "source": bootstrap_lines}],
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                      "name": "python3"},
                      "language_info": {"name": "python", "version": "3.10.0"}},
        "nbformat": 4, "nbformat_minor": 5
    }

    metadata = {
        "id": kernel_id,
        "title": f"{NOTEBOOK_SLUG}-{ts}",
        "code_file": f"{NOTEBOOK_SLUG}.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": False,
        "enable_internet": True,
        "dataset_sources": [DATASET_SLUG],
        "kernel_sources": [],
        "competition_sources": []
    }

    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, f"{NOTEBOOK_SLUG}.ipynb"), "w") as f:
            json.dump(notebook, f)
        with open(os.path.join(tmpdir, "kernel-metadata.json"), "w") as f:
            json.dump(metadata, f)

        r = run_cmd(["kaggle", "kernels", "push", "-p", tmpdir])
        if r.returncode != 0:
            raise Exception(f"kaggle push failed: {r.stderr[:500]} {r.stdout[:500]}")

        print(f"  Push output: {r.stdout.strip()}")
        url = f"https://www.kaggle.com/code/{USERNAME}/{NOTEBOOK_SLUG}-{ts}"
        return kernel_id, url
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def write_summary(msg):
    gha = os.environ.get("GITHUB_STEP_SUMMARY")
    if gha:
        with open(gha, "a") as f:
            f.write(msg + "\n")


def main():
    print("Installing kaggle CLI...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "kaggle"], check=True)

    kaggle_dir = os.path.expanduser("~/.kaggle")
    os.makedirs(kaggle_dir, exist_ok=True)
    with open(os.path.join(kaggle_dir, "access_token"), "w") as f:
        f.write(API_TOKEN)
    os.chmod(os.path.join(kaggle_dir, "access_token"), 0o600)

    now = datetime.now(timezone.utc)
    anchor = get_current_anchor()
    anchor_label = anchor.strftime("%Y%m%d-%H%M")
    secs_since_anchor = (now - anchor).total_seconds()

    print(f"[{now.isoformat()}] Anchor: {anchor.isoformat()} ({secs_since_anchor:.0f}s ago)")

    status, ref, created_dt = get_latest_session()

    if created_dt and status == "running":
        age_at_anchor = abs((created_dt - anchor).total_seconds())
        if age_at_anchor <= BOOT_WINDOW:
            msg = f"✅ Session running for this anchor ({(now-created_dt).total_seconds()/60:.0f}min ago) — SKIP"
            print(msg); write_summary(msg); return

    if status == "running" and created_dt:
        age = (now - created_dt).total_seconds()
        if age < BOOT_WINDOW:
            msg = f"✅ Session running ({age/60:.0f}min old) — SKIP"
            print(msg); write_summary(msg); return

    reason = f"status={status}" if status else "no session found"
    msg = f"🔄 Creating session for anchor {anchor_label} ({reason})"
    print(msg); write_summary(msg)

    cleanup_old_sessions(keep=2)

    try:
        new_ref, new_url = create_session(anchor_label)
        print(f"  ✅ Created: {new_url}")
        time.sleep(30)
        new_status, _, _ = get_latest_session()
        print(f"  Status: {new_status}")
        write_summary(f"  ✅ [{new_url}]({new_url}) | Status: {new_status}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")
        write_summary(f"❌ FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

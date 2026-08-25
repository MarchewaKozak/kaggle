#!/usr/bin/env python3
"""
Wall-clock anchored Kaggle MC server with auto-cleanup.
Uses the CORRECT /api/v1/kernels/push endpoint.
"""

import os, sys, json, time, textwrap, requests, tempfile, shutil
from datetime import datetime, timezone

USERNAME = os.environ["KAGGLE_USERNAME"]
KEY = os.environ["KAGGLE_KEY"]
AUTH = (USERNAME, KEY)
BASE = "https://www.kaggle.com/api/v1"
NOTEBOOK_SLUG = os.environ.get("NOTEBOOK_SLUG", "mc-server-scheduled")
DATASET_SLUG = os.environ.get("DATASET_SLUG", f"{USERNAME}/mc-server-full-backup")
BOOT_WINDOW = int(os.environ.get("BOOT_WINDOW_SECONDS", "900"))


def api(method, endpoint, **kwargs):
    resp = requests.request(method, f"{BASE}/{endpoint}", auth=AUTH, timeout=120, **kwargs)
    return resp


def get_current_anchor():
    now = datetime.now(timezone.utc)
    noon_today = now.replace(hour=12, minute=0, second=0, microsecond=0)
    midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now >= noon_today:
        return noon_today
    else:
        return midnight_today


def get_latest_session():
    try:
        resp = api("GET", "kernels/list", params={
            "user": USERNAME, "page": 1, "pageSize": 10, "sortBy": "dateCreated"
        })
        if resp.status_code != 200:
            return None, None, None
        for k in resp.json().get("kernels", []):
            slug = k.get("slug", "")
            title = k.get("title", "").lower().replace(" ", "-")
            if NOTEBOOK_SLUG in slug or NOTEBOOK_SLUG in title:
                created_str = k.get("creationDate", "")
                dt = None
                if created_str:
                    try:
                        cs = created_str.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(cs)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                return k.get("status"), k.get("ref"), dt
    except Exception as e:
        print(f"⚠️  List error: {e}")
    return None, None, None


def cleanup_old_sessions(keep=2):
    try:
        resp = api("GET", "kernels/list", params={
            "user": USERNAME, "page": 1, "pageSize": 50, "sortBy": "dateCreated"
        })
        if resp.status_code != 200:
            return
        kernels = resp.json().get("kernels", [])
        matching = [
            k for k in kernels
            if NOTEBOOK_SLUG in k.get("slug", "")
            or NOTEBOOK_SLUG in k.get("title", "").lower().replace(" ", "-")
        ]
        to_delete = matching[keep:]
        for k in to_delete:
            ref = k.get("ref")
            try:
                api("DELETE", f"kernels/delete/{ref}")
                print(f"  🗑️  Deleted old session: {k.get('title')}")
            except Exception as e:
                print(f"  ⚠️  Failed to delete {ref}: {e}")
        if to_delete:
            print(f"  Cleaned up {len(to_delete)} old sessions, kept {keep}")
    except Exception as e:
        print(f"  ⚠️  Cleanup error: {e}")


def build_server_code():
    """Return the full Python code that runs inside the Kaggle notebook."""
    env_lines = "\n".join([
        f'os.environ["{k}"] = "{v}"'
        for k, v in {
            "KAGGLE_USERNAME": USERNAME,
            "KAGGLE_KEY": KEY,
            "PLAYIT_AUTH_TOKEN": os.environ.get("PLAYIT_AUTH_TOKEN", ""),
            "MINECRAFT_VERSION": os.environ.get("MINECRAFT_VERSION", "1.21.1"),
            "SERVER_MOTD": os.environ.get("SERVER_MOTD", "Martin Kaggle MC Server"),
            "MAX_PLAYERS": os.environ.get("MAX_PLAYERS", "20"),
            "DATASET_SLUG": DATASET_SLUG,
        }.items()
    ])

    server_code = textwrap.dedent(r'''
import os,sys,time,json,shutil,subprocess,pathlib,textwrap,signal

subprocess.run([sys.executable,"-m","pip","install","-q","--upgrade","kaggle"],capture_output=True)
subprocess.run(["apt-get","update","-qq"],capture_output=True)
subprocess.run(["apt-get","install","-y","-qq","unzip","curl","jq"],capture_output=True,env={**os.environ,"DEBIAN_FRONTEND":"noninteractive"})

kd=pathlib.Path.home()/".kaggle";kd.mkdir(parents=True,exist_ok=True)
(kd/"kaggle.json").write_text(json.dumps({"username":os.environ["KAGGLE_USERNAME"],"key":os.environ["KAGGLE_KEY"]}));(kd/"kaggle.json").chmod(0o600)

SYNC_DAEMON=r"""
import os,time,shutil,subprocess,json,pathlib,fcntl,signal,sys
from datetime import datetime,timezone

os.environ["PATH"]+=":/root/.local/bin:/usr/local/bin"
KBIN=shutil.which("kaggle") or "kaggle"
MC=pathlib.Path(os.environ["MC_DIR"])
SR=pathlib.Path(os.environ["SYNC_ROOT"])
STAGE=SR/"stage";LOCK=SR/"sync.lock";LOG=SR/"sync_daemon.log"
DS=os.environ["DATASET_SLUG"];INT=max(10,int(os.environ.get("SYNC_INTERVAL_SECONDS","120")))
FIFO=MC/"stdin.fifo";SHUTDOWN_FLAG=SR/"shutdown_requested"

def log(m):
 l=f"[{datetime.now(timezone.utc).isoformat()}] {m}";print(l,flush=True)
 try:LOG.parent.mkdir(parents=True,exist_ok=True);open(LOG,"a").write(l+"\n")
 except:pass

def mc_cmd(c):
 try:fd=os.open(FIFO,os.O_WRONLY|os.O_NONBLOCK);os.write(fd,(c+"\n").encode());os.close(fd);return True
 except:return False

def safe_save():
 log("🔒 Ordered save: save-off → save-all flush → confirm")
 mc_cmd("save-off");time.sleep(2)
 mc_cmd("save-all flush")
 log_path=MC/"logs"/"latest.log";deadline=time.time()+30;flushed=False
 while time.time()<deadline:
  try:
   if log_path.exists():
    lines=log_path.read_text(errors="ignore").split("\n")
    if any("Saved the game" in l for l in lines[-20:]):flushed=True;break
  except:pass
  time.sleep(1)
 log("✅ Flush confirmed" if flushed else "⚠️  Flush unconfirmed, proceeding")
 time.sleep(3);return flushed

def mirror(s,d):
 if d.exists():shutil.rmtree(d)
 d.mkdir(parents=True)
 for r,ds,fs in os.walk(s):
  rp=pathlib.Path(r);rel=rp.relative_to(s);dr=d/rel;dr.mkdir(parents=True,exist_ok=True)
  for n in ds:
   src=rp/n;dst=dr/n
   if src.is_symlink():
    t=src.resolve()
    if t.is_dir()and not dst.exists():shutil.copytree(t,dst)
    elif t.is_file():shutil.copy2(t,dst)
   else:dst.mkdir(exist_ok=True)
  for n in fs:
   src=rp/n;dst=dr/n
   try:
    if src.is_symlink():
     t=src.resolve()
     if t.is_file():shutil.copy2(t,dst)
     elif t.is_dir():shutil.copytree(t,dst)
    elif src.is_file():shutil.copy2(src,dst)
   except Exception as e:log(f"Copy fail {src}: {e}")

def upload(msg):
 res=[];ms=STAGE/"mc-server"
 if ms.exists():
  for f in ms.rglob("*"):
   if f.is_file():res.append({"path":f.relative_to(STAGE).as_posix()})
 meta={"title":"MC Server Full Backup","id":DS,"licenses":[{"name":"CC0-1.0"}],"resources":res}
 (STAGE/"datapackage.json").write_text(json.dumps(meta,indent=2))
 cmds=[[KBIN,"datasets","version","-d",DS,"-p",str(STAGE),"-m",msg],
       [KBIN,"datasets","create","-p",str(STAGE)],
       [KBIN,"datasets","version","-d",DS,"-p",str(STAGE),"-m",msg,"--dir-mode","zip"],
       [KBIN,"datasets","create","-p",str(STAGE),"--dir-mode","zip"]]
 for c in cmds:
  r=subprocess.run(c,capture_output=True,text=True)
  if r.returncode==0:log("✅ Upload OK");return True
  log(f"Cmd fail: {r.stderr[:200]}")
 return False

def sync_once():
 if not DS:return
 LOCK.parent.mkdir(parents=True,exist_ok=True)
 with open(LOCK,"w") as lf:
  try:fcntl.flock(lf,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:log("Skip: prev sync running");return
  try:
   if STAGE.exists():shutil.rmtree(STAGE)
   STAGE.mkdir(parents=True)
   if MC.exists():
    safe_save()
    try:mirror(MC,STAGE/"mc-server")
    finally:mc_cmd("save-on")
   else:(STAGE/"mc-server").mkdir(parents=True,exist_ok=True)
   upload(f"Backup {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
  except Exception as e:log(f"Sync err: {e}")
  finally:fcntl.flock(lf,fcntl.LOCK_UN)

def handle_shutdown(signum,frame):
 log("🛑 Shutdown signal — final safe save")
 SHUTDOWN_FLAG.touch()
 try:
  if MC.exists():
   safe_save()
   if STAGE.exists():shutil.rmtree(STAGE)
   STAGE.mkdir(parents=True);mirror(MC,STAGE/"mc-server")
   upload(f"FINAL {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
 except Exception as e:log(f"Shutdown err: {e}")
 log("💾 Final save done");sys.exit(0)

signal.signal(signal.SIGTERM,handle_shutdown)
signal.signal(signal.SIGINT,handle_shutdown)
log(f"Daemon: interval={INT}s ds={DS}");time.sleep(20)
while not SHUTDOWN_FLAG.exists():sync_once();time.sleep(INT)
"""
pathlib.Path("/kaggle/working/mc_sync_daemon.py").write_text(SYNC_DAEMON)

STARTUP=r"""#!/bin/bash
set -uo pipefail
export MC_DIR="${MC_DIR:-/kaggle/working/mc-server}"
export JAVA_HOME="/kaggle/working/.java25"
export PATH="$JAVA_HOME/bin:$PATH"
export SYNC_ROOT="${SYNC_ROOT:-/kaggle/working/.mc_dataset_sync}"
mkdir -p "$MC_DIR" "$SYNC_ROOT";cd "$MC_DIR"

if [ ! -x "$JAVA_HOME/bin/java" ]; then
 for V in 25 24 21; do rm -rf "$JAVA_HOME";mkdir -p "$JAVA_HOME"
  if curl -fsSL "https://api.adoptium.net/v3/binary/latest/${V}/ga/linux/x64/jdk/hotspot/normal/eclipse?project=jdk" -o /tmp/j.tar.gz&&\
     tar -xzf /tmp/j.tar.gz -C "$JAVA_HOME" --strip-components=1&&\
     "$JAVA_HOME/bin/java" -version >/dev/null 2>&1;then echo "Java $V installed";break;fi;done;fi
"$JAVA_HOME/bin/java" -version

DN="${DATASET_SLUG##*/}";ID="/kaggle/input/$DN"
if [ -d "$ID" ];then
 [ -d "$ID/mc-server" ]&&cp -a "$ID/mc-server"/. "$MC_DIR"/||cp -a "$ID"/. "$MC_DIR"/
 find "$MC_DIR" -maxdepth 3 -name '*.zip' -exec unzip -qo {} -d "$MC_DIR" \; 2>/dev/null||true;fi

[ ! -f server.jar ]&&python3 -c "
import json,os,urllib.request
v=os.environ.get('MINECRAFT_VERSION','1.21.1')
m=json.load(urllib.request.urlopen('https://launchermeta.mojang.com/mc/game/version_manifest.json'))
u=next(x for x in m['versions'] if x['id']==v)['url']
d=json.load(urllib.request.urlopen(u))
urllib.request.urlretrieve(d['downloads']['server']['url'],'server.jar');print('Downloaded',v)"

cat > server.properties <<EOF
server-port=25565
motd=${SERVER_MOTD:-Kaggle MC}
max-players=${MAX_PLAYERS:-20}
view-distance=12
simulation-distance=8
online-mode=true
enable-rcon=false
EOF
echo "eula=true">eula.txt

if [ ! -x ./playit ];then
 PU=$(curl -fsSL https://api.github.com/repos/playit-cloud/playit-agent/releases/latest|\
  python3 -c "import sys,json;r=json.load(sys.stdin);u=[a['browser_download_url'] for a in r.get('assets',[]) if 'linux' in a['name'].lower() and ('x86_64' in a['name'].lower() or 'amd64' in a['name'].lower())];print(u[0] if u else '')")
 [ -z "$PU" ]&&PU="https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-linux-x86_64"
 curl -L "$PU" -o playit&&chmod +x playit;fi
rm -f /kaggle/working/playit.log
if [ -n "${PLAYIT_AUTH_TOKEN:-}" ];then
 bash -c './playit --auth-token "$0" >/kaggle/working/playit.log 2>&1||./playit >>/kaggle/working/playit.log 2>&1' "$PLAYIT_AUTH_TOKEN"&
else ./playit >/kaggle/working/playit.log 2>&1&fi

rm -f stdin.fifo;mkfifo stdin.fifo;sleep infinity > stdin.fifo &
"$JAVA_HOME/bin/java" -Xms${JAVA_MEM:-24G} -Xmx${JAVA_MEM:-24G} -XX:+UseG1GC \
 -jar server.jar nogui < stdin.fifo > /kaggle/working/minecraft_server.log 2>&1 &
MC_PID=$!

python3 /kaggle/working/mc_sync_daemon.py > "$SYNC_ROOT/daemon.log" 2>&1 &
SYNC_PID=$!

cleanup() {
 echo "🛑 Shutdown trap";kill -TERM "$SYNC_PID" 2>/dev/null;wait "$SYNC_PID" 2>/dev/null
 echo "save-off">stdin.fifo 2>/dev/null;sleep 2
 echo "save-all flush">stdin.fifo 2>/dev/null;sleep 10
 echo "stop">stdin.fifo 2>/dev/null;sleep 5
 kill "$MC_PID" 2>/dev/null;wait 2>/dev/null;echo "✅ Clean shutdown"
}
trap cleanup SIGTERM SIGINT EXIT

echo "STARTUP_COMPLETE"
wait "$MC_PID"
"""
sp=pathlib.Path("/kaggle/working/start_mc.sh");sp.write_text(STARTUP);sp.chmod(0o755)
proc=subprocess.Popen(["/bin/bash","/kaggle/working/start_mc.sh"],
 stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=os.environ.copy(),start_new_session=True)
for line in proc.stdout:
 print(line.rstrip())
 if "STARTUP_COMPLETE" in line:break
while True:
 if proc.poll() is not None:print("SERVER_DIED");sys.exit(1)
 time.sleep(60)
''')

    return f"import os\n{env_lines}\n\n{server_code}"


def create_session(anchor_label):
    """
    Uses the CORRECT Kaggle API: POST /api/v1/kernels/push
    This requires writing a temporary folder with kernel-metadata.json + .ipynb
    then calling the push endpoint, exactly like `kaggle kernels push` does.
    """
    ts = anchor_label
    kernel_id = f"{USERNAME}/{NOTEBOOK_SLUG}-{ts}"

    # Build notebook JSON
    code = build_server_code()
    notebook = {
        "cells": [{"cell_type": "code", "execution_count": None, "metadata": {},
                    "outputs": [], "source": code.split("\n")}],
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                      "name": "python3"},
                      "language_info": {"name": "python", "version": "3.10.0"}},
        "nbformat": 4, "nbformat_minor": 5
    }

    # kernel-metadata.json (required by /kernels/push)
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

    # Write to temp directory and push via API
    tmpdir = tempfile.mkdtemp()
    try:
        nb_path = os.path.join(tmpdir, f"{NOTEBOOK_SLUG}.ipynb")
        meta_path = os.path.join(tmpdir, "kernel-metadata.json")

        with open(nb_path, "w") as f:
            json.dump(notebook, f)
        with open(meta_path, "w") as f:
            json.dump(metadata, f)

        # Push using multipart form data (same as kaggle CLI)
        with open(nb_path, "rb") as nb_file, open(meta_path, "rb") as meta_file:
            files = {
                "file": (f"{NOTEBOOK_SLUG}.ipynb", nb_file, "application/json"),
                "metadata": ("kernel-metadata.json", meta_file, "application/json"),
            }
            resp = requests.post(
                f"{BASE}/kernels/push",
                auth=AUTH,
                files=files,
                timeout=120
            )

        if resp.status_code != 200:
            raise Exception(f"Push failed ({resp.status_code}): {resp.text[:500]}")

        result = resp.json()
        ref = result.get("ref", kernel_id)
        url = result.get("url", f"https://www.kaggle.com/code/{USERNAME}/{NOTEBOOK_SLUG}-{ts}")
        return ref, url

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def write_summary(msg):
    gha = os.environ.get("GITHUB_STEP_SUMMARY")
    if gha:
        with open(gha, "a") as f:
            f.write(msg + "\n")


def main():
    now = datetime.now(timezone.utc)
    anchor = get_current_anchor()
    anchor_label = anchor.strftime("%Y%m%d-%H%M")
    secs_since_anchor = (now - anchor).total_seconds()

    print(f"[{now.isoformat()}] Anchor: {anchor.isoformat()} ({secs_since_anchor:.0f}s ago)")

    status, ref, created_dt = get_latest_session()

    if created_dt and status == "running":
        age_at_anchor = abs((created_dt - anchor).total_seconds())
        if age_at_anchor <= BOOT_WINDOW:
            msg = f"✅ Session already running for this anchor (started {(now-created_dt).total_seconds()/60:.0f}min ago) — SKIP"
            print(msg)
            write_summary(msg)
            return

    if status == "running" and created_dt:
        age = (now - created_dt).total_seconds()
        if age < BOOT_WINDOW:
            msg = f"✅ Session running ({age/60:.0f}min old) — SKIP"
            print(msg)
            write_summary(msg)
            return

    reason = f"status={status}" if status else "no session found"
    msg = f"🔄 Creating session for anchor {anchor_label} ({reason})"
    print(msg)
    write_summary(msg)

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

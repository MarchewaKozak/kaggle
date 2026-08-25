import os, time, shutil, subprocess, json, pathlib, fcntl, signal, sys
from datetime import datetime, timezone

os.environ["PATH"] += ":/root/.local/bin:/usr/local/bin"
KBIN = shutil.which("kaggle") or "kaggle"
MC = pathlib.Path(os.environ["MC_DIR"])
SR = pathlib.Path(os.environ["SYNC_ROOT"])
STAGE = SR / "stage"
LOCK = SR / "sync.lock"
LOG = SR / "sync_daemon.log"
DS = os.environ["DATASET_SLUG"]
INT = max(10, int(os.environ.get("SYNC_INTERVAL_SECONDS", "120")))
FIFO = MC / "stdin.fifo"
SHUTDOWN_FLAG = SR / "shutdown_requested"

def log(m):
    l = f"[{datetime.now(timezone.utc).isoformat()}] {m}"
    print(l, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        open(LOG, "a").write(l + "\n")
    except:
        pass

def mc_cmd(c):
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, (c + "\n").encode())
        os.close(fd)
        return True
    except:
        return False

def safe_save():
    log("🔒 Ordered save: save-off → save-all flush → confirm")
    mc_cmd("save-off")
    time.sleep(2)
    mc_cmd("save-all flush")
    log_path = MC / "logs" / "latest.log"
    deadline = time.time() + 30
    flushed = False
    while time.time() < deadline:
        try:
            if log_path.exists():
                lines = log_path.read_text(errors="ignore").split("\n")
                if any("Saved the game" in l for l in lines[-20:]):
                    flushed = True
                    break
        except:
            pass
        time.sleep(1)
    log("✅ Flush confirmed" if flushed else "⚠️  Flush unconfirmed, proceeding")
    time.sleep(3)
    return flushed

def mirror(s, d):
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for r, ds, fs in os.walk(s):
        rp = pathlib.Path(r)
        rel = rp.relative_to(s)
        dr = d / rel
        dr.mkdir(parents=True, exist_ok=True)
        for n in ds:
            src = rp / n
            dst = dr / n
            if src.is_symlink():
                t = src.resolve()
                if t.is_dir() and not dst.exists():
                    shutil.copytree(t, dst)
                elif t.is_file():
                    shutil.copy2(t, dst)
            else:
                dst.mkdir(exist_ok=True)
        for n in fs:
            src = rp / n
            dst = dr / n
            try:
                if src.is_symlink():
                    t = src.resolve()
                    if t.is_file():
                        shutil.copy2(t, dst)
                    elif t.is_dir():
                        shutil.copytree(t, dst)
                elif src.is_file():
                    shutil.copy2(src, dst)
            except Exception as e:
                log(f"Copy fail {src}: {e}")

def upload(msg):
    res = []
    ms = STAGE / "mc-server"
    if ms.exists():
        for f in ms.rglob("*"):
            if f.is_file():
                res.append({"path": f.relative_to(STAGE).as_posix()})
    meta = {"title": "MC Server Full Backup", "id": DS,
            "licenses": [{"name": "CC0-1.0"}], "resources": res}
    (STAGE / "datapackage.json").write_text(json.dumps(meta, indent=2))
    cmds = [
        [KBIN, "datasets", "version", "-d", DS, "-p", str(STAGE), "-m", msg],
        [KBIN, "datasets", "create", "-p", str(STAGE)],
        [KBIN, "datasets", "version", "-d", DS, "-p", str(STAGE), "-m", msg, "--dir-mode", "zip"],
        [KBIN, "datasets", "create", "-p", str(STAGE), "--dir-mode", "zip"],
    ]
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode == 0:
            log("✅ Upload OK")
            return True
        log(f"Cmd fail: {r.stderr[:200]}")
    return False

def sync_once():
    if not DS:
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Skip: prev sync running")
            return
        try:
            if STAGE.exists():
                shutil.rmtree(STAGE)
            STAGE.mkdir(parents=True)
            if MC.exists():
                safe_save()
                try:
                    mirror(MC, STAGE / "mc-server")
                finally:
                    mc_cmd("save-on")
            else:
                (STAGE / "mc-server").mkdir(parents=True, exist_ok=True)
            upload(f"Backup {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        except Exception as e:
            log(f"Sync err: {e}")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

def handle_shutdown(signum, frame):
    log("🛑 Shutdown signal — final safe save")
    SHUTDOWN_FLAG.touch()
    try:
        if MC.exists():
            safe_save()
            if STAGE.exists():
                shutil.rmtree(STAGE)
            STAGE.mkdir(parents=True)
            mirror(MC, STAGE / "mc-server")
            upload(f"FINAL {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    except Exception as e:
        log(f"Shutdown err: {e}")
    log("💾 Final save done")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)
log(f"Daemon: interval={INT}s ds={DS}")
time.sleep(20)
while not SHUTDOWN_FLAG.exists():
    sync_once()
    time.sleep(INT)

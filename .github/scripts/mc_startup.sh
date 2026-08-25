#!/bin/bash
set -uo pipefail
export MC_DIR="${MC_DIR:-/kaggle/working/mc-server}"
export JAVA_HOME="/kaggle/working/.java25"
export PATH="$JAVA_HOME/bin:$PATH"
export SYNC_ROOT="${SYNC_ROOT:-/kaggle/working/.mc_dataset_sync}"
mkdir -p "$MC_DIR" "$SYNC_ROOT"
cd "$MC_DIR"

if [ ! -x "$JAVA_HOME/bin/java" ]; then
  for V in 25 24 21; do
    rm -rf "$JAVA_HOME"; mkdir -p "$JAVA_HOME"
    if curl -fsSL "https://api.adoptium.net/v3/binary/latest/${V}/ga/linux/x64/jdk/hotspot/normal/eclipse?project=jdk" -o /tmp/j.tar.gz && \
       tar -xzf /tmp/j.tar.gz -C "$JAVA_HOME" --strip-components=1 && \
       "$JAVA_HOME/bin/java" -version >/dev/null 2>&1; then
      echo "Java $V installed"; break
    fi
  done
fi
"$JAVA_HOME/bin/java" -version

DN="${DATASET_SLUG##*/}"; ID="/kaggle/input/$DN"
if [ -d "$ID" ]; then
  [ -d "$ID/mc-server" ] && cp -a "$ID/mc-server"/. "$MC_DIR"/ || cp -a "$ID"/. "$MC_DIR"/
  find "$MC_DIR" -maxdepth 3 -name '*.zip' -exec unzip -qo {} -d "$MC_DIR" \; 2>/dev/null || true
fi

if [ ! -f server.jar ]; then
  python3 -c "
import json,os,urllib.request
v=os.environ.get('MINECRAFT_VERSION','1.21.1')
m=json.load(urllib.request.urlopen('https://launchermeta.mojang.com/mc/game/version_manifest.json'))
u=next(x for x in m['versions'] if x['id']==v)['url']
d=json.load(urllib.request.urlopen(u))
urllib.request.urlretrieve(d['downloads']['server']['url'],'server.jar')
print('Downloaded',v)"
fi

cat > server.properties <<EOF
server-port=25565
motd=${SERVER_MOTD:-Kaggle MC}
max-players=${MAX_PLAYERS:-20}
view-distance=12
simulation-distance=8
online-mode=true
enable-rcon=false
EOF
echo "eula=true" > eula.txt

if [ ! -x ./playit ]; then
  PU=$(curl -fsSL https://api.github.com/repos/playit-cloud/playit-agent/releases/latest | \
    python3 -c "import sys,json;r=json.load(sys.stdin);u=[a['browser_download_url'] for a in r.get('assets',[]) if 'linux' in a['name'].lower() and ('x86_64' in a['name'].lower() or 'amd64' in a['name'].lower())];print(u[0] if u else '')")
  [ -z "$PU" ] && PU="https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-linux-x86_64"
  curl -L "$PU" -o playit && chmod +x playit
fi

rm -f /kaggle/working/playit.log
if [ -n "${PLAYIT_AUTH_TOKEN:-}" ]; then
  bash -c './playit --auth-token "$0" >/kaggle/working/playit.log 2>&1 || ./playit >>/kaggle/working/playit.log 2>&1' "$PLAYIT_AUTH_TOKEN" &
else
  ./playit >/kaggle/working/playit.log 2>&1 &
fi

rm -f stdin.fifo; mkfifo stdin.fifo
sleep infinity > stdin.fifo &

"$JAVA_HOME/bin/java" -Xms${JAVA_MEM:-24G} -Xmx${JAVA_MEM:-24G} -XX:+UseG1GC \
  -jar server.jar nogui < stdin.fifo > /kaggle/working/minecraft_server.log 2>&1 &
MC_PID=$!

python3 /kaggle/working/mc_sync_daemon.py > "$SYNC_ROOT/daemon.log" 2>&1 &
SYNC_PID=$!

cleanup() {
  echo "🛑 Shutdown trap"
  kill -TERM "$SYNC_PID" 2>/dev/null; wait "$SYNC_PID" 2>/dev/null
  echo "save-off" > stdin.fifo 2>/dev/null; sleep 2
  echo "save-all flush" > stdin.fifo 2>/dev/null; sleep 10
  echo "stop" > stdin.fifo 2>/dev/null; sleep 5
  kill "$MC_PID" 2>/dev/null; wait 2>/dev/null
  echo "✅ Clean shutdown"
}
trap cleanup SIGTERM SIGINT EXIT

echo "STARTUP_COMPLETE"
wait "$MC_PID"

#!/bin/bash
# 播放器.app（启动器版）：双击即跑 standalone_player_atv.py（uv + 已打补丁源码 + mpv-dist）。
# 用法：bash ~/build-player-app.sh [输出目录，默认 ~/Applications]
set -euo pipefail
OUT_DIR="${1:-$HOME/Applications}"
APP="$OUT_DIR/播放器.app"
SCRIPT="/Users/sunrise/atv-player-patches/standalone_player_atv_v2.py"
SRC_DIR="$HOME/src/atv-player"

[ -f "$SCRIPT" ] || { echo "缺少 $SCRIPT"; exit 1; }
[ -d "$SRC_DIR" ] || { echo "缺少 $SRC_DIR"; exit 1; }
command -v uv >/dev/null || { echo "缺少 uv"; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>播放器</string>
  <key>CFBundleDisplayName</key><string>播放器</string>
  <key>CFBundleIdentifier</key><string>com.xiaohei.player</string>
  <key>CFBundleExecutable</key><string>播放器</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>10.15</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeExtensions</key>
      <array><string>iso</string><string>mkv</string><string>mp4</string><string>avi</string><string>mov</string><string>m2ts</string><string>ts</string></array>
      <key>CFBundleTypeRole</key><string>Viewer</string>
    </dict>
  </array>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/播放器" <<'LAUNCHER'
#!/bin/bash
# 启动器：进源码目录，用 uv 跑独立播放器（传进来的参数如拖进来的文件/URL原样透传）。
# Finder 启动时 PATH 不含 ~/.local/bin，用 uv 全路径；出错写日志便于排查。
exec >>"$HOME/Library/Logs/播放器.log" 2>&1
echo "启动: $(date)"
cd "$HOME/src/atv-player" || { echo "进源码目录失败"; exit 1; }
export LC_NUMERIC=C
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec /Users/sunrise/.local/bin/uv run python /Users/sunrise/atv-player-patches/standalone_player_atv_v2.py "$@"
LAUNCHER
chmod +x "$APP/Contents/MacOS/播放器"

echo "已生成：$APP"
echo "双击即可打开。拖文件/ISO 到图标上可直接播。"

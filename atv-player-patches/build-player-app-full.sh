#!/bin/bash
# ATVPlayer.app（完整版）：PyInstaller 把 Python + 依赖 + libmpv + 已打补丁源码全打进 .app。
# 用法：bash ~/atv-player-patches/build-player-app-full.sh [输出目录，默认 /Applications]
# 要求：~/src/atv-player 已打好补丁（v8 除 mpv_widget + 字幕关闭 + 菜单手感 + 渲染文件覆盖 + ISO-on-render v2）。
set -euo pipefail
OUT_DIR="${1:-/Applications}"
SRC="${SRC:-$HOME/src/atv-player}"
PATCHES="${PATCHES:-$HOME/atv-player-patches}"
MPV_LIB="${MPV_LIB:-$HOME/mpv-dist/lib}"
WORK="$HOME/.cache/player-full-build"
APP_NAME="ATVPlayer"

# 1. 校验源码已打补丁
grep -q "set_iso_track_languages" "$SRC/src/atv_player/player/mpv_widget.py" || { echo "错误：mpv_widget.py 缺 ISO 语言修复（先按复原测试流程打补丁）"; exit 1; }
grep -q "track_id" "$SRC/src/atv_player/ui/player_window.py" || { echo "错误：player_window.py 缺偏好 id 修复"; exit 1; }
[ -f "$MPV_LIB/libmpv.2.dylib" ] || { echo "错误：缺少 $MPV_LIB/libmpv.2.dylib"; exit 1; }

# 2. 准备工作目录
rm -rf "$WORK" && mkdir -p "$WORK"
cp "$PATCHES/standalone_player_atv_v2.py" "$WORK/player_main.py"

# 3. 收集 libmpv + 全部依赖 dylib（--add-binary 用）
ADD_BINARIES=()
for f in "$MPV_LIB"/*.dylib; do
  ADD_BINARIES+=(--add-binary "$f:.")
done

# 4. PyInstaller 打包（在项目环境里跑，和 PySide6 同一 venv；uvx 会隔到干净环境，找不到 PySide6）
cd "$SRC"
uv run --with pyinstaller pyinstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier com.xiaohei.player \
  --paths "$SRC/src" \
  --distpath "$WORK/dist" \
  --workpath "$WORK/build" \
  --specpath "$WORK" \
  --copy-metadata atv-player \
  --hidden-import PySide6.QtOpenGL \
  --hidden-import PySide6.QtOpenGLWidgets \
  --hidden-import PySide6.QtGui \
  --hidden-import PySide6.QtWidgets \
  --hidden-import PySide6.QtCore \
  --hidden-import PySide6.QtNetwork \
  --hidden-import PySide6.QtMultimedia \
  "${ADD_BINARIES[@]}" \
  --collect-all atv_player \
  --exclude-module PySide6.QtWebEngineWidgets \
  --exclude-module PySide6.QtWebEngineCore \
  --exclude-module PySide6.Qt3DCore \
  --exclude-module PySide6.Qt3DRender \
  --exclude-module PySide6.Qt3DExtras \
  --exclude-module PySide6.QtCharts \
  --exclude-module PySide6.QtDataVisualization \
  --exclude-module PySide6.QtPdf \
  --exclude-module PySide6.QtPdfWidgets \
  --exclude-module PySide6.QtRemoteObjects \
  --exclude-module PySide6.QtScxml \
  --exclude-module PySide6.QtStateMachine \
  --exclude-module PySide6.QtTextToSpeech \
  --exclude-module PySide6.QtLocation \
  --exclude-module PySide6.QtPositioning \
  --exclude-module PySide6.QtSensors \
  --exclude-module PySide6.QtSerialPort \
  --exclude-module PySide6.QtSerialBus \
  --exclude-module PySide6.QtBluetooth \
  --exclude-module PySide6.QtNfc \
  --exclude-module PySide6.QtSql \
  --exclude-module PySide6.QtXml \
  --exclude-module PySide6.QtTest \
  --exclude-module PySide6.QtDesigner \
  --exclude-module PySide6.QtHelp \
  --exclude-module PySide6.QtUiTools \
  "$WORK/player_main.py"

# 5. 修复 bundle 内 dylib 的依赖路径（/usr/local/opt/... → @loader_path/...）
#    否则换台无 Homebrew 的 Mac 加载不到依赖。
APP="$WORK/dist/$APP_NAME.app"
FRAMEWORKS="$APP/Contents/Frameworks"
mkdir -p "$FRAMEWORKS"
# PyInstaller 把 dylib 放到 Contents/Frameworks（onedir）或 _MEIPASS；先找它们
# 注意：grep 找不到匹配时退 1，pipefail + set -e 会杀掉脚本，故加 || true。
find "$APP/Contents" -name "*.dylib" | while read -r dylib; do
  # 把 /usr/local/opt/... 与 /opt/homebrew/... 的依赖改成 @loader_path/同名文件
  (otool -L "$dylib" | tail -n +2 | awk '{print $1}' | grep -E "^/(usr/local/opt|opt/homebrew|usr/local/Cellar)" || true) | while read -r dep; do
    base=$(basename "$dep")
    install_name_tool -change "$dep" "@loader_path/$base" "$dylib" 2>/dev/null || true
  done
  # 加 @loader_path 到 rpath（让 @loader_path 解析到 dylib 所在目录）
  install_name_tool -add_rpath "@loader_path" "$dylib" 2>/dev/null || true
  codesign --force --sign - "$dylib" 2>/dev/null || true
done

# 5b. 注册 URL scheme（alist 外部播放器用 atvplayer://play?url=<链接> 调起）
#     只有完整版 PyInstaller .app 能收 Apple Event（QFileOpenEvent）；启动器版 shell 脚本收不到。
#     用 plistlib（PlistBuddy Add 在 key 已存在时静默失败，会把 scheme 丢掉）。
python3 -c "
import plistlib
p = '$APP/Contents/Info.plist'
with open(p, 'rb') as f:
    d = plistlib.load(f)
d['CFBundleURLTypes'] = [{'CFBundleURLName': 'com.xiaohei.player', 'CFBundleURLSchemes': ['atvplayer']}]
with open(p, 'wb') as f:
    plistlib.dump(d, f)
"

# 6. 输出到目标目录（直接拷到 /Applications，自动安装，省去手动拖）
# 必须先 mkdir -p：OUT_DIR 不存在时 cp -R "$APP" "$OUT_DIR/" 会把 .app 本体拷成
# OUT_DIR 自身（里面只剩裸 Contents/），后续 ditto 找不到 ATVPlayer.app。
mkdir -p "$OUT_DIR"
rm -rf "$OUT_DIR/$APP_NAME.app"
cp -R "$APP" "$OUT_DIR/"
[ -d "$OUT_DIR/$APP_NAME.app" ] || { echo "错误：$OUT_DIR/$APP_NAME.app 不存在（cp 落点不对）"; ls -la "$OUT_DIR"; exit 1; }
# 注册 scheme（新装 .app 浏览器可能还没认到）
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$OUT_DIR/$APP_NAME.app" 2>/dev/null || true
echo "已安装：$OUT_DIR/$APP_NAME.app"
echo "可拷贝到其它 Intel Mac 双击运行（无需 uv/源码/mpv-dist）。"

#!/bin/bash
# VIDEO RE-EDITOR: one-file installer for an Ubuntu server (Oracle Cloud Always Free).
#
# Oracle Cloud -> Create instance -> image "Canonical Ubuntu 24.04" -> Advanced options ->
# Management -> cloud-init script -> choose this file (or paste its text). Nothing else to type.
# The script runs once at first boot, installs everything in the background (5-10 minutes)
# and starts the bot. It keeps retrying until it succeeds, also after a reboot.
#
# This is a TEMPLATE: build the real file with deploy/oracle/build_installer.py.
# The built file contains the bot token and Telegram API keys - never publish it.

BOT_TOKEN='__BOT_TOKEN__'
TG_API_ID='__TG_API_ID__'
TG_API_HASH='__TG_API_HASH__'
REPO_URL='__REPO_URL__'
BRANCHES='__BRANCHES__'

BASE=/opt/video-re-editor
APP_DIR=$BASE/app
VENV=$BASE/venv
DATA_DIR=/var/lib/video-re-editor
CONF_DIR=/etc/video-re-editor
ENV_FILE=$CONF_DIR/bot.env
SELF=/usr/local/sbin/video-re-editor-install
LOG=/var/log/video-re-editor-install.log
RUN_USER=vre

log() {
    echo "[video-re-editor] $(date '+%F %T') $*" | tee -a "$LOG"
}

write_config() {
    install -d -m 700 "$CONF_DIR"
    (
        umask 077
        cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$BOT_TOKEN
TG_API_ID=$TG_API_ID
TG_API_HASH=$TG_API_HASH
DATA_DIR=$DATA_DIR
EOF
        cat > "$CONF_DIR/update.conf" <<EOF
REPO_URL='$REPO_URL'
BRANCHES='$BRANCHES'
EOF
    )
    chmod 600 "$ENV_FILE" "$CONF_DIR/update.conf"
}

ensure_swap() {
    local mem_kb
    mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
    if [ "${mem_kb:-0}" -lt 3000000 ] && [ -z "$(swapon --show --noheadings 2>/dev/null)" ] && [ ! -f /swapfile ]; then
        log "Мало памяти: создаю файл подкачки 2 ГБ"
        fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
        chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
        grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
}

install_packages() {
    export DEBIAN_FRONTEND=noninteractive
    local opts=(-o DPkg::Lock::Timeout=900 -o Acquire::Retries=5
                -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
    apt-get "${opts[@]}" update -q >>"$LOG" 2>&1 &&
        apt-get "${opts[@]}" install -y -q --no-install-recommends \
            ca-certificates git ffmpeg python3 python3-venv python3-pip >>"$LOG" 2>&1
}

clone_code() {
    local dest=$1 branch
    for branch in $BRANCHES; do
        rm -rf "$dest"
        if GIT_TERMINAL_PROMPT=0 git clone -q --depth 1 --branch "$branch" "$REPO_URL" "$dest" >>"$LOG" 2>&1 &&
            [ -f "$dest/main.py" ]; then
            return 0
        fi
    done
    rm -rf "$dest"
    return 1
}

install_code() {
    [ -f "$APP_DIR/main.py" ] && return 0
    mkdir -p "$BASE"
    if ! clone_code "$APP_DIR.tmp"; then
        log "Не получается скачать код с GitHub. Репозиторий должен быть публичным. Повторю через 2 минуты."
        return 1
    fi
    mv "$APP_DIR.tmp" "$APP_DIR"
}

install_python() {
    [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV" >>"$LOG" 2>&1 || return 1
    "$VENV/bin/pip" install -q --disable-pip-version-check --upgrade pip >>"$LOG" 2>&1 || true
    "$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt" >>"$LOG" 2>&1
}

write_units() {
    cat > /etc/systemd/system/video-re-editor.service <<EOF
[Unit]
Description=VIDEO RE-EDITOR Telegram bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
User=$RUN_USER
Group=$RUN_USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal+console
StandardError=journal+console
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

    cat > /usr/local/sbin/video-re-editor-update <<'EOF'
#!/bin/bash
# Checks GitHub for a new version, verifies it, switches and rolls back if it does not start.
set -u
. /etc/video-re-editor/update.conf
BASE=/opt/video-re-editor
APP=$BASE/app
NEW=$BASE/app.new
OLD=$BASE/app.old
VENV=$BASE/venv
DATA=/var/lib/video-re-editor
BAD=$BASE/bad-commit
export GIT_TERMINAL_PROMPT=0
log() { echo "[video-re-editor-update] $*"; }

# Never interrupt somebody's video: postpone while uploads or jobs are present.
if [ -d "$DATA/temp" ] && [ -n "$(find "$DATA/temp" -mindepth 1 -maxdepth 1 -mmin -45 2>/dev/null | head -n 1)" ]; then
    log "бот занят, обновлю позже"
    exit 0
fi
rm -rf "$NEW"
for branch in $BRANCHES; do
    if git clone -q --depth 1 --branch "$branch" "$REPO_URL" "$NEW" 2>/dev/null && [ -f "$NEW/main.py" ]; then
        break
    fi
    rm -rf "$NEW"
done
if [ ! -f "$NEW/main.py" ]; then
    log "код на GitHub недоступен, пропускаю"
    exit 0
fi
NEW_COMMIT=$(git -C "$NEW" rev-parse HEAD)
if [ "$NEW_COMMIT" = "$(git -C "$APP" rev-parse HEAD 2>/dev/null)" ] || [ "$NEW_COMMIT" = "$(cat "$BAD" 2>/dev/null)" ]; then
    rm -rf "$NEW"
    exit 0
fi
log "найдена новая версия, проверяю"
if ! "$VENV/bin/pip" install -q --disable-pip-version-check -r "$NEW/requirements.txt"; then
    log "не удалось установить библиотеки, остаюсь на текущей версии"
    rm -rf "$NEW"
    exit 1
fi
if ! (cd "$NEW" && "$VENV/bin/python" -m compileall -q app main.py >/dev/null && "$VENV/bin/python" -c "import main"); then
    log "новая версия не прошла проверку, остаюсь на текущей"
    echo "$NEW_COMMIT" > "$BAD"
    rm -rf "$NEW"
    exit 1
fi
rm -rf "$OLD"
mv "$APP" "$OLD" && mv "$NEW" "$APP"
systemctl restart video-re-editor
sleep 60
if systemctl is-active --quiet video-re-editor; then
    log "обновлено до $(git -C "$APP" rev-parse --short HEAD)"
else
    log "новая версия не запускается, возвращаю прежнюю"
    echo "$NEW_COMMIT" > "$BAD"
    rm -rf "$NEW"
    mv "$APP" "$NEW" && mv "$OLD" "$APP"
    rm -rf "$NEW"
    systemctl restart video-re-editor
fi
EOF
    chmod 700 /usr/local/sbin/video-re-editor-update

    cat > /etc/systemd/system/video-re-editor-update.service <<'EOF'
[Unit]
Description=VIDEO RE-EDITOR auto-update
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/video-re-editor-update
EOF

    cat > /etc/systemd/system/video-re-editor-update.timer <<'EOF'
[Unit]
Description=VIDEO RE-EDITOR auto-update check

[Timer]
OnBootSec=20min
OnUnitActiveSec=6h
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
EOF

    cat > /usr/local/bin/video-re-editor-status <<'EOF'
#!/bin/bash
systemctl --no-pager status video-re-editor | head -n 5
echo
journalctl -u video-re-editor --no-pager -n 30
EOF
    chmod 755 /usr/local/bin/video-re-editor-status
    systemctl daemon-reload
}

run_install() {
    log "Шаг 1/5: системные пакеты (FFmpeg, Python, Git)"
    ensure_swap
    if ! install_packages; then
        log "Ошибка установки пакетов, повторю через 2 минуты (подробности в $LOG)"
        return 1
    fi
    id "$RUN_USER" >/dev/null 2>&1 ||
        useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$RUN_USER"
    install -d -o "$RUN_USER" -g "$RUN_USER" -m 750 "$DATA_DIR"
    log "Шаг 2/5: код бота с GitHub"
    install_code || return 1
    log "Шаг 3/5: библиотеки Python"
    if ! install_python; then
        log "Ошибка установки библиотек Python, повторю через 2 минуты (подробности в $LOG)"
        return 1
    fi
    log "Шаг 4/5: автозапуск и автообновление"
    mkdir -p /etc/systemd/system
    write_units
    systemctl enable --now video-re-editor.service >>"$LOG" 2>&1
    systemctl enable --now video-re-editor-update.timer >>"$LOG" 2>&1
    systemctl disable video-re-editor-install.service >>"$LOG" 2>&1 || true
    log "Шаг 5/5: готово! Бот запущен. Напиши ему /start в Telegram."
}

bootstrap() {
    if [ "$(id -u)" != 0 ]; then
        echo "Запусти от root: sudo bash $0"
        exit 1
    fi
    if ! command -v apt-get >/dev/null 2>&1; then
        log "ОШИБКА: нужен образ Ubuntu (Canonical Ubuntu 24.04 или 22.04)."
        exit 1
    fi
    write_config
    mkdir -p /etc/systemd/system /usr/local/sbin
    install -m 700 "$0" "$SELF"
    cat > /etc/systemd/system/video-re-editor-install.service <<EOF
[Unit]
Description=VIDEO RE-EDITOR installation (retries until it succeeds)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=$SELF --run
Restart=on-failure
RestartSec=120
TimeoutStartSec=0
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable video-re-editor-install.service >>"$LOG" 2>&1
    systemctl start --no-block video-re-editor-install.service
    log "Установка запущена в фоне, обычно занимает 5-10 минут. Ход установки: $LOG"
}

case "${1:-}" in
    --run) run_install ;;
    *) bootstrap ;;
esac

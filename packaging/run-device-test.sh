#!/bin/sh
set -eu
umask 077
export XDG_RUNTIME_DIR=/run/user/1000
[ "$(id -un)" = pi ] || { echo 'Run as pi'; exit 1; }
backlight=/sys/class/backlight/backlight/brightness
previous=$(cat "$backlight")
restore() {
    printf '%s\n' "$previous" > "$backlight"
    systemctl --user start APPLaunch.service
}
systemctl --user stop APPLaunch.service
trap restore EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
printf '70\n' > "$backlight"
/usr/bin/airsim-cardputer "$@"

#!/bin/bash
# Install local-weather-awareness as a cron job. No systemd needed: tau.kirx.net runs Gentoo
# with OpenRC. Every 5 minutes cron runs deploy/local-weather-awareness-cron.sh as an
# unprivileged user; it regenerates the static page into OUT_DIR, which Apache serves as
# https://<host>/myweather/ (https://tau.kirx.net/myweather/ in the example deployment). (On
# a systemd host deploy/install.sh does the same with a service + timer; use one or the
# other, not both.)
#
# Usage:  sudo RUN_USER=weather ./deploy/install-cron.sh [OUT_DIR] [RUN_USER]
#   OUT_DIR   where index.html + PNGs go. Default: WEATHER_OUT_DIR from
#             /etc/local-weather-awareness.env or ~RUN_USER/local-weather-awareness.env, else
#             /var/www/localhost/htdocs/myweather when /var/www/localhost/htdocs exists
#             (Gentoo's default DocumentRoot), else /var/www/html/myweather.
#   CACHE_DIR (environment only) the generator's cache. Default: WEATHER_CACHE_DIR from those
#             env files, else ~RUN_USER/.cache/local-weather-awareness.
#   RUN_USER  account that runs the job (default: the sudo caller, else the owner of this
#             checkout; root is refused).
#   DRY_RUN=1 print what would be done and change nothing (no root needed). The files it
#             would write (env file, crontab, Apache snippet) go to a temp dir for review.
#
# Steps (idempotent: rerun it after an update that touched deploy/):
#   1. resolve the user and the paths; an OUT_DIR / CACHE_DIR that contradicts an env file
#      is refused (the wrapper loads the env files at run time, so they would win), and so is
#      one taken from an env file the run user can write
#      (~RUN_USER/local-weather-awareness.env): this script runs as root and hands those
#      directories to RUN_USER;
#   2. create the output and cache directories, owned by RUN_USER (see prepare_dir: root
#      never acts on a path the run user controls, and never takes over a system directory
#      or an existing directory of someone else);
#   3. make /etc/local-weather-awareness.env set WEATHER_OUT_DIR and WEATHER_CACHE_DIR: the
#      resolved values are appended when missing, no other line is ever rewritten; mode
#      0644, because the cron job reads it as RUN_USER;
#   4. put "*/5 * * * * <repo>/deploy/local-weather-awareness-cron.sh # local-weather-awareness"
#      into RUN_USER's crontab, replacing an older local-weather-awareness line and keeping
#      every other line;
#   5. check that a cron daemon is running (prints the Gentoo fix when none is);
#   6. render the Apache snippet with the real OUT_DIR to
#      /etc/local-weather-awareness/apache-local-weather-awareness.conf;
#   7. run the generator once as RUN_USER through the wrapper (a failure is not fatal);
#   8. print the next steps: Apache, checks, uninstall.
# It never edits the Apache configuration itself.
#
# Test hooks (environment): ENV_FILES (env files to read, space-separated; default
# "/etc/local-weather-awareness.env ~RUN_USER/local-weather-awareness.env"), SYSTEM_ENV_FILE
# (the file step 3 completes; default /etc/local-weather-awareness.env), GENTOO_DOCROOT
# (default /var/www/localhost/htdocs), ETC_DIR (default /etc/local-weather-awareness).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WRAPPER="$REPO/deploy/local-weather-awareness-cron.sh"
DRY_RUN="${DRY_RUN:-}"
ETC_DIR="${ETC_DIR:-/etc/local-weather-awareness}"         # rendered Apache snippet lives here
SYSTEM_ENV_FILE="${SYSTEM_ENV_FILE:-/etc/local-weather-awareness.env}"
GENTOO_DOCROOT="${GENTOO_DOCROOT:-/var/www/localhost/htdocs}"
REQ_OUT_DIR="${1:-${OUT_DIR:-}}"                           # asked for explicitly (argument or env)
REQ_CACHE_DIR="${CACHE_DIR:-}"
CRON_TAG="# local-weather-awareness"
GENTOO_CRON_FIX="emerge --ask sys-process/cronie && rc-update add cronie default && rc-service cronie start"

die() { echo "error: $*" >&2; exit 1; }
run() {  # run CMD...: execute, or just print it in dry-run mode
    if [ -n "$DRY_RUN" ]; then echo "+ $*"; else "$@"; fi
}

if [ -z "$DRY_RUN" ] && [ "$(id -u)" -ne 0 ]; then
    die "run me as root:  sudo RUN_USER=<user> $0 [OUT_DIR]   (or DRY_RUN=1 $0 to preview)"
fi
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PREVIEW=""
if [ -n "$DRY_RUN" ]; then
    PREVIEW="$(mktemp -d)"
    ETC_DIR="$PREVIEW/local-weather-awareness"
    echo "DRY RUN: nothing is changed; preview files go to $PREVIEW"
fi

# ---- 1. who runs it --------------------------------------------------------------------
RUN_USER="${2:-${RUN_USER:-${SUDO_USER:-$(stat -c %U "$REPO")}}}"
[ "$RUN_USER" != "root" ] || die "refusing to run the generator as root; pass RUN_USER=<user>" \
    "(e.g. create one: useradd --system --create-home --shell /sbin/nologin weather)"
id "$RUN_USER" >/dev/null 2>&1 || die "no such user: $RUN_USER" \
    "(create it: useradd --system --create-home --shell /sbin/nologin $RUN_USER)"
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[ -n "$RUN_HOME" ] || die "cannot determine the home directory of $RUN_USER"

# ---- 1. paths: argument / environment, else the env files, else the default ---------------
# The files the cron wrapper loads before every run, in its order (later wins).
ENV_FILES="${ENV_FILES:-$SYSTEM_ENV_FILE $RUN_HOME/local-weather-awareness.env}"
# env_lookup, env_source and canon are the same as in deploy/install.sh (a test keeps them
# identical); they read the env files the way the wrapper parses them.
env_lookup() {  # env_lookup KEY: last non-empty KEY=value across the env files, read the way
                # the cron wrapper reads them (blanks around '=' and the value, a CR and one
                # pair of surrounding quotes removed)
    local key="$1" val="" f v
    for f in $ENV_FILES; do
        [ -r "$f" ] || continue
        v="$(grep -E "^[[:space:]]*${key}[[:space:]]*=[[:space:]]*[^[:space:]]" "$f" | tail -n 1 \
             | sed -e 's/\r$//' -e "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//" \
                   -e 's/[[:space:]]*$//' -e "s/^\"\(.*\)\"$/\1/" -e "s/^'\(.*\)'$/\1/")" || true
        [ -n "$v" ] && val="$v"
    done
    printf '%s' "$val"
}
env_source() {  # env_source KEY: the env file whose KEY= is the one env_lookup returns
    local key="$1" src="" f
    for f in $ENV_FILES; do
        [ -r "$f" ] || continue
        grep -qE "^[[:space:]]*${key}[[:space:]]*=[[:space:]]*[^[:space:]]" "$f" && src="$f"
    done
    printf '%s' "$src"
}
file_trusted() {  # file_trusted FILE: owned by root or by whoever runs this script, and neither
                  # it nor its directory writable by anybody else (so
                  # ~RUN_USER/local-weather-awareness.env is NOT trusted when root runs the
                  # installer for RUN_USER)
    local f="$1" me d uid mode
    me="$(id -u)"
    for d in "$f" "$(dirname "$f")"; do
        uid="$(stat -L -c %u "$d" 2>/dev/null)" || return 1
        mode="$(stat -L -c %a "$d" 2>/dev/null)" || return 1
        [ "$uid" = 0 ] || [ "$uid" = "$me" ] || return 1
        [ $((0$mode & 022)) -eq 0 ] || return 1          # group- or other-writable
    done
    return 0
}
path_from_trusted_file() {  # path_from_trusted_file KEY: stop when the env file that sets KEY
                            # could be changed by somebody other than the user running this
    local key="$1" src
    src="$(env_source "$key")"
    [ -n "$src" ] || return 0
    file_trusted "$src" && return 0
    die "$src sets $key, but that file or its directory belongs to another user or is
       writable by others (file: owner $(stat -L -c %U "$src" 2>/dev/null || echo ?), mode
       $(stat -L -c %a "$src" 2>/dev/null || echo ?)). This installer runs as $(id -un), creates
       that directory and hands it to $RUN_USER, so it takes the path only from a file nobody
       else can change. Move the $key line to $SYSTEM_ENV_FILE (owned by root, mode 0644)
       and rerun."
}
canon() {  # canon PATH: ~ expanded (cache dir only; config.py does the same), no trailing
           # slash, // and /./ collapsed; symlinks are NOT resolved
    local p="$1"
    [ "${2:-}" = "tilde" ] && p="${p/#\~/$RUN_HOME}"
    case "$p" in
        /*) realpath -m -s -- "$p" 2>/dev/null || printf '%s' "${p%/}" ;;
        *)  printf '%s' "${p%/}" ;;
    esac
}
path_from_trusted_file WEATHER_OUT_DIR
path_from_trusted_file WEATHER_CACHE_DIR
ENV_OUT_DIR="$(env_lookup WEATHER_OUT_DIR)"
ENV_CACHE_DIR="$(env_lookup WEATHER_CACHE_DIR)"
[ -z "$ENV_OUT_DIR" ] || ENV_OUT_DIR="$(canon "$ENV_OUT_DIR")"
[ -z "$ENV_CACHE_DIR" ] || ENV_CACHE_DIR="$(canon "$ENV_CACHE_DIR" tilde)"
[ -z "$REQ_OUT_DIR" ] || REQ_OUT_DIR="$(canon "$REQ_OUT_DIR")"
[ -z "$REQ_CACHE_DIR" ] || REQ_CACHE_DIR="$(canon "$REQ_CACHE_DIR" tilde)"

# The wrapper loads the env files before every run, so a requested path that differs from
# theirs would be created here and then never used.
env_conflict() {  # env_conflict NAME KEY REQUESTED FROM_ENV_FILE
    local name="$1" key="$2" req="$3" have="$4" src
    [ -n "$req" ] && [ -n "$have" ] && [ "$req" != "$have" ] || return 0
    src="$(env_source "$key")"
    die "$name=$req was requested, but ${src:-an env file} sets $key=$have.
       The env file wins at run time (deploy/local-weather-awareness-cron.sh loads it
       before every run), so the generator would write to $have, which this installer
       would not have prepared. Either set $key=$req in ${src:-that file} and rerun, or
       rerun without $name to use $have."
}
env_conflict OUT_DIR WEATHER_OUT_DIR "$REQ_OUT_DIR" "$ENV_OUT_DIR"
env_conflict CACHE_DIR WEATHER_CACHE_DIR "$REQ_CACHE_DIR" "$ENV_CACHE_DIR"

if [ -d "$GENTOO_DOCROOT" ]; then                  # Gentoo's www-servers/apache default
    DEFAULT_OUT_DIR="$(canon "$GENTOO_DOCROOT")/myweather"
else                                               # Debian, RHEL and most others
    DEFAULT_OUT_DIR="/var/www/html/myweather"
fi
OUT_DIR="${REQ_OUT_DIR:-${ENV_OUT_DIR:-$DEFAULT_OUT_DIR}}"
CACHE_DIR="${REQ_CACHE_DIR:-${ENV_CACHE_DIR:-$RUN_HOME/.cache/local-weather-awareness}}"
# These paths end up in a crontab line (run by /bin/sh, where '%' means newline), in sed
# replacements and in env files: allow only plain path characters.
for p in "$OUT_DIR" "$CACHE_DIR" "$REPO" "$RUN_HOME"; do
    case "$p" in
        /*) ;;
        *) die "path must be absolute: $p" ;;
    esac
    case "$p" in
        *[!A-Za-z0-9._/+@,:=-]*) die "path may only contain letters, digits and . _ / + @ , : = -: $p" ;;
    esac
done

# What the wrapper will run with: PATH and WEATHER_PYTHON from the env files, else cron's
# usual default PATH (cronie: /usr/bin:/bin). Both are only ever run as RUN_USER (as_run_user),
# so they may come from RUN_USER's own env file; a value the wrapper could not use stops here
# instead of failing every cron run later.
CRON_PATH="$(env_lookup PATH)"
case "$CRON_PATH" in
    "") CRON_PATH="/usr/bin:/bin" ;;
    *[!A-Za-z0-9._/:+-]*)
        die "$(env_source PATH) sets PATH=$CRON_PATH: the env files are not shell scripts, so
       \$PATH, ~ or blanks are not expanded; write the directories out, e.g.
       PATH=/usr/local/bin:/usr/bin:/bin" ;;
esac
PY="$(env_lookup WEATHER_PYTHON)"
case "$PY" in
    "") PY="python3" ;;
    *[!A-Za-z0-9._/+-]*)
        die "$(env_source WEATHER_PYTHON) sets WEATHER_PYTHON=$PY: give an interpreter name
       or absolute path (letters, digits and . _ / + - only), e.g. WEATHER_PYTHON=python3.12" ;;
esac
CRON_LINE="*/5 * * * * $WRAPPER $CRON_TAG"

case "$OUT_DIR" in
    /var/www/html/*)
        if [ -d "$GENTOO_DOCROOT" ]; then
            where="$(env_source WEATHER_OUT_DIR)"
            echo "NOTE: $OUT_DIR is the Debian/RHEL location, but this host has $GENTOO_DOCROOT" \
                 "(Gentoo's default DocumentRoot). The page is then reachable only through the" \
                 "snippet's Alias; to put it under the DocumentRoot instead, set" \
                 "WEATHER_OUT_DIR=$(canon "$GENTOO_DOCROOT")/myweather in ${where:-$SYSTEM_ENV_FILE}" \
                 "and rerun." >&2
        fi ;;
esac
echo "repo:      $REPO"
echo "user:      $RUN_USER ($RUN_GROUP), home $RUN_HOME"
echo "output:    $OUT_DIR"
echo "cache:     $CACHE_DIR"
echo "env file:  $SYSTEM_ENV_FILE"
echo "cron line: $CRON_LINE"

# ---- sanity: the wrapper, python + Pillow, fonts, cron ----------------------------------
[ -x "$WRAPPER" ] || die "$WRAPPER is missing or not executable (chmod 755 it)"
[ -f "$REPO/make_weather_page.py" ] || echo "WARNING: $REPO/make_weather_page.py not found" >&2
if [ -f /etc/systemd/system/local-weather-awareness.timer ]; then
    echo "WARNING: /etc/systemd/system/local-weather-awareness.timer exists (deploy/install.sh):" \
         "with the cron job too, both run the generator; disable one" \
         "(systemctl disable --now local-weather-awareness.timer)." >&2
fi

as_run_user() {  # as_run_user CMD: run a shell command as RUN_USER with cron's bare environment
    local envcmd="cd / && exec env -i HOME=$RUN_HOME LOGNAME=$RUN_USER USER=$RUN_USER SHELL=/bin/sh PATH=$CRON_PATH"
    if [ "$(id -u)" -eq 0 ]; then
        su -s /bin/sh "$RUN_USER" -c "$envcmd $1"
    else                          # dry run without root: as this user, with the same env
        sh -c "$envcmd $1"
    fi
}
if ! as_run_user "$PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'" 2>/dev/null; then
    echo "WARNING: '$PY' (PATH=$CRON_PATH, as $RUN_USER) is missing or older than Python 3.9;" \
         "every cron run will fail. Gentoo: emerge --ask dev-lang/python; or set WEATHER_PYTHON" \
         "(and PATH) in $SYSTEM_ENV_FILE." >&2
elif ! as_run_user "$PY -c 'import PIL'" 2>/dev/null; then
    cat >&2 <<MSG
WARNING: '$PY' (as $RUN_USER, PATH=$CRON_PATH) cannot import Pillow: the page will be
         generated WITHOUT radar maps. Pillow must be built for the Python version that
         '$PY' runs ($(as_run_user "$PY --version" 2>&1 || true)):
           Gentoo:  emerge --ask dev-python/pillow   (PYTHON_TARGETS must include that
                    version; or point WEATHER_PYTHON=python3.X in $SYSTEM_ENV_FILE at a
                    version Pillow is built for; see README)
           Debian/Ubuntu: apt install python3-pil    RHEL family: dnf install python3-pillow (EPEL)
MSG
else
    # The map labels want FreeType and a TrueType font; PNG output needs zlib. Ask the
    # renderer's own font list (as in install.sh), as RUN_USER like every other interpreter
    # call here: WEATHER_PYTHON may come from RUN_USER's own env file.
    rc=0
    as_run_user "$PY -B -c '
import sys
sys.path.insert(0, \"$REPO\")
try:
    from PIL import ImageFont
    from weather.radar import FONT_PATHS
except Exception:
    sys.exit(2)
try:
    from PIL import features
    if not features.check(\"zlib\"):
        sys.exit(4)
    if not features.check(\"freetype2\"):
        sys.exit(3)
except ImportError:
    pass
for p in FONT_PATHS:
    try:
        ImageFont.truetype(p, 12)
    except Exception:
        continue
    sys.exit(0)
sys.exit(1)'" 2>/dev/null || rc=$?
    case "$rc" in
        1) echo "WARNING: no TrueType font for the map labels (DejaVu Sans, Liberation Sans or" \
                "FreeSans); they fall back to Pillow's small default font. Gentoo: emerge --ask" \
                "media-fonts/dejavu  (Debian: fonts-dejavu-core, RHEL: dejavu-sans-fonts)" >&2 ;;
        3) echo "WARNING: Pillow was built without FreeType, so the map labels use its small" \
                "default font. Gentoo: enable USE=truetype for dev-python/pillow and re-emerge it." >&2 ;;
        4) echo "WARNING: Pillow was built without zlib and cannot write PNG maps. Gentoo: enable" \
                "USE=zlib for dev-python/pillow and re-emerge it." >&2 ;;
    esac
fi
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]; then
    echo "WARNING: SELinux is on: Apache may only read httpd_sys_content_t; label the output" \
         "directory: chcon -R -t httpd_sys_content_t $OUT_DIR (deploy/install.sh does it persistently)" >&2
fi
HAVE_CRONTAB=1
if ! command -v crontab >/dev/null 2>&1; then
    HAVE_CRONTAB=""
    [ -n "$DRY_RUN" ] || die "no crontab command: install a cron daemon first. Gentoo: $GENTOO_CRON_FIX"
    echo "WARNING: no crontab command here (Gentoo: $GENTOO_CRON_FIX); the preview shows the new line alone" >&2
fi

# ---- 2. directories ----------------------------------------------------------------------
# prepare_dir and root_safe are the same in deploy/install.sh (a test keeps them identical).
root_safe() {  # root_safe DIR: every existing ancestor of DIR is owned by root and writable by
               # nobody else, so no other user can swap a path component under root's feet
    local d="$1" uid mode
    while [ "$d" != / ]; do
        d="$(dirname "$d")"
        [ -e "$d" ] || continue
        uid="$(stat -c %u "$d")" && mode="$(stat -c %a "$d")" || return 1
        [ "$uid" = 0 ] && [ $((0$mode & 022)) -eq 0 ] || return 1
    done
    return 0
}
prepare_dir() {  # prepare_dir WHAT DIR DEFAULT: DIR (mode 0755) owned by RUN_USER, safely
    local what="$1" dir="$2" default="$3" real owner
    # symlinks resolved: root acts on the real path, never through a link someone may swap
    real="$(realpath -m -- "$dir")"
    case "$real/" in
        /bin/* | /boot/* | /dev/* | /etc/* | /lib/* | /lib32/* | /lib64/* | /libx32/* | /proc/* | \
        /root/* | /run/* | /sbin/* | /sys/* | /usr/* | /var/db/* | /var/log/* | /var/spool/*)
            die "refusing $what $dir ($real): a system location is never handed to $RUN_USER" ;;
    esac
    case "$real" in
        / | /home | /opt | /srv | /tmp | /var | /var/cache | /var/lib | /var/tmp | /var/www | \
        /var/www/html | /var/www/localhost | /var/www/localhost/htdocs | "$RUN_HOME")
            die "refusing $what $dir: use a directory of its own below it, e.g. $dir/myweather" ;;
    esac
    if root_safe "$real"; then
        if [ -e "$real" ]; then
            [ -d "$real" ] || die "$what $real exists and is not a directory"
            owner="$(stat -c %U "$real")"
            if [ "$owner" != "$RUN_USER" ] && [ "$real" != "$(realpath -m -- "$default")" ]; then
                die "$what $real already exists and belongs to $owner. This installer takes over
       only the default directory ($default) or one that already belongs to $RUN_USER.
       If $real is meant for local-weather-awareness, hand it over yourself and rerun:
         chown $RUN_USER:$RUN_GROUP $real && chmod 0755 $real"
            fi
        fi
        run install -d -m 0755 -o "$RUN_USER" -g "$RUN_GROUP" "$real"
    else
        # inside space that RUN_USER (or another user) controls, e.g. ~RUN_USER/.cache: RUN_USER
        # creates it, so no root action ever follows a path somebody else can change
        if [ -n "$DRY_RUN" ]; then
            echo "+ su -s /bin/sh $RUN_USER -c 'mkdir -p $real && chmod 0755 $real'"
        elif ! as_run_user "mkdir -p $real && chmod 0755 $real && test -w $real" 2>/dev/null; then
            die "$RUN_USER cannot create or write $what $real (it lies in a directory root does
       not own, so this installer does not create it as root). Create it as $RUN_USER, or
       choose a directory under a root-owned parent."
        fi
    fi
    echo "$(printf '%-10s' "$what") $dir owned by $RUN_USER"
}
prepare_dir "output dir" "$OUT_DIR" "$DEFAULT_OUT_DIR"
prepare_dir "cache dir" "$CACHE_DIR" "$RUN_HOME/.cache/local-weather-awareness"

# ---- 3. /etc/local-weather-awareness.env sets both paths ---------------------------------
ensure_env_file() {  # ensure_env_file SRC DEST: DEST = SRC + the missing path lines, mode 0644
    local src="$1" dest="$2" add="" key val mode
    for key in WEATHER_OUT_DIR WEATHER_CACHE_DIR; do
        if [ -r "$src" ] && grep -qE "^[[:space:]]*${key}=[[:space:]]*[^[:space:]]" "$src"; then
            continue
        fi
        if [ "$key" = WEATHER_OUT_DIR ]; then val="$OUT_DIR"; else val="$CACHE_DIR"; fi
        add="${add}${key}=${val}"$'\n'
    done
    if [ "$src" != "$dest" ]; then                 # dry run: work on a copy
        if [ -f "$src" ]; then cp -p "$src" "$dest"; else : > "$dest"; fi
    elif [ ! -e "$dest" ]; then
        install -m 0644 /dev/null "$dest"
    fi
    if [ -n "$add" ]; then
        # never glue the first new line onto a last line that lacks its newline
        if [ -s "$dest" ] && [ -n "$(tail -c 1 "$dest")" ]; then printf '\n' >> "$dest"; fi
        printf '# added by deploy/install-cron.sh on %s (the cron wrapper and the installer read it)\n%s' \
            "$(date '+%Y-%m-%d')" "$add" >> "$dest"
        echo "appended to $SYSTEM_ENV_FILE:"
        printf '%s' "$add" | sed 's/^/    /'
    else
        echo "unchanged  $SYSTEM_ENV_FILE (sets WEATHER_OUT_DIR and WEATHER_CACHE_DIR)"
    fi
    mode="$(stat -c %a "$src" 2>/dev/null || echo 644)"
    if [ "$mode" != 644 ]; then
        echo "mode       $SYSTEM_ENV_FILE $mode -> 0644 (the cron job reads it as $RUN_USER)"
    fi
    chmod 0644 "$dest"
}
if [ -n "$DRY_RUN" ]; then
    ensure_env_file "$SYSTEM_ENV_FILE" "$PREVIEW/local-weather-awareness.env"
    echo "  (preview of the result: $PREVIEW/local-weather-awareness.env)"
else
    ensure_env_file "$SYSTEM_ENV_FILE" "$SYSTEM_ENV_FILE"
fi

# ---- 4. crontab line ------------------------------------------------------------------------
crontab_cmd() {  # crontab_cmd ARGS...: crontab(1) for RUN_USER
    if [ "$(id -un)" = "$RUN_USER" ]; then crontab "$@"; else crontab -u "$RUN_USER" "$@"; fi
}
cron_read() {  # cron_read FILE: RUN_USER's crontab into FILE (empty when there is none)
    local rc=0 msg
    : > "$1"
    [ -n "$HAVE_CRONTAB" ] || return 0
    crontab_cmd -l > "$1" 2> "$WORK/crontab.err" || rc=$?
    [ "$rc" -eq 0 ] && return 0
    : > "$1"
    msg="$(head -n 1 "$WORK/crontab.err")"
    if grep -qi 'no crontab' "$WORK/crontab.err"; then
        return 0
    elif [ -n "$DRY_RUN" ]; then
        echo "note: cannot read the crontab of $RUN_USER ($msg); the preview shows the new line alone" >&2
    else
        die "cannot read the crontab of $RUN_USER: $msg (if the user simply has none yet and" \
            "your cron says so differently, create an empty one: crontab -u $RUN_USER /dev/null)"
    fi
}
cron_read "$WORK/crontab.old"
# Drop every earlier local-weather-awareness line (tagged, or calling a wrapper of any
# checkout), keep everything else byte for byte, and add the current line at the end.
grep -v -e "[[:space:]]${CRON_TAG}[[:space:]]*\$" -e '/deploy/local-weather-awareness-cron\.sh' \
    "$WORK/crontab.old" > "$WORK/crontab.new" || true
printf '%s\n' "$CRON_LINE" >> "$WORK/crontab.new"
if cmp -s "$WORK/crontab.old" "$WORK/crontab.new"; then
    echo "unchanged  crontab of $RUN_USER"
    [ -z "$DRY_RUN" ] || cp "$WORK/crontab.new" "$PREVIEW/crontab"
elif [ -n "$DRY_RUN" ]; then
    cp "$WORK/crontab.new" "$PREVIEW/crontab"
    echo "+ crontab -u $RUN_USER $PREVIEW/crontab    (the new crontab of $RUN_USER:)"
    sed 's/^/    /' "$PREVIEW/crontab"
else
    crontab_cmd "$WORK/crontab.new"
    echo "installed  crontab line for $RUN_USER: $CRON_LINE"
fi

# ---- 5. is a cron daemon running? ------------------------------------------------------------
cron_daemon() {  # prints the name of a running cron daemon, nothing when none is found
    local name
    if command -v pgrep >/dev/null 2>&1; then
        for name in crond cron cronie fcron dcron; do       # cronie and dcron run as "crond"
            if pgrep -x "$name" >/dev/null 2>&1; then echo "$name"; return 0; fi
        done
        if pgrep -f 'busybox crond' >/dev/null 2>&1; then echo "busybox crond"; fi
        return 0
    fi
    ps -e -o args= 2>/dev/null | awk '
        { n = $1; sub(/.*\//, "", n) }
        n == "crond" || n == "cron" || n == "cronie" || n == "fcron" || n == "dcron" { print n; exit }
        n == "busybox" && $2 == "crond" { print "busybox crond"; exit }' || true
}
DAEMON="$(cron_daemon)"
if [ -n "$DAEMON" ]; then
    echo "cron:      $DAEMON is running"
else
    cat >&2 <<MSG
WARNING: no running cron daemon found (looked for cronie/crond, cron, fcron, dcron, busybox
         crond), so the job will not run. On Gentoo with OpenRC:
           $GENTOO_CRON_FIX
MSG
fi
if command -v rc-update >/dev/null 2>&1 \
        && ! rc-update show 2>/dev/null | grep -Eq '^[[:space:]]*(cronie|dcron|fcron|cron|vixie-cron|busybox-crond)[[:space:]]*\|'; then
    echo "WARNING: no cron service is in an OpenRC runlevel (rc-update show), so cron will not" \
         "start after a reboot: rc-update add cronie default" >&2
fi

# ---- 6. the Apache snippet ----------------------------------------------------------------
sed_escape() { printf '%s' "$1" | sed -e 's/[|&\\]/\\&/g'; }
render() {  # render SRC DEST MODE: fill placeholders, install only if changed
    local src="$1" dest="$2" mode="$3" tmp left
    tmp="$WORK/render.tmp"
    sed -e "s|__OUT_DIR__|$(sed_escape "$OUT_DIR")|g" \
        -e "s|__CACHE_DIR__|$(sed_escape "$CACHE_DIR")|g" \
        -e "s|__REPO__|$(sed_escape "$REPO")|g" \
        -e "s|__USER__|$(sed_escape "$RUN_USER")|g" \
        "$src" > "$tmp"
    if grep -v '^[[:space:]]*#' "$tmp" | grep -q '__[A-Z_]*__'; then
        left="$(grep -v '^[[:space:]]*#' "$tmp" | grep -o '__[A-Z_]*__' | sort -u | tr '\n' ' ')"
        die "unrendered placeholder in $src: $left"
    fi
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
        echo "unchanged  $dest"
    else
        install -D -m "$mode" "$tmp" "$dest"
        echo "installed  $dest"
    fi
}
SNIPPET="$ETC_DIR/apache-local-weather-awareness.conf"
render "$REPO/deploy/apache-local-weather-awareness.conf" "$SNIPPET" 0644

# ---- 7. one run now, as cron would do it ----------------------------------------------------
# WEATHER_LOG=- shows this run's output here; an env file that sets WEATHER_LOG wins.
FIRST_RUN="cd / && exec env -i HOME=$RUN_HOME LOGNAME=$RUN_USER USER=$RUN_USER SHELL=/bin/sh PATH=$CRON_PATH WEATHER_LOG=- $WRAPPER"
if [ -n "$DRY_RUN" ]; then
    echo "+ su -s /bin/sh $RUN_USER -c '$FIRST_RUN'"
else
    echo "running the generator once as $RUN_USER through the cron wrapper (the first run" \
         "fetches basemap tiles for every site: allow a minute or two)"
    rc=0
    su -s /bin/sh "$RUN_USER" -c "$FIRST_RUN" || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "first run: exit status 0"
    else
        echo "WARNING: the first run exited with status $rc (see the lines above)" >&2
    fi
    if [ -r "$OUT_DIR/status.json" ]; then
        as_run_user "$PY -c 'import json, sys
d = json.load(open(sys.argv[1]))
print(\"status.json: ok=%s page_written=%s problems: %s\" % (
    d.get(\"ok\"), d.get(\"page_written\"), \"; \".join(d.get(\"problems\") or []) or \"none\"))' \
            $OUT_DIR/status.json" 2>/dev/null || true
    fi
fi

# ---- 8. next steps --------------------------------------------------------------------------
if command -v logger >/dev/null 2>&1 && [ -S /dev/log ]; then
    LOGS="syslog, tag local-weather-awareness:
            grep local-weather-awareness /var/log/messages (or wherever your syslog writes)"
else
    LOGS="$CACHE_DIR/cron.log (no logger(1) or /dev/log here; install a syslog daemon to use syslog)"
fi
ENV_LOG="$(env_lookup WEATHER_LOG)"
[ -z "$ENV_LOG" ] || LOGS="$ENV_LOG (WEATHER_LOG from an env file)"
cat <<MSG

Done. cron runs $WRAPPER every 5 minutes as $RUN_USER;
it regenerates $OUT_DIR.
  crontab:  crontab -u $RUN_USER -l
  logs:     $LOGS
  run now:  su -s /bin/sh $RUN_USER -c 'WEATHER_LOG=- $WRAPPER'
  config:   $SYSTEM_ENV_FILE or $RUN_HOME/local-weather-awareness.env
            (see weather.env.example); rerun this installer after changing
            WEATHER_OUT_DIR / WEATHER_CACHE_DIR.
  update:   git pull (or rsync) in $REPO; the next run uses the new code, nothing to
            restart. Rerun this installer when files under deploy/ changed.

Apache: rendered snippet at $SNIPPET
MSG
APACHE_CONF="<your Apache configuration directory>/local-weather-awareness.conf"
if [ -d /etc/apache2/vhosts.d ]; then
    APACHE_CONF=/etc/apache2/vhosts.d/local-weather-awareness.conf
    cat <<MSG
  Gentoo:   cp $SNIPPET $APACHE_CONF
            /etc/init.d/apache2 configtest && rc-service apache2 reload
MSG
elif [ -d /etc/httpd/conf.d ]; then
    APACHE_CONF=/etc/httpd/conf.d/local-weather-awareness.conf
    cat <<MSG
  cp $SNIPPET $APACHE_CONF
  apachectl configtest && apachectl graceful
MSG
elif [ -d /etc/apache2/conf-available ]; then
    APACHE_CONF=/etc/apache2/conf-available/local-weather-awareness.conf
    cat <<MSG
  a2enmod headers
  cp $SNIPPET $APACHE_CONF
  a2enconf local-weather-awareness && apache2ctl configtest && apache2ctl graceful
MSG
else
    echo "  (no /etc/apache2 or /etc/httpd found here: copy it into your Apache configuration)"
fi
DOCROOTS="$(grep -rhE '^[[:space:]]*DocumentRoot[[:space:]]' /etc/apache2 /etc/httpd 2>/dev/null \
            | awk '{ gsub(/"/, "", $2); print $2 }' | sort -u | tr '\n' ' ')" || true
if [ -n "$DOCROOTS" ]; then
    echo "  DocumentRoot(s) configured here: $DOCROOTS"
fi
UNDER_DOCROOT=""
for d in $DOCROOTS; do
    [ "${d%/}/myweather" != "$OUT_DIR" ] || UNDER_DOCROOT="$d"
done
if [ -n "$UNDER_DOCROOT" ]; then
    echo "  $OUT_DIR is <DocumentRoot>/myweather, so /myweather/ works even without the Alias;" \
         "keeping it is harmless, and the snippet also turns off listings and sets cache headers."
else
    echo "  The Alias in the snippet is what maps /myweather/ to $OUT_DIR: install the snippet."
fi
# The page's address: WEATHER_PAGE_URL from the env files, else a guess from this host's name.
PAGE_URL="$(env_lookup WEATHER_PAGE_URL)"
if [ -n "$PAGE_URL" ]; then
    PAGE_BASE="${PAGE_URL%/}"
else
    PAGE_BASE="https://$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo '<your host>')/myweather"
    echo "  WEATHER_PAGE_URL is not set, so the page footer shows no address: add"
    echo "  WEATHER_PAGE_URL=$PAGE_BASE/ (your public address) to $SYSTEM_ENV_FILE"
fi
cat <<MSG
  check:    curl -I $PAGE_BASE/
            curl -s $PAGE_BASE/status.json

Uninstall:
  crontab -u $RUN_USER -l | grep -v '$CRON_TAG\$' | crontab -u $RUN_USER -
  rm -f $APACHE_CONF   (then reload Apache)
  rm -rf $ETC_DIR $SYSTEM_ENV_FILE $OUT_DIR $CACHE_DIR
MSG

#!/usr/bin/bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
force_config=false
review_user=""

usage() {
    echo "Usage: $0 [--review-user USER] [--force-config]"
}

while (($#)); do
    case "$1" in
        --review-user)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            review_user=$2
            shift 2
            ;;
        --force-config)
            force_config=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ((EUID != 0)); then
    escalated_args=()
    [[ -n $review_user ]] && escalated_args+=(--review-user "$review_user")
    [[ $force_config == true ]] && escalated_args+=(--force-config)
    exec sudo -- "$0" "${escalated_args[@]}"
fi

if [[ -z $review_user ]]; then
    if [[ -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then
        review_user=$SUDO_USER
    elif [[ -n ${PKEXEC_UID:-} ]]; then
        review_user=$(getent passwd "$PKEXEC_UID" | cut -d: -f1)
    else
        review_user=$(logname 2>/dev/null || true)
    fi
fi
[[ -n $review_user && $review_user != root ]] || {
    echo "Cannot determine the desktop user; pass --review-user USER." >&2
    exit 1
}
id "$review_user" >/dev/null

missing=()
for command in python3 expac pacman-conf bsdtar runuser less; do
    command -v "$command" >/dev/null || missing+=("$command")
done
if ((${#missing[@]})); then
    echo "Missing runtime commands: ${missing[*]}" >&2
    echo "Install: expac python libarchive util-linux less" >&2
    exit 1
fi

python3 -m py_compile "$project_dir/src/chaotic_review.py"
install -Dm755 "$project_dir/src/chaotic_review.py" /usr/local/bin/chaotic-review
install -d -m755 /var/lib/chaotic-review

if [[ ! -e /etc/chaotic-review.conf || $force_config == true ]]; then
    temporary=$(mktemp)
    trap 'rm -f "$temporary"' EXIT
    sed "s/@REVIEW_USER@/$review_user/g" \
        "$project_dir/config/chaotic-review.conf.in" >"$temporary"
    install -Dm644 "$temporary" /etc/chaotic-review.conf
fi

/usr/local/bin/chaotic-review bootstrap
install -Dm644 "$project_dir/packaging/05-chaotic-review.hook" \
    /etc/pacman.d/hooks/05-chaotic-review.hook

echo "Installed chaotic-review for user $review_user."

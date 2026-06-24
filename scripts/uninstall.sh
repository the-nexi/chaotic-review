#!/usr/bin/bash
set -euo pipefail

state_policy=ask

usage() {
    echo "Usage: $0 [--purge-state|--keep-state]"
}

while (($#)); do
    case "$1" in
        --purge-state)
            state_policy=purge
            shift
            ;;
        --keep-state)
            state_policy=keep
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
    [[ $state_policy == purge ]] && escalated_args+=(--purge-state)
    [[ $state_policy == keep ]] && escalated_args+=(--keep-state)
    exec sudo -- "$0" "${escalated_args[@]}"
fi

if [[ $state_policy == ask ]]; then
    if [[ -t 0 ]]; then
        read -r -p "Remove review state in /var/lib/chaotic-review? [y/N] " answer
        [[ $answer == [yY] || $answer == [yY][eE][sS] ]] && state_policy=purge || state_policy=keep
    else
        echo "Noninteractive uninstall: preserving /var/lib/chaotic-review."
        state_policy=keep
    fi
fi

rm -f /etc/pacman.d/hooks/05-chaotic-review.hook
rm -f /usr/local/bin/chaotic-review
rm -f /etc/chaotic-review.conf

if [[ $state_policy == purge ]]; then
    rm -rf /var/lib/chaotic-review
    echo "Removed chaotic-review and its review state."
else
    echo "Removed chaotic-review; preserved /var/lib/chaotic-review."
fi

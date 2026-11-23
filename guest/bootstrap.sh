#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

stage=$1
shift
case "$stage" in
    prepare)
        command -v rex >/dev/null || { echo 'Install Rex in the guest and put it on PATH.' >&2; exit 1; }
        command -v git >/dev/null || { echo 'Install Git in the guest.' >&2; exit 1; }
        base="$HOME/.rox/$1"
        mkdir -p -m 700 "$base"
        # Projects are checked out under $base/projects, or at the project image's prepared
        # checkout in $HOME/.rox/image/project. The launcher picks which.
        printf '%s\n' "$base" "$base/r.sock"
        ;;
    bake-prepare)
        command -v git >/dev/null || { echo 'Install Git in the guest.' >&2; exit 1; }
        rm -rf "$HOME/.rox/image"
        mkdir -p -m 700 "$HOME/.rox/image"
        printf '%s\n' "$HOME/.rox/image/project"
        ;;
    clone)
        workspace=$1
        url=$2
        branch=$3
        known_hosts="${workspace%/*}/git_known_hosts"
        if [[ $4 == yes ]]; then
            if [[ ! -S ${SSH_AUTH_SOCK:-} ]]; then
                echo 'The guest SSH server did not forward the Git agent.' >&2
                exit 1
            fi
        else
            unset SSH_AUTH_SOCK
        fi

        export GIT_TERMINAL_PROMPT=0 GIT_ALLOW_PROTOCOL=ssh:https
        export GIT_SSH_COMMAND='ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no'
        if [[ -f $known_hosts ]]; then
            printf -v known_hosts_option '%q' "UserKnownHostsFile=\"$known_hosts\""
            GIT_SSH_COMMAND+=" -o $known_hosts_option"
        fi
        if [[ ! -d $workspace/.git ]]; then
            git -c credential.helper= clone --branch "$branch" -- "$url" "$workspace" </dev/null
            exit 0
        fi

        # Bring a project image's checkout up to the branch tip, keeping its prepared files.
        cd "$workspace"
        git remote set-url origin "$url"
        git -c credential.helper= fetch --quiet --no-tags origin \
            "+refs/heads/$branch:refs/remotes/origin/$branch" </dev/null
        tip=$(git rev-parse "refs/remotes/origin/$branch^{commit}")
        if [[ $(git symbolic-ref --quiet --short HEAD || true) != "$branch" || $(git rev-parse HEAD) != "$tip" ]]; then
            git checkout --quiet --force -B "$branch" "$tip"
            git branch --quiet --set-upstream-to="origin/$branch"
        fi
        printf 'Checkout is at %s (%s)\n' "$(git rev-parse --short HEAD)" "$branch" >&2
        ;;
    setup)
        cd "$1"
        export ROX_PROJECT_DIR="$PWD" ROX_NAME="$2" ROX_BACKEND="$3" ROX_PHASE=boot
        export REX_SERVER="unix://$4"
        unset SSH_AUTH_SOCK
        if [[ $5 == prebuilt ]]; then
            printf 'Skipping .rox.sh in %s: it ran when the image was built\n' "$ROX_NAME" >&2
        elif [[ $5 == false && -f .rox.sh ]]; then
            printf 'Running .rox.sh in %s\n' "$ROX_NAME" >&2
            bash .rox.sh </dev/null
        fi
        ;;
    bake)
        cd "$1"
        export ROX_PROJECT_DIR="$PWD" ROX_BACKEND="$2" ROX_NAME="$3" ROX_PHASE=build
        unset REX_SERVER SSH_AUTH_SOCK
        printf 'Running .rox.sh for %s\n' "$ROX_NAME" >&2
        bash .rox.sh </dev/null
        ;;
    server)
        unset SSH_AUTH_SOCK
        base="${1%/*}"
        endpoint="unix://$1"
        rex_ready() {
            [[ -S $1 ]] && rex --autostart=false --timeout 1s -S "$endpoint" ls >/dev/null 2>&1
        }
        if rex_ready "$1"; then
            exit 0
        fi
        log="$base/rex.log"
        nohup rex server run --listen "$endpoint" --state-dir "$base/rex-state" >"$log" 2>&1 </dev/null &
        server_pid=$!
        deadline=$((SECONDS + 10))
        while ((SECONDS < deadline)); do
            if rex_ready "$1"; then
                exit 0
            fi
            kill -0 "$server_pid" 2>/dev/null || break
            sleep 0.2
        done
        echo "Rex did not answer inside the guest at $1." >&2
        cat "$log" >&2
        exit 1
        ;;
    session)
        exec "$@" </dev/null
        ;;
    *)
        printf 'Unknown guest stage %s\n' "$stage" >&2
        exit 1
        ;;
esac

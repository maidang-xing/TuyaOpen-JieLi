#!/bin/bash
set -e

export GIT_TERMINAL_PROMPT=0

# Configure Git buffer and timeouts
git config --global http.postBuffer 104857600
git config --global http.version HTTP/1.1
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 0

git remote add target https://${INPUT_TARGET_USERNAME}:${INPUT_TARGET_TOKEN}@${INPUT_TARGET_URL#https://}

push_with_retry() {
    local refspec="$1"
    local force_flag="$2"
    local max_retries=5
    local count=0
    while true; do
        count=$((count + 1))
        echo "Pushing ${refspec} (attempt ${count}/${max_retries})..."
        if git push -v --progress ${force_flag} target "${refspec}"; then
            echo "Successfully pushed ${refspec}."
            break
        fi
        if [ ${count} -ge ${max_retries} ]; then
            echo "Failed to push ${refspec} after ${max_retries} attempts."
            return 1
        fi
        echo "Push of ${refspec} failed. Waiting 20 seconds before retry..."
        sleep 20
    done
}

get_target_master() {
    local res=""
    for i in 1 2 3 4 5; do
        if res=$(git ls-remote target refs/heads/master | awk '{print $1}'); then
            echo "${res}"
            return 0
        fi
        echo "Failed to query target remote master, retrying ($i/5)..." >&2
        sleep 5
    done
    return 1
}

case "${GITHUB_EVENT_NAME}" in
    push|release|workflow_dispatch)
        TARGET_MASTER=$(get_target_master)
        FIRST_MERGE=$(git log --merges --reverse --format="%H" master | head -n 1)

        # If target has not yet reached the first merge commit, run/resume progressive bootstrap
        if [ -n "${FIRST_MERGE}" ] && { [ -z "${TARGET_MASTER}" ] || ! git merge-base --is-ancestor "${FIRST_MERGE}" "${TARGET_MASTER}"; }; then
            echo "Target Gitee repository needs bootstrap push (Target master: ${TARGET_MASTER:-empty})."

            if [ -z "${TARGET_MASTER}" ]; then
                ROOT_COMMIT=$(git log --reverse --format="%H" master | head -n 1)
                echo "Pushing root commit: ${ROOT_COMMIT}..."
                push_with_retry "${ROOT_COMMIT}:refs/heads/master"
                TARGET_MASTER="${ROOT_COMMIT}"
            fi

            for c in $(git log --reverse --format="%H" ${FIRST_MERGE}^1..${FIRST_MERGE}^2); do
                if [ -n "${TARGET_MASTER}" ] && git merge-base --is-ancestor "${c}" "${TARGET_MASTER}"; then
                    echo "Commit ${c} is already present on target, skipping."
                    continue
                fi
                echo "Bootstrap pushing commit ${c}..."
                push_with_retry "${c}:refs/heads/master" -f
                TARGET_MASTER="${c}"
            done

            echo "Bootstrap pushing first merge ${FIRST_MERGE}..."
            push_with_retry "${FIRST_MERGE}:refs/heads/master" -f
            TARGET_MASTER="${FIRST_MERGE}"

            echo "Bootstrap pushing full master..."
            push_with_retry "master:refs/heads/master" -f
        fi

        echo "Pushing all branches and tags to target..."
        push_with_retry "--all" -f
        push_with_retry "--tags" -f
        ;;
    delete)
        git push -d target ${GITHUB_EVENT_REF}
        ;;
    *)
        push_with_retry "--all" -f
        push_with_retry "--tags" -f
        ;;
esac

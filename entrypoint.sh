#!/bin/bash
set -e

export GIT_TERMINAL_PROMPT=0

# Configure Git buffer and timeouts for large pushes
git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 1000
git config --global http.lowSpeedTime 300

git remote add target https://${INPUT_TARGET_USERNAME}:${INPUT_TARGET_TOKEN}@${INPUT_TARGET_URL#https://}

case "${GITHUB_EVENT_NAME}" in
    push|release|workflow_dispatch)
        # If the target repository is empty, push in two stages to stay well under the Gitee 500MB HTTP limit
        if [ -z "$(git ls-remote target refs/heads/master)" ]; then
            echo "Target Gitee repository is empty. Running bootstrap stage 1..."
            BOOTSTRAP_COMMIT=$(git log --reverse --format="%H" | sed -n '2p')
            if [ -n "${BOOTSTRAP_COMMIT}" ]; then
                echo "Pushing bootstrap commit: ${BOOTSTRAP_COMMIT}..."
                git push -v --progress target ${BOOTSTRAP_COMMIT}:refs/heads/master
            fi
            echo "Pushing bootstrap stage 2: full master..."
            git push -v --progress -f target master:refs/heads/master
        fi
        echo "Pushing all branches and tags to target..."
        git push -v --progress -f --all target
        git push -v --progress -f --tags target
        ;;
    delete)
        git push -d target ${GITHUB_EVENT_REF}
        ;;
    *)
        git push -v --progress -f --all target
        git push -v --progress -f --tags target
        ;;
esac

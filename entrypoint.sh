#!/bin/bash
set -e

export GIT_TERMINAL_PROMPT=0

# Configure Git buffer and timeouts
git config --global http.postBuffer 104857600
git config --global http.lowSpeedLimit 1000
git config --global http.lowSpeedTime 300

git remote add target https://${INPUT_TARGET_USERNAME}:${INPUT_TARGET_TOKEN}@${INPUT_TARGET_URL#https://}

case "${GITHUB_EVENT_NAME}" in
    push|release|workflow_dispatch)
        # If the target repository is empty, push in small stages (<=80MB each) to stay well within Gitee HTTP limits
        if [ -z "$(git ls-remote target refs/heads/master)" ]; then
            echo "Target Gitee repository is empty. Running progressive bootstrap..."
            ROOT_COMMIT=$(git log --reverse --format="%H" master | head -n 1)
            echo "Pushing root commit: ${ROOT_COMMIT}..."
            git push -v --progress target ${ROOT_COMMIT}:refs/heads/master

            FIRST_MERGE=$(git log --merges --reverse --format="%H" master | head -n 1)
            if [ -n "${FIRST_MERGE}" ]; then
                for c in $(git log --reverse --format="%H" ${FIRST_MERGE}^1..${FIRST_MERGE}^2); do
                    echo "Bootstrap pushing commit ${c}..."
                    git push -v --progress target ${c}:refs/heads/master
                done
                echo "Bootstrap pushing first merge ${FIRST_MERGE}..."
                git push -v --progress -f target ${FIRST_MERGE}:refs/heads/master
            fi

            echo "Pushing full master..."
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

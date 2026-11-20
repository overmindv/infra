#!/bin/sh
set -eu
umask 077
export MC_NO_COLOR=1
export MC_CONFIG_DIR
MC_CONFIG_DIR=$(mktemp -d /tmp/overmind-minio.XXXXXX)

fail() {
    printf 'MinIO bootstrap: %s\n' "$1" >&2
    exit 1
}

run() {
    step=$1
    shift
    "$@" >/dev/null 2>&1 || fail "$step failed"
}

ensure_identity() {
    policy=$1
    key=$2
    secret=$3
    expected=$(printf 'AccessKey: %s\nStatus: enabled\nPolicyName: %s\nMemberOf: []' "$key" "$policy")
    if info=$(mc admin user info local "$key" 2>&1); then
        [ "$info" = "$expected" ] || fail "$policy has unexpected IAM bindings or status"
        run "$policy credentials" mc admin user add local "$key" "$secret"
    else
        case "$info" in
            *"The specified user does not exist."*) ;;
            *) fail "cannot check $policy IAM identity" ;;
        esac
        run "$policy credentials" mc admin user add local "$key" "$secret"
        run "$policy binding" mc admin policy attach local "$policy" --user "$key"
    fi
    info=$(mc admin user info local "$key" 2>/dev/null) || fail "cannot verify $policy IAM identity"
    [ "$info" = "$expected" ] || fail "$policy has unexpected IAM bindings or status"
}

run "root login" mc alias set local "${OVM_MINIO_ENDPOINT:-http://minio:9000}" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
for bucket in media-quarantine media sandbox-tests sandbox-artifacts; do
    run "create $bucket" mc mb --ignore-existing "local/$bucket"
    run "privatize $bucket" mc anonymous set none "local/$bucket"
done
run "media policy" mc admin policy create local ovm-media /app/minio/media.json
run "test reader policy" mc admin policy create local ovm-tests-reader /app/minio/tests-reader.json
run "artifact writer policy" mc admin policy create local ovm-artifacts-writer /app/minio/artifacts-writer.json

ensure_identity ovm-media "$MEDIA_S3_ACCESS_KEY" "$MEDIA_S3_SECRET_KEY"
ensure_identity ovm-tests-reader "$SANDBOX_TESTS_ACCESS_KEY_ID" "$SANDBOX_TESTS_SECRET_ACCESS_KEY"
ensure_identity ovm-artifacts-writer "$SANDBOX_ARTIFACTS_ACCESS_KEY_ID" "$SANDBOX_ARTIFACTS_SECRET_ACCESS_KEY"

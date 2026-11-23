#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
work=$(mktemp -d)
db=
media_pid=
cleanup() {
    if [ -n "$media_pid" ]; then
        kill "$media_pid" 2>/dev/null || :
        wait "$media_pid" 2>/dev/null || :
    fi
    if [ -n "$db" ]; then docker stop "$db" >/dev/null 2>&1 || :; fi
    rm -f -- "$work/media" "$work/media.log"
    rmdir -- "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

cd "$root/media"
go build -o "$work/media" ./cmd/media
db=$(docker run -d --rm --publish 127.0.0.1::5432 \
    --tmpfs /var/lib/postgresql:size=256m \
    --env POSTGRES_DB=media_logo_test --env POSTGRES_HOST_AUTH_METHOD=trust postgres:18-alpine)
addr=$(docker port "$db" 5432/tcp)
case "$addr" in 127.0.0.1:*) ;; *) exit 2 ;; esac
tries=0
until docker exec "$db" pg_isready -h 127.0.0.1 -U postgres -d media_logo_test >/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -eq 20 ]; then exit 1; fi
    sleep 1
done
docker exec "$db" createdb -U postgres entities_logo_test
media_dsn="postgres://postgres@$addr/media_logo_test?sslmode=disable"
entities_dsn="postgres://postgres@$addr/entities_logo_test?sslmode=disable"
"$work/media" migrate --dir migrations --dsn "$media_dsn" up
cd "$root/entities"
go run ./cmd/entities migrate --dir migrations --dsn "$entities_dsn" up

read -r owner < /proc/sys/kernel/random/uuid
read -r file < /proc/sys/kernel/random/uuid
read -r blob < /proc/sys/kernel/random/uuid
docker exec -i "$db" psql -U postgres -d media_logo_test -v ON_ERROR_STOP=1 \
    -v "owner=$owner" -v "file=$file" -v "blob=$blob" >/dev/null <<'SQL'
INSERT INTO blobs (id, checksum_sha256, size_bytes, visibility, detected_content_type, object_key, scan_status, scan_verified)
VALUES (:'blob', repeat('a', 64), 16, 'public', 'image/png', 'fixture-logo', 'clean', true);
INSERT INTO files (id, blob_id, owner_user_id, purpose, visibility, original_name, declared_content_type,
                   size_bytes, checksum_sha256, status)
VALUES (:'file', :'blob', :'owner', 'catalog_logo', 'public', 'fixture.png', 'image/png', 16, repeat('a', 64), 'ready');
SQL

token=$(openssl rand -hex 32)
gateway_token=$(openssl rand -hex 32)
users_token=$(openssl rand -hex 32)
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
env -i PATH="$PATH" ENV=local HTTP_ADDR="127.0.0.1:$port" DATABASE_URL="$media_dsn" \
    MEDIA_GATEWAY_TOKEN="$gateway_token" MEDIA_USERS_TOKEN="$users_token" MEDIA_ENTITIES_TOKEN="$token" \
    MEDIA_WORKER_POLL_INTERVAL=1h MEDIA_S3_ENDPOINT=http://127.0.0.1:1 \
    "$work/media" > "$work/media.log" 2>&1 &
media_pid=$!
tries=0
until curl --max-time 2 -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -eq 20 ]; then tail -n 20 "$work/media.log" >&2; exit 1; fi
    sleep 1
done
ENTITIES_TEST_DSN="$entities_dsn" \
    ENTITIES_TEST_LOGO_MEDIA_URL="http://127.0.0.1:$port" ENTITIES_TEST_LOGO_MEDIA_TOKEN="$token" \
    ENTITIES_TEST_LOGO_OWNER="$owner" ENTITIES_TEST_LOGO_FILE="$file" \
    go test -race ./internal/repository -run '^TestLogoBindingWithMedia$' -count=1 -v

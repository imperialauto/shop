#!/bin/sh
# Nightly copy of both databases, proven by restoring into a throwaway server.
# Only reads production. Runs on the db-backup-temp service. Logs counts, never data.
set -u
KEEP=14
stamp=$(date -u +%Y%m%d-%H%M)
web="/backup/nightly-web-$stamp.dump"
msg="/backup/nightly-messaging-$stamp.dump"
fail() { echo "NIGHTLY_BACKUP_FAILED $1"; exit 1; }
count() { psql "$1" -Atc "select $2" 2>/dev/null; }
WEB_COUNTS="(select count(*) from customers)||'/'||(select count(*) from repair_orders)||'/'||(select count(*) from users)||'/'||(select count(*) from dispatch_items)"
MSG_COUNTS="(select count(*) from \"Conversation\")||'/'||(select count(*) from \"Message\")"

live_web=$(count "$DATABASE_URL" "$WEB_COUNTS"); [ -n "$live_web" ] || fail "web_counts"
live_msg=$(count "$MSG_DATABASE_URL" "$MSG_COUNTS"); [ -n "$live_msg" ] || fail "messaging_counts"
pg_dump -Fc "$DATABASE_URL" -f "$web" || fail "web_dump"
pg_dump -Fc "$MSG_DATABASE_URL" -f "$msg" || fail "messaging_dump"

run_as() { if command -v su-exec >/dev/null; then su-exec postgres "$@"; else gosu postgres "$@"; fi; }
data=/tmp/restore-test
rm -rf "$data"; mkdir -p "$data" /tmp/pgsock; chown postgres "$data" /tmp/pgsock
run_as initdb -D "$data" -A trust >/dev/null || fail "initdb"
run_as pg_ctl -D "$data" -w -o "-c listen_addresses='' -c unix_socket_directories=/tmp/pgsock" start >/dev/null || fail "server_start"
local_url() { echo "postgresql://postgres@/$1?host=/tmp/pgsock"; }
restore() {
  psql "$(local_url postgres)" -qc "create database $1" || fail "create_$1"
  pg_restore --no-owner --no-privileges -d "$(local_url "$1")" "$2" >/dev/null 2>/tmp/restore.err
  if [ -s /tmp/restore.err ]; then echo "RESTORE_NOTES $1 $(grep -c 'error:' /tmp/restore.err)"; fi
}
restore web_test "$web"
restore msg_test "$msg"
test_web=$(count "$(local_url web_test)" "$WEB_COUNTS")
test_msg=$(count "$(local_url msg_test)" "$MSG_COUNTS")
run_as pg_ctl -D "$data" -m fast stop >/dev/null
rm -rf "$data"
echo "COUNTS web live=$live_web restored=$test_web messaging live=$live_msg restored=$test_msg"
[ "$test_web" = "$live_web" ] || fail "web_restore_mismatch"
[ "$test_msg" = "$live_msg" ] || fail "messaging_restore_mismatch"

for kind in web messaging; do
  ls -1t /backup/nightly-$kind-*.dump | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; done
done
echo "NIGHTLY_BACKUP_OK $stamp web=$(wc -c < "$web") messaging=$(wc -c < "$msg") kept=$(ls /backup/nightly-*.dump | wc -l)"

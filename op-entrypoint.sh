#!/bin/sh
set -eu

token_file="/run/secrets/op_service_account_token"
if [ ! -f "$token_file" ]; then
  echo "Missing 1Password service account token file: $token_file" >&2
  exit 1
fi

OP_SERVICE_ACCOUNT_TOKEN="$(cat "$token_file")"
export OP_SERVICE_ACCOUNT_TOKEN

if [ "$(id -u)" = "0" ]; then
  ca_src="${MONGO_CA_CERT_PATH:-/etc/ssl/mongo/mongo-ca.crt}"
  if [ -f "$ca_src" ]; then
    ca_dst="/tmp/mongo-ca.crt"
    cp "$ca_src" "$ca_dst"
    chmod 0444 "$ca_dst"
    export MONGO_CA_CERT_SRC="$ca_src"
    export MONGO_CA_CERT_TMP="$ca_dst"
  fi

  exec gosu app op run -- /bin/sh -c '
    if [ -n "${MONGODB_URI:-}" ] && [ -n "${MONGO_CA_CERT_SRC:-}" ] && [ -n "${MONGO_CA_CERT_TMP:-}" ]; then
      if printf "%s" "$MONGODB_URI" | grep -q "tlsCAFile=${MONGO_CA_CERT_SRC}"; then
        MONGODB_URI="$(printf "%s" "$MONGODB_URI" | sed "s|tlsCAFile=${MONGO_CA_CERT_SRC}|tlsCAFile=${MONGO_CA_CERT_TMP}|")"
        export MONGODB_URI
      fi
    fi
    exec "$@"
  ' -- "$@"
fi

exec op run -- "$@"

#!/bin/sh
set -eu
umask 077
config=/home/unitycatalog/etc/conf/server.properties
cp /openbricks/server.properties "$config"

# The local S3 service uses static credentials on an isolated network. This is
# an upstream UC per-bucket compatibility mode, not scoped IAM/STS governance.
if [ -n "${OPENBRICKS_S3_ACCESS_KEY:-}" ] && [ -n "${OPENBRICKS_S3_SECRET_KEY:-}" ]; then
    escape_property() { printf '%s' "$1" | sed 's/\\/\\\\/g'; }
    index=0
    for bucket in $(printf '%s' "${OPENBRICKS_CATALOG_S3_BUCKETS:-openbricks}" | tr ',' ' '); do
        case "$bucket" in *[!a-z0-9.-]*|'') printf '%s\n' 'Invalid catalog S3 bucket name' >&2; exit 1;; esac
        printf '\ns3.bucketPath.%s=s3://%s\ns3.region.%s=%s\ns3.accessKey.%s=%s\ns3.secretKey.%s=%s\ns3.sessionToken.%s=openbricks-local-static\n' \
            "$index" "$bucket" "$index" "${OPENBRICKS_S3_REGION:-us-east-1}" \
            "$index" "$(escape_property "$OPENBRICKS_S3_ACCESS_KEY")" \
            "$index" "$(escape_property "$OPENBRICKS_S3_SECRET_KEY")" "$index" >> "$config"
        index=$((index + 1))
    done
fi
exec /home/unitycatalog/bin/start-uc-server

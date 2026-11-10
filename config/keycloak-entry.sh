#!/bin/bash
set -eu
export KC_DB_PASSWORD="$(cat /run/secrets/db_password)"
export KC_BOOTSTRAP_ADMIN_PASSWORD="$(cat /run/secrets/admin_password)"
exec /opt/keycloak/bin/kc.sh start --optimized --import-realm

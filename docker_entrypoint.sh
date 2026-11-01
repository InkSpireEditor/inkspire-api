#!/bin/sh
# Prepares /data, applies the migrations, then runs what it was given. With no
# arguments that is the server (see the image's CMD); `docker exec` bypasses this file.
set -eu

mkdir -p /data/db /data/files /data/home

alembic upgrade head

exec "$@"

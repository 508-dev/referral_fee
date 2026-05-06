#!/bin/bash
# Dev entrypoint: pip-install this app, then hand off to the real command.
set -e
cd /home/frappe/frappe-bench
./env/bin/pip install -e apps/referral_fee -q
exec "$@"

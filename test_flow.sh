#!/usr/bin/env bash
set -e
PY=python3
Q="$PY queuectl.py"

echo "Resetting DB (for test only) — backing up DB"
if [ -f ~/.queuectl/queuectl.db ]; then
  cp ~/.queuectl/queuectl.db ~/.queuectl/queuectl.db.bak.$(date +%s)
  rm ~/.queuectl/queuectl.db
fi

$PY queuectl.py config set max_retries 2
$PY queuectl.py config set backoff_base 2

echo "Enqueue success job (echo)"
$Q enqueue '{"id":"job_ok","command":"echo hello world","max_retries":2}'

echo "Enqueue failing job (command notfound)"
$Q enqueue '{"id":"job_fail","command":"some_nonexistent_cmd_do_not_exist","max_retries":2}'

echo "Enqueue slow job (sleep 3)"
$Q enqueue '{"id":"job_sleep","command":"sleep 3","max_retries":1}'

echo "Start one worker in background (run in separate terminal ideally):"
echo "  $PY queuectl.py worker start --count 1"
echo "Now run that in another terminal, then monitor status:"
echo "  $PY queuectl.py status"
echo "  $PY queuectl.py dlq list"


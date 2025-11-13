#!/usr/bin/env python3
"""
queuectl.py

Single-file CLI background job queue with:
- SQLite persistence
- Workers (multiprocessing)
- Exponential backoff retries
- Dead Letter Queue (state = 'dead')
- CLI with Click:
    enqueue, worker start/stop, status, list, dlq list/retry, config set/get

Usage examples:
  python queuectl.py enqueue '{"id":"job1","command":"sleep 2","max_retries":3}'
  python queuectl.py worker start --count 2
  python queuectl.py status
"""

import os
import sys
import json
import time
import uuid
import signal
import sqlite3
import shutil
import subprocess
import threading
from datetime import datetime, timezone, timedelta
from multiprocessing import Process, Event, current_process
import click

# ---------------------------
# Config & paths
# ---------------------------
APP_DIR = os.path.expanduser("~/.queuectl")
DB_PATH = os.path.join(APP_DIR, "queuectl.db")
PID_PATH = os.path.join(APP_DIR, "queuectl.pid")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "max_retries": 3,
    "backoff_base": 2,
    "worker_poll_interval": 1.0,
    "job_timeout": 60.0
}

os.makedirs(APP_DIR, exist_ok=True)

# ---------------------------
# DB helpers
# ---------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None,
                           detect_types=sqlite3.PARSE_DECLTYPES)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL,
        max_retries INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        next_run_at TEXT,
        last_error TEXT,
        timeout INTEGER
    )
    """)
    # index to accelerate picking up pending jobs
    cur.execute("CREATE INDEX IF NOT EXISTS idx_state_nextrun ON jobs(state, next_run_at)")
    # Add timeout column if it doesn't exist (for backward compatibility)
    try:
        cur.execute("SELECT timeout FROM jobs LIMIT 1")
    except sqlite3.OperationalError:
        cur.execute("ALTER TABLE jobs ADD COLUMN timeout INTEGER")

    conn.commit()
    conn.close()

init_db()

# ---------------------------
# Config functions
# ---------------------------
def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            try:
                cfg = json.load(f)
            except Exception:
                cfg = {}
    else:
        cfg = {}
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    return merged

def save_config(cfg):
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    with open(CONFIG_PATH, "w") as f:
        json.dump(merged, f, indent=2)

# ensure defaults exist
if not os.path.exists(CONFIG_PATH):
    save_config(DEFAULT_CONFIG)

# ---------------------------
# Utilities
# ---------------------------
def now_iso():
    return datetime.now(timezone.utc).astimezone(timezone.utc).isoformat()

def parse_job_json(arg):
    # arg can be a JSON string or a path to json file
    try:
        data = json.loads(arg)
    except Exception:
        if os.path.exists(arg):
            with open(arg, "r") as f:
                data = json.load(f)
        else:
            raise click.UsageError("Invalid job json input or file not found.")
    return data

# ---------------------------
# Job operations
# ---------------------------
def enqueue_job(job_dict):
    conn = get_conn()
    cur = conn.cursor()
    cfg = load_config()
    job_id = job_dict.get("id") or str(uuid.uuid4())
    command = job_dict.get("command")
    if not command:
        raise ValueError("job must include 'command'")
    max_retries = int(job_dict.get("max_retries", cfg["max_retries"]))
    timeout = int(job_dict.get("timeout", cfg.get("job_timeout", 60)))
    created_at = job_dict.get("created_at", now_iso())
    updated_at = created_at
    attempts = int(job_dict.get("attempts", 0))
    next_run_at = job_dict.get("next_run_at", created_at)
    try:
        cur.execute("""
            INSERT INTO jobs (id, command, state, attempts, max_retries, created_at, updated_at, next_run_at, timeout)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, command, "pending", attempts, max_retries, created_at, updated_at, next_run_at, timeout))
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"Job with id {job_id} already exists")
    finally:
        conn.close()
    return job_id

def get_job(job_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def list_jobs(state=None):
    conn = get_conn()
    cur = conn.cursor()
    if state:
        cur.execute("SELECT * FROM jobs WHERE state = ? ORDER BY created_at", (state,))
    else:
        cur.execute("SELECT * FROM jobs ORDER BY created_at")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def status_summary():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT state, COUNT(*) as cnt FROM jobs GROUP BY state")
    rows = cur.fetchall()
    conn.close()
    return {r["state"]: r["cnt"] for r in rows}

# ---------------------------
# Worker logic
# ---------------------------
def lock_next_job(conn):
    """
    Attempt to atomically pick a pending job whose next_run_at <= now.
    Returns job row dict if locked, else None.
    """
    cur = conn.cursor()
    now = now_iso()
    # Select candidate
    cur.execute("BEGIN IMMEDIATE")
    cur.execute("SELECT id FROM jobs WHERE state = 'pending' AND (next_run_at IS NULL OR next_run_at <= ?) ORDER BY created_at LIMIT 1", (now,))
    row = cur.fetchone()
    if not row:
        conn.commit()
        return None
    candidate_id = row["id"]
    # try to update to 'processing' only if still pending
    updated_at = now_iso()
    cur.execute("UPDATE jobs SET state = 'processing', updated_at = ? WHERE id = ? AND state = 'pending'", (updated_at, candidate_id))
    if cur.rowcount == 1:
        conn.commit()
        cur.execute("SELECT * FROM jobs WHERE id = ?", (candidate_id,))
        job = cur.fetchone()
        return dict(job)
    else:
        conn.commit()
        return None

def run_command(job, timeout=None):
    """
    Run the job command via shell. Returns (success_bool, output_or_error).
    """
    try:
        # Use subprocess.run to capture output and returncode
        completed = subprocess.run(job["command"], shell=True, capture_output=True, text=True, timeout=timeout)
        out = completed.stdout.strip() + ("\n" + completed.stderr.strip() if completed.stderr else "")
        if completed.returncode == 0:
            return True, out
        else:
            return False, f"Exit {completed.returncode}: {out}"
    except FileNotFoundError as e:
        return False, f"FileNotFoundError: {e}"
    except subprocess.TimeoutExpired as e:
        return False, f"TimeoutExpired: {e}"
    except Exception as e:
        return False, f"Exception: {e}"



# ---------------------------
# Worker management (main process)
# ---------------------------
def run_workers_foreground(count):
    """
    Run worker manager in foreground: spawn 'count' worker processes and wait.
    Writes a PID file so `worker stop` can signal this manager.
    """
    # choose to use multiprocessing.Process with a threading.Event per process is impossible cross-process;
    # we'll use multiprocessing.Event from the multiprocessing package via threading.Event in this single-process manager,
    # but simpler: spawn processes that check for a termination file.
    stop_flag = Event()

    def sigterm_handler(signum, frame):
        click.echo("Received termination signal, stopping workers gracefully...")
        stop_flag.set()

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)

    # write manager pid
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    procs = []
    for i in range(count):
        p = Process(target=worker_process_entry, args=(i, PID_PATH))
        p.start()
        procs.append(p)
    try:
        while not stop_flag.is_set():
            time.sleep(0.5)
            # join any that died unexpectedly and restart? for simplicity we will not auto-restart child processes here.
            alive = [p.is_alive() for p in procs]
            if not any(alive):
                # all died
                break
    finally:
        # signal children to stop: create a stop file that children poll
        if os.path.exists(PID_PATH):
            try:
                os.remove(PID_PATH)
            except Exception:
                pass
        click.echo("Waiting for workers to exit...")
        for p in procs:
            p.terminate()
            p.join(timeout=5)
        click.echo("Workers shut down.")

def worker_process_entry(worker_id, pid_path):
    """
    Each worker process: check for existence of pid file to keep running.
    When the manager removes pid file, workers will stop gracefully after finishing job.
    """
    stop_event = False
    click.echo(f"[child-worker-{worker_id}] pid={os.getpid()} started")
    conn = get_conn()
    cfg = load_config()
    poll = float(cfg.get("worker_poll_interval", 1.0))
    while True:
        # check manager pid file presence
        if not os.path.exists(pid_path):
            click.echo(f"[child-worker-{worker_id}] manager pid file removed; stopping after current job.")
            break
        try:
            job = lock_next_job(conn)
            if not job:
                time.sleep(poll)
                continue
            click.echo(f"[child-worker-{worker_id}] executing job {job['id']} cmd='{job['command']}'")
            success, result = run_command(job, timeout=job.get("timeout"))
            now = now_iso()
            cur = conn.cursor()
            if success:
                cur.execute("UPDATE jobs SET state = ?, updated_at = ?, last_error = ? WHERE id = ?", ("completed", now, None, job["id"]))
                conn.commit()
                click.echo(f"[child-worker-{worker_id}] job {job['id']} completed.")
            else:
                attempts = job["attempts"] + 1
                max_retries = job["max_retries"]
                base = int(cfg.get("backoff_base", DEFAULT_CONFIG["backoff_base"]))
                if attempts > max_retries:
                    cur.execute("UPDATE jobs SET state = ?, attempts = ?, updated_at = ?, last_error = ? WHERE id = ?",
                                ("dead", attempts, now, str(result), job["id"]))
                    conn.commit()
                    click.echo(f"[child-worker-{worker_id}] job {job['id']} moved to DLQ (dead). error={result}")
                else:
                    delay_secs = (base ** attempts)
                    next_run = (datetime.now(timezone.utc) + timedelta(seconds=delay_secs)).astimezone(timezone.utc).isoformat()
                    cur.execute("UPDATE jobs SET state = ?, attempts = ?, next_run_at = ?, updated_at = ?, last_error = ? WHERE id = ?",
                                ("pending", attempts, next_run, now, str(result), job["id"]))
                    conn.commit()
                    click.echo(f"[child-worker-{worker_id}] job {job['id']} failed; will retry in {delay_secs}s (attempt {attempts}/{max_retries}).")
        except Exception as e:
            click.echo(f"[child-worker-{worker_id}] error: {e}")
            time.sleep(1)
    conn.close()
    click.echo(f"[child-worker-{worker_id}] exiting.")

def stop_workers_by_pidfile():
    if not os.path.exists(PID_PATH):
        click.echo("No running worker manager found (no pid file).")
        return
    with open(PID_PATH, "r") as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Sent SIGTERM to manager pid={pid}")
    except ProcessLookupError:
        click.echo("Process not found; cleaning up pidfile.")
        try:
            os.remove(PID_PATH)
        except Exception:
            pass

# ---------------------------
# CLI (click)
# ---------------------------
@click.group()
def cli():
    pass

@cli.command(help="Enqueue a job. Argument is JSON string or path to JSON file.")
@click.argument("job_json")
def enqueue(job_json):
    try:
        job = parse_job_json(job_json)
        # normalize and set defaults
        job_id = enqueue_job(job)
        click.echo(f"Enqueued job {job_id}")
    except Exception as e:
        click.echo(f"Failed to enqueue: {e}")
        sys.exit(1)

@cli.group(help="Worker commands")
def worker():
    pass

@worker.command("start", help="Start workers (runs in foreground). Use --count to spawn multiple child worker processes.")
@click.option("--count", "-c", default=1, show_default=True, help="Number of worker processes to spawn")
def worker_start(count):
    click.echo(f"Starting worker manager with {count} workers. PID file: {PID_PATH}")
    run_workers_foreground(count)

@worker.command("stop", help="Stop running workers gracefully (signal manager via pidfile).")
def worker_stop():
    stop_workers_by_pidfile()

@cli.command(help="Show summary of job states & active worker manager")
def status():
    summ = status_summary()
    click.echo("Jobs summary:")
    for k in ["pending", "processing", "completed", "failed", "dead"]:
        click.echo(f"  {k}: {summ.get(k,0)}")
    running = os.path.exists(PID_PATH)
    click.echo(f"Worker manager running: {running}")
    if running:
        with open(PID_PATH, "r") as f:
            click.echo(f"  manager pid: {f.read().strip()}")

@cli.command(help="List jobs. Optional --state to filter.")
@click.option("--state", "-s", default=None, help="Filter by state (pending/processing/completed/dead)")
def list(state):
    rows = list_jobs(state)
    if not rows:
        click.echo("No jobs found.")
        return
    for r in rows:
        click.echo(json.dumps(r, indent=2))

@cli.group(help="Dead Letter Queue (DLQ) commands")
def dlq():
    pass

@dlq.command("list", help="List DLQ jobs (state=dead).")
def dlq_list():
    rows = list_jobs("dead")
    if not rows:
        click.echo("No jobs in DLQ.")
        return
    for r in rows:
        click.echo(json.dumps(r, indent=2))

@dlq.command("retry", help="Retry a job currently in DLQ by id (resets attempts and sets state to pending).")
@click.argument("job_id")
def dlq_retry(job_id):
    job = get_job(job_id)
    if not job:
        click.echo("Job not found.")
        return
    if job["state"] != "dead":
        click.echo("Job is not in DLQ (dead).")
        return
    conn = get_conn()
    cur = conn.cursor()
    now = now_iso()
    cur.execute("UPDATE jobs SET state = ?, attempts = ?, updated_at = ?, next_run_at = ?, last_error = ? WHERE id = ?",
                ("pending", 0, now, now, None, job_id))
    conn.commit()
    conn.close()
    click.echo(f"Job {job_id} re-queued from DLQ.")

@cli.group(help="Configuration commands")
def config():
    pass

@config.command("set", help="Set a configuration key. Example: queuectl config set max-retries 5")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    cfg = load_config()
    # convert kebab-case to snake_case for internal use
    key = key.replace("-", "_")
    # simple cast to int if looks like int
    try:
        if value.isdigit():
            v = int(value)
        else:
            # allow floats e.g. backoff_base could be int but we'll accept ints only for sensible values
            try:
                v = float(value)
            except Exception:
                v = value
    except Exception:
        v = value
    cfg[key] = v
    save_config(cfg)
    click.echo(f"Config updated: {key} = {v}")

@config.command("get", help="Show current configuration")
def config_get():
    cfg = load_config()
    click.echo(json.dumps(cfg, indent=2))

# ---------------------------
# Entry point
# ---------------------------
if __name__ == "__main__":
    cli()


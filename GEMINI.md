# GEMINI.md

## Project Overview

This project is a command-line background job queue system named `queuectl`, written entirely in Python. It provides a robust mechanism for managing background tasks with features like persistent job storage, concurrent workers, automatic retries, and a dead-letter queue.

**Key Technologies & Libraries:**
*   **Language:** Python 3
*   **CLI Framework:** `click`
*   **Database:** SQLite for job persistence (stored at `~/.queuectl/queuectl.db`)
*   **Concurrency:** `multiprocessing` for running multiple worker processes.

**Architecture:**
The system is designed as a single-file Python script (`queuectl.py`) that can be invoked from the command line. It manages a job queue stored in a local SQLite database. Users can enqueue new jobs, start and stop worker processes, and inspect the status of the queue.

*   **Job Lifecycle:** Jobs are enqueued in a `pending` state. A worker process picks up a job, moves it to `processing`, and executes its command. If the command succeeds, the job is marked `completed`. If it fails, the system retries it based on a configurable `max_retries` setting with exponential backoff. After all retries are exhausted, the job is moved to the Dead Letter Queue (`dead` state).
*   **Configuration:** Project configuration is handled via a JSON file located at `~/.queuectl/config.json`.
*   **Process Management:** A PID file (`~/.queuectl/queuectl.pid`) is used to manage the main worker process, allowing for graceful shutdowns.

## Building and Running

### Requirements
*   Python 3.8+
*   `click` library

### Setup
1.  **Install Dependencies:**
    ```bash
    pip install click
    ```
2.  **Make the script executable (optional):**
    ```bash
    chmod +x queuectl.py
    ```

### Key Commands

*   **Start Workers:** Run one or more worker processes in the foreground.
    ```bash
    # Start a single worker
    python queuectl.py worker start

    # Start multiple workers
    python queuectl.py worker start --count 4
    ```

*   **Stop Workers:** Gracefully stop the worker manager and all its child processes.
    ```bash
    python queuectl.py worker stop
    ```

*   **Enqueue a Job:** Add a new job to the queue. The job is defined by a JSON string.
    ```bash
    # Enqueue a simple command
    python queuectl.py enqueue '{"id":"job1", "command":"echo Hello World"}'

    # Enqueue a job with a specified number of retries
    python queuectl.py enqueue '{"id":"job2", "command":"sleep 5", "max_retries":5}'
    ```

*   **Check Status:** Get a summary of job states and see if the worker manager is running.
    ```bash
    python queuectl.py status
    ```

*   **List Jobs:** View all jobs or filter by a specific state.
    ```bash
    # List all jobs
    python queuectl.py list

    # List only pending jobs
    python queuectl.py list --state pending
    ```

*   **Manage Dead Letter Queue (DLQ):**
    ```bash
    # List all jobs in the DLQ
    python queuectl.py dlq list

    # Retry a specific job from the DLQ
    python queuectl.py dlq retry <JOB_ID>
    ```

*   **Configuration:**
    ```bash
    # View current configuration
    python queuectl.py config get

    # Set a configuration value
    python queuectl.py config set max_retries 5
    ```

### Testing
The `test_flow.sh` script provides a simple workflow for testing the system. It clears the database, enqueues a few sample jobs, and provides instructions for starting a worker and monitoring its progress.

```bash
./test_flow.sh
```

## Development Conventions

*   **Single-File Structure:** All application logic is contained within `queuectl.py`.
*   **State Management:** The state of the application (jobs, configuration) is stored in the user's home directory (`~/.queuectl/`).
*   **Error Handling:** The script includes error handling for database operations and command execution. Failed jobs include a `last_error` message.
*   **CLI Design:** The command-line interface is organized into logical groups (`worker`, `dlq`, `config`) using `click`.
*   **Code Style:** The code is well-commented and follows standard Python conventions.

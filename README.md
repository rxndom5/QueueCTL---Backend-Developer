# queuectl — CLI Background Job Queue

`queuectl` is a minimal, production-minded background job queue system built in Python. It allows you to enqueue jobs, process them with multiple workers, handle failures with exponential backoff retries, and manage a Dead Letter Queue (DLQ) for jobs that fail permanently.

All operations are accessible via a command-line interface, and job data is persisted in a local SQLite database.


The system is designed around a few core concepts:

*   **Job Lifecycle**: Jobs transition through states like `pending`, `processing`, `completed`, and `dead`. This allows for clear tracking and management.
*   **Persistent Queue**: An SQLite database (`~/.queuectl/queuectl.db`) stores all job information, ensuring that no data is lost between restarts.
*   **Concurrent Workers**: The system uses Python's `multiprocessing` module to run multiple worker processes in parallel, allowing for high-throughput job execution.
*   **Atomic Operations**: To prevent race conditions (like two workers processing the same job), database transactions are used to atomically lock a job before processing begins.
*   **Exponential Backoff**: Failed jobs are not retried immediately. Instead, an exponential backoff algorithm calculates a delay, preventing struggling services from being overwhelmed. The delay is calculated as `delay = base ^ attempts`.
*   **Dead Letter Queue (DLQ)**: After a configurable number of retries (`max_retries`), a job is moved to the `dead` state. From here, it can be inspected manually or retried.
*   **Graceful Shutdown**: Workers can be stopped gracefully. They will finish their current job before exiting, preventing data corruption.

## Setup Instructions

1.  **Prerequisites**:
    *   Python 3.8+

2.  **Installation**:
    Clone the repository and install the `click` dependency.
    ```bash
    git clone <your-repo>
    cd <your-repo>
    pip install click
    ```

3.  **Initialize Database**:
    The database and configuration files are created automatically on the first run in `~/.queuectl/`.

## Usage Examples

### Workers

Start one or more worker processes. They will run in the foreground and begin processing jobs.

*   **Start a single worker:**
    ```bash
    python queuectl.py worker start
    ```
*   **Start three workers:**
    ```bash
    python queuectl.py worker start --count 3
    ```

*   **Stop all workers:**
    This sends a graceful shutdown signal to the worker manager.
    ```bash
    python queuectl.py worker stop
    ```

### Enqueueing Jobs

Add a new job to the queue. The only required field is `command`.

*   **Enqueue a simple job:**
    ```bash
    python queuectl.py enqueue '{"command": "echo Hello World"}'
    ```

*   **Enqueue a job with a unique ID and retry limit:**
    ```bash
    python queuectl.py enqueue '{"id": "my-unique-job", "command": "sleep 5", "max_retries": 5}'
    ```

### Monitoring and Status

Check the state of the queue and view job details.

*   **Get a summary of all job states:**
    ```bash
    python queuectl.py status
    ```
    *Example Output:*
    ```
    Jobs summary:
      pending: 5
      processing: 2
      completed: 52
      failed: 0
      dead: 1
    Worker manager running: True
      manager pid: 12345
    ```

*   **List all jobs:**
    ```bash
    python queuectl.py list
    ```

*   **List only pending jobs:**
    ```bash
    python queuectl.py list --state pending
    ```

### Dead Letter Queue (DLQ)

Manage jobs that have permanently failed.

*   **List all jobs in the DLQ:**
    ```bash
    python queuectl.py dlq list
    ```

*   **Retry a job from the DLQ:**
    This resets the job's attempts and moves it back to the `pending` state.
    ```bash
    python queuectl.py dlq retry <JOB_ID>
    ```

### Configuration

Configure settings like the default number of retries and the backoff base.

*   **View the current configuration:**
    ```bash
    python queuectl.py config get
    ```

*   **Set the default max retries for new jobs:**
    ```bash
    python queuectl.py config set max-retries 5
    ```

*   **Set the exponential backoff base:**
    ```bash
    python queuectl.py config set backoff-base 3
    ```

    *Note: Configuration keys provided via the CLI use kebab-case (e.g., `max-retries`), which are automatically converted to snake_case (e.g., `max_retries`) for internal storage in `config.json`.*

## Testing Instructions

A simple test script, `test_flow.sh`, is included to validate the core functionality. It clears the database, enqueues a mix of successful and failing jobs, and provides instructions for you to see the system in action.

1.  **Run the test script:**
    ```bash
    ./test_flow.sh
    ```
2.  **In a separate terminal, start a worker:**
    ```bash
    python queuectl.py worker start
    ```
3.  **Monitor the status in the first terminal:**
    ```bash
    python queuectl.py status
    # Wait a few seconds and run again to see the jobs being processed
    python queuectl.py status
    # Check the DLQ for the failed job
    python queuectl.py dlq list
    ```

## Demo Video

A live demonstration of the CLI in action can be found here: [Link to Video on Google Drive/Dropbox]

## Assumptions & Trade-offs

*   **Single-File Simplicity**: The entire application is in a single Python file. This makes it easy to understand and deploy for this assignment's scope, but for a larger system, it would be better to split the logic into multiple modules (e.g., `db.py`, `worker.py`, `cli.py`).
*   **SQLite for Persistence**: SQLite is simple, serverless, and sufficient for this use case. However, it may not be suitable for extremely high-throughput environments where a dedicated database server like PostgreSQL would be more appropriate. The use of `PRAGMA journal_mode=WAL` is a good compromise for improving concurrency.
*   **Local Process Management**: Worker management is handled via a PID file. This is effective for a single-machine setup but would not scale across multiple machines. A distributed system would require a more robust solution like Redis or a dedicated service discovery tool.
*   **No Job Output Logging**: The system does not store the `stdout` or `stderr` of successfully completed jobs to save database space. Only the `last_error` is stored for failed jobs.
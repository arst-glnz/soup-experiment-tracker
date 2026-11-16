# Design note

## Event format and ordering

Each metric point is addressed by `(run_id, metric, step)`. A batch has a
client-generated `batch_id`; retries are safe because point inserts use a
unique key and conflicting retries do not overwrite the first value. The
SQLite transaction inserts every point and then one event, so an event is
never visible without its data. Event IDs are database-assigned and are only
used as a replay cursor; metric ordering is `metric, step`, not arrival order.
The client treats the database response as canonical after every event.

## Disconnect and isolation

The SSE endpoint accepts `Last-Event-ID` and replays all durable events after
that cursor. The browser then refetches the complete run, which also handles a
connection that drops between replay and rendering. Every run lookup, metric
write, session, and stream first resolves the API key/session to a project and
queries with both project and run IDs. IDs are UUIDs, but authorization does
not rely on unguessability. API keys are stored hashed and rotation revokes
all prior keys. Cookies are HttpOnly and SameSite=Strict.

## Evidence

`test_out_of_order_duplicate_late_and_reconnect_converge` sends out-of-order,
duplicate, and late batches and resumes SSE after an artificial cursor gap.
The final API state is the expected database state. This test would **not**
catch browser rendering bugs, a multi-process race, a lost event after the
retention window, network proxy buffering, or a failure caused only by a real
slow consumer.

`test_project_isolation_in_api_sse_and_key_rotation` checks cross-project
read/write/stream access and old-key revocation. It also checks that a valid
key cannot use another project's run ID. Logs are not allowed to include keys;
production should add structured redaction tests. Timing and existence
side-channels remain concerns: all unauthorized run responses are normalized
to 404, but rate limiting and constant-time policy need production hardening.

## Silent-failure review

Reconnect loss: replay cursor plus canonical refetch. Duplicates: unique
point key and batch retry test. Slow queue overflow: this implementation
does not queue unboundedly; it replays from durable storage, but production
needs connection limits and metrics. Partial writes: one transaction and
rollback. Key rotation: old-key request is tested and rejected. A broken
database raises an HTTP 500 rather than returning success-shaped output.

## Verdict

**DON'T SHIP unchanged for 50 concurrent runs at 10 events/second.** SQLite
WAL is useful for a take-home but is not the right write path or event broker
for that load. First change: PostgreSQL transactions with an outbox table,
retention/compaction, and a bounded SSE broker with replay metrics; then load
test 500 events/second and verify p95 latency and zero missing event IDs.

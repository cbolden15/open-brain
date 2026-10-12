# Existing local Brain transactions

Trusted foreground callers can hold the existing Brain's runtime admission and canonical writer around a synchronous SDK operation. This is useful when the caller must inspect state before startup and verify it after startup, mutation and teardown have finished.

```python
from open_brain.services.local_bootstrap import hold_existing_local_brain_transaction

with hold_existing_local_brain_transaction(existing_root) as transaction:
    before_check()
    result = transaction.run(synchronous_sdk_operation)
    after_check()
    transaction.validate()
    authorize_release(result)
```

The root must already be initialized, with its existing private writer and runtime registry controls. Entry reads the existing profile, acquires exclusive runtime admission and then holds the canonical writer. It rejects live foreground peers. It does not create a Brain, open the engine, run recovery or grant permission for an operation.

`run` allows the existing bootstrap and engine to borrow those exact barriers during one synchronous callback. SDK startup, normal task guards, migration checks, session registration and session cleanup still run. Cleanup finishes before `run` returns. The enclosing context holds both barriers until exit and performs no engine recovery after the caller's final checks.

Ownership is bound to the root identity, issuing process, thread and context lifetime. Nested or asynchronous callbacks refuse. Ordinary writer acquisitions remain non-reentrant outside `run`. A borrowed reader runs under the outer exclusive writer; other lock scopes retain their existing behavior. Only the owner closes or unlocks the canonical writer descriptor.

The caller must enforce current authorization, protection and result release. Independent outboxes, configuration and consent stores have their own concurrency rules; holding the Brain writer does not serialize them. This API adds no network, custody, encryption or service dependencies.

Exclusive runtime admission excludes supported app clients from SDK startup and canonical ingestion while the transaction is held. The canonical writer serializes materialization. Direct engine callers do not enter the app registry, and schema-10 capture deliberately records ingestion custody before acquiring the writer so an ordinary busy writer can still return a queued receipt. Such direct callers, and raw SQLite writers, require separate exclusion by the embedding caller. The transaction does not change that custody contract.

POSIX record locks are process-wide. Code running in the transaction's process must not open and close another descriptor for the canonical writer inode, including while scanning the Brain for snapshots. Run that inventory in a separate foreground process when it needs to read the lock file. `validate` checks ownership capability identity, control-file identity and lifetime; it cannot independently prove kernel lock continuity after arbitrary code closes a conflicting descriptor. Reacquiring a lost lock does not establish uninterrupted exclusion.

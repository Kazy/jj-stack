# Generated integration testing

These constraints supplement the [testing philosophy](testing-philosophy.md):

- In `StackMachine`, keep server events and recovery as separate actions. A server merge must
  leave room for local edits before sync; an interrupted submit must leave room for edits or
  remote changes before retry. Combining these actions would hide the inconsistent states the
  harness exists to test.
- An external stack merge may leave the survivors' rewrite pending, and `server_rewrite`
  completes it later, rooted on trunk's tip at that time as GitHub does, optionally after
  advancing trunk itself. Keep the merge and the rewrite separate actions so local work and
  other server events can happen between them.
- `interrupt_sync` fails a sync after it has rewritten the local stack and updated the
  surviving PRs but before it cleans up the merged ones, the state a crashed or interrupted sync
  leaves behind. Recovery must come from a later `sync`, `sync --all`, or `cleanup`, so keep the
  interruption separate from those actions.
- Shared-file edits use single-line replacements, and generated moves preserve the relative order
  of changes that edit the same file. These restrictions let the harness predict conflicts without
  implementing `jj`'s merge algorithm; broadening the edits requires revisiting that assumption.
- Each independent seeded search (shard) in the property tests has its own Hypothesis example
  database. Sharing a database would spend parallel search time replaying and shrinking the same
  saved failure.

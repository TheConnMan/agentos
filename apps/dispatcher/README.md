# apps/dispatcher

Owning task: **E1**. Slack Bolt for Python in Socket Mode: acks Slack events in under three seconds with a placeholder, then enqueues onto Valkey Streams with an idempotency key, under reconnect supervision. Verifies Slack-free via the Bolt test harness (synthetic events); a real workspace is only needed at the walking-skeleton gate. R0 ships only an empty importable skeleton so the workspace lint and test harness is green.

---
project: Atlas
date: 2025-03-01
tags: [ops, runbook]
---
# Production deployment runbook

## Deploy steps
1. Merge the release branch and wait for the CI pipeline to finish.
2. Build the Docker image and push it to the internal registry.
3. Run the Helm upgrade against the Kubernetes production namespace.
4. Verify that the PostgreSQL schema migration job completed successfully.
5. Watch the error rate dashboard for ten minutes before announcing the release.

## Rollback
Run the Helm rollback command to the previous revision. Database migrations are backward compatible for one release so rollback never needs a schema change.

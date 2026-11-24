---
project: Atlas
date: 2025-04-20
tags: [ops, oncall]
---
# On-call guide

## First response
Acknowledge the page within five minutes. Check the error rate dashboard and the recent deploy list before touching anything.

## Database failover
If the primary database node becomes unavailable, Patroni promotes a replica automatically within about 30 seconds. Verify the new primary accepts writes, then update the PgBouncer target if the promotion did not do it.

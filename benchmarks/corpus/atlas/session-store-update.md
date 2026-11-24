---
project: Atlas
date: 2025-06-02
tags: [auth, ops]
---
# Session store migration

After the logout storm we migrated from Redis to Memcached for session caching. Sessions are now stored with explicit TTLs and the old Redis instance only serves the revocation list.

## Settings
The default timeout is 60 seconds for session lookups after the migration. The Memcached cluster has 3 nodes.

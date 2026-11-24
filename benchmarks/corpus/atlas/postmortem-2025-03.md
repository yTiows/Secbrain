---
project: Atlas
date: 2025-03-20
tags: [incident, postmortem]
---
# Postmortem: logout storm

## What happened
On March 19 thousands of users were logged out within a few minutes. Redis reached its memory limit and started evicting session keys, so the auth service treated valid sessions as expired.

## Lessons learned
We learned that the Redis session store had no memory alerts and no eviction policy review. We tried raising the memory limit but the storm returned a week later. The team concluded that sessions should not share an instance with cache data.

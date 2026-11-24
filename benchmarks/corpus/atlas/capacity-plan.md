---
project: Atlas
date: 2025-04-08
tags: [capacity, research]
---
# Capacity planning

The order service handles 800 orders per second at peak in load tests. PostgreSQL storage grows by roughly 40 GB per month, so the primary volume must be expanded every quarter. The payments service is not the bottleneck; the ledger writes are.

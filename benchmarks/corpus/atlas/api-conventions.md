---
project: Atlas
date: 2025-02-05
tags: [api, guidelines]
---
# API conventions

All endpoints return JSON and use cursor pagination. Always send an Idempotency-Key header on POST requests that create charges or orders. Never expose internal database identifiers in public responses. Errors use a problem-details body with a stable error code.

---
project: Atlas
date: 2025-07-10
tags: [security, review]
---
# Security review findings

The access token lifetime is 30 minutes according to the production configuration we audited. The reviewers flagged that this is longer than the documented policy and recommended shortening it.

## Recommendations
Enable mutual TLS between internal services. Rotate the PgBouncer credentials quarterly and store them in the secrets manager.

---
project: Atlas
date: 2025-01-22
tags: [vendor, decision]
---
# Payment vendor selection

We chose Stripe as the card processor for Atlas instead of Adyen. Stripe has better developer tooling and webhook signing. Webhook payloads must be verified with the signing secret before they are trusted. Fees are 2.9 percent plus a fixed amount per transaction.

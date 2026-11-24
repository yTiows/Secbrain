---
project: Atlas
date: 2025-01-15
tags: [architecture, platform]
---
# Atlas platform architecture

Atlas is the order and payments platform for our online marketplace. It is split into an API gateway, an auth service, a payments service and an order service.

## Services
The API gateway terminates TLS and routes traffic to the internal services. The gateway runs on Kubernetes and scales horizontally. Each service owns its own schema in PostgreSQL.

## Messaging
We decided to use RabbitMQ for asynchronous events between the order service and the payments service. Kafka was considered but rejected because the team has no operational experience with it and our volume is modest.

## Data storage
PostgreSQL is the system of record for orders and the payment ledger. See the database decision record for the rationale.

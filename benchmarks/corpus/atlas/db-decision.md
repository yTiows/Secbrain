---
project: Atlas
date: 2025-02-10
tags: [database, decision]
---
# Database decision record

PostgreSQL, also known as Postgres, is the primary database for Atlas.

## Decision
We decided to use PostgreSQL for the primary database. The team compared PostgreSQL with MySQL and DynamoDB over two weeks.

## Rationale
PostgreSQL supports JSONB columns, strong transactional guarantees and mature streaming replication. The ledger needs serializable transactions, which MySQL handled poorly in our tests. The default timeout is 30 seconds for every query issued by the services.

## Connection pooling
Each service connects through PgBouncer in transaction pooling mode. The pool size is capped at 50 connections per service to avoid exhausting Postgres backends.

## Open questions
What is the failover plan for the primary node? Should we adopt Patroni for automatic failover?

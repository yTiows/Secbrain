# Notes on Designing Data-Intensive Applications

Replication keeps copies of data on several nodes. Postgres uses write-ahead log shipping for streaming replication. Partitioning spreads data across nodes; consensus protocols decide leaders after failures.

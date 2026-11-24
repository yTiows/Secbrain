---
project: Atlas
date: 2025-03-12
tags: [frontend]
---
# Storefront frontend

The storefront is a React application written in TypeScript. It is built by the CI pipeline and published to the CDN.

## Release steps
1. Run the unit tests and the visual regression suite.
2. Build the production bundle.
3. Upload the bundle to the CDN bucket and purge the cache.

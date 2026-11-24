---
project: Atlas
date: 2025-02-25
tags: [auth, security]
---
# Auth service

The auth service issues JSON Web Token (JWT) credentials to clients after login. The access token lifetime is 15 minutes. Clients renew access using a refresh token that is valid for 30 days.

## Session handling
The auth service uses Redis for the session store and token revocation list. Refresh tokens are rotated on every use, and a reused refresh token revokes the whole session family.

## Signing keys
Tokens are signed with RS256. Keys rotate every 90 days and the previous key stays valid for verification during a 24 hour overlap window.

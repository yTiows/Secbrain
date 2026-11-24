---
project: Orion
date: 2025-02-02
tags: [python, guidelines]
---
# Python environment guidelines

Always create a virtual environment for every Orion experiment. Pin dependency versions in a lock file. Never install packages globally on the shared research machines.

## Notebooks
Do not commit notebook outputs. Restart and run all cells before sharing a notebook with the team.

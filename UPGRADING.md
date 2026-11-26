# What a project changes after taking a version

One section per public release, newest first: what a project does in its own
files after moving to a new release. Pin a release with
`pip install git+https://github.com/Arp-299-Org/board-graph.git@v<version>`
(nothing in a project changes until you choose to upgrade). Read every section
between your old version and the new one before upgrading to see whether any
project-owned files need updates. A release that asks nothing of a project
still has a section saying so.

## 0.33.1

Initial public release of `board-graph` (MIT license, KiCad 10, Python 3.9+).
Nothing has to change in existing projects already initialized with
`board-graph init`. Includes the full graph engine, KiCad 10 IPC and
interactive routing workflow, explicit delegated autonomous mode with
owner-locked `order` guardrails, single-machine and SSH KiCad host modes,
Linux `Xvfb` Docker smoke testing, and self-contained wheel packaging.

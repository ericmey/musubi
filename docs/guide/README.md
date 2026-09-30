# Musubi user guide

Start here if you want to run Musubi and point your agents at it. The
[architecture docs](../Musubi/README.md) explain how it's built; this guide
covers how to use it.

1. [Install](install.md): try it on one machine, then run it for real.
2. [Connect an agent](connect.md): tokens, then a plugin for your agent host.
3. [Use it](use.md): namespaces, the three planes, capture and recall.
4. [Operate it](operate.md): health, upgrades, backups and alerts.

Musubi is one HTTP service (`/v1`) backed by Qdrant, local embedding models
and a local LLM. Every agent integration is a separate package that talks
to that API.

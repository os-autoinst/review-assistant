# Listening Daemon with Outbound Conditional Polling

Status: accepted

To review pull requests on `os-autoinst/openQA` continuously without human intervention, `review-assistant` runs as a 24/7 Kubernetes `Deployment` (`replicas: 1`, `strategy: Recreate`) rather than an hourly `CronJob`. To operate inside SUSE OpenPlatform Prague behind corporate firewall boundaries without inbound public webhooks, the daemon polls `GET /repos/os-autoinst/openQA/pulls` using HTTP `ETag` (`If-None-Match`) conditional headers every 60 seconds and dispatches reviews through a sequential single-worker queue.

## Considered Options

- **Kubernetes CronJob (hourly batch)**: Rejected because batch scheduling delays feedback to pull request authors by up to one hour and runs against the continuous review requirement.
- **Inbound GitHub Webhooks**: Rejected because OpenPlatform clusters resolve to private RFC-1918 addresses without public ingress or DMZ access from GitHub webhook dispatchers.
- **Outbound Event Stream Polling (`/events`)**: Rejected because polling event streams cannot guarantee recovery after pod restarts or downtime, whereas conditional `/pulls` polling catches up missed changes automatically.

## Consequences

- The daemon requires zero open ports, eliminating external network attack surface and simplifying Kubernetes NetworkPolicy.
- Conditional requests returning HTTP `304 Not Modified` do not consume GitHub API rate-limit quota during idle periods.
- Kubernetes liveness probes rely on a file-based heartbeat check (`/tmp/heartbeat`) updated on every poll cycle.
- Volume mounts on the `ReadWriteOnce` deduplication state PVC require `strategy: Recreate` so terminating pods release the volume before replacement pods start.

# F01 Crash loop after a config change

Root cause: the service, bad release. First action: **rollback**.

![Restarts of the new release](img/F01.png)

## Symptoms

- New pods of one service restart again and again: `CrashLoopBackOff`.
  *Service RED*, panel *Restarts*; *Platform*, panel *Restarts*.
- The rollout does not progress, and after its progress deadline (5 minutes)
  Argo Rollouts aborts it on its own.
- Users usually notice nothing: the old pods keep serving, because a rollout
  never takes an old pod away before a new one is ready. The incident is a
  release that cannot land, and capacity that cannot grow.

## Checks

1. Which pods restart, and since when?
   `sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="shopsearch"}[10m]))`
2. Do the restarting pods belong to the newest release? Compare their
   `rollouts-pod-template-hash` label with `kubectl -n shopsearch get rollout <service> -o jsonpath='{.status.currentPodHash} {.status.stableRS}'`.
   Only the new hash restarting means the release, not the node or a dependency.
3. Why does it exit? `kubectl -n shopsearch logs <pod> --previous`. A config
   error is usually the last lines: a value that does not parse, a host that
   does not resolve.
4. What changed? The service's Application history shows the change that
   started the release: an environment variable, a config value, an image.

Rule out: a dependency that is down makes a service unready, not crash at start
(F10, F09); an OOM kill shows `OOMKilled` as the last state, not an exit code (F04).

## Fix

Roll back the change: revert it in Git, and ArgoCD rolls the service back. If
the rollout is already aborted, the old pods are serving and only Git still
holds the bad value: revert it anyway, or the next sync tries again.

## Prevention

- Validate configuration at build time (schema-check the values) and at start,
  with an error that names the variable, as the services here do.
- Keep `progressDeadlineAbort`: it turns a release that never gets ready into an
  automatic abort instead of a stuck rollout.

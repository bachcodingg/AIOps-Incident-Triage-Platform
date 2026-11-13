#!/usr/bin/env bash
# Raises the kernel's inotify limits for a kind cluster.
#
# Every pod's file watchers (config reloaders, cert watchers, log tailers) count
# against the host kernel's limits, and GitHub's runners allow only 128 inotify
# instances. With the full platform, chaos-controller-manager then dies at start
# with "too many open files". kind's nodes are privileged containers in the
# host's user namespace, so they can raise the limits without sudo on the host.
# A limit already higher is left alone.
#
#   NODE  the kind node container, e.g. incidentlens-control-plane
set -euo pipefail

NODE="${NODE:?set NODE to a kind node container}"

docker exec "$NODE" sh -c '
  for kv in max_user_instances=512 max_user_watches=524288; do
    f=/proc/sys/fs/inotify/${kv%=*}; want=${kv#*=}
    [ "$(cat "$f")" -ge "$want" ] || echo "$want" > "$f"
    echo "fs.inotify.${kv%=*} = $(cat "$f")"
  done
' || echo "warning: could not raise the inotify limits; pods may fail with \"too many open files\"" >&2

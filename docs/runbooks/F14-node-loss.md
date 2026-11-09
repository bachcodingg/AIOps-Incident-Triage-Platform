# F14 Node loss

Root cause: the node, infrastructure. First action: **verify** that it recovers.

![A node going NotReady](img/F14.png)

## Symptoms

- A node goes `NotReady` (*Platform*, *Nodes ready*) about 40 s after it stops
  answering.
- Every pod on it stops at once, whatever service it belongs to: errors and
  latency in several services together, and fewer pods behind each Service.
- Pods on the lost node stay `Running` in the API for 5 minutes, then are
  evicted and recreated on the other nodes.

## Checks

1. `kubectl get nodes` and `kubectl describe node <node>`: conditions and the
   time the kubelet stopped posting status.
2. Which pods were there? `kubectl get pods -A -o wide --field-selector spec.nodeName=<node>`.
   The services with errors are the ones that had pods on it.
3. Nothing else explains it: no release, and the other nodes are healthy.

Rule out: one service failing alone is not a node; DNS failures (F09) also hit
everything but leave the nodes Ready.

## Fix

The platform heals this: the node comes back, or its pods are rescheduled after
5 minutes. Verify it did. Every service has ready pods again, the error ratio is
back to normal, and nothing is left `Pending` for lack of room on the
remaining nodes. If pods stay Pending, that is the escalation: the cluster has
too little spare capacity.

## Prevention

- At least two replicas of each service, spread over nodes (the services use a
  topology spread constraint; on kind they run one replica each to fit a laptop).
- Spare capacity for the loss of the largest node.
- PodDisruptionBudgets, so planned node drains never take a whole service down.

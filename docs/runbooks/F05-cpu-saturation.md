# F05 CPU saturation

Root cause: the service, resource. First action: **scale out**.

![CPU throttling](img/F05.png)

## Symptoms

- One service's pods are throttled: they hit their CPU limit and wait.
  *Platform*, panel *CPU throttling*, goes from near 0 to 25 % and more.
- That service's latency rises, more at peak traffic than at night.
- The HorizontalPodAutoscaler may already be adding pods: the replica count goes
  up on its own.

## Checks

1. Which container is throttled, and is its CPU use at the limit?
   `sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="shopsearch",container="<service>"}[5m]))`
   against `kube_pod_container_resource_limits{resource="cpu",container="<service>"}`.
2. Is it more work, or the same work costing more? Compare the request rate
   (*Platform*, *Requests per second*) with the CPU: CPU up and traffic flat is
   saturation from inside; both up together is a traffic spike (F06).
3. Is it on every pod of the service, or on one node? One node means a noisy
   neighbour or the node itself.
4. Was there a release? A release that made the code slower shows as latency
   from its first minute and on its new pods only (F03).

## Fix

Scale out: more pods share the load, each one under its limit. Then find what is
burning the CPU (a profile, or the traces with the largest self-time).

## Prevention

- Requests and limits that leave headroom at peak, and an HPA whose target
  (70 % here) leaves time to react.
- An alert on throttling above 25 % for 10 minutes, which fires before users
  see the latency.

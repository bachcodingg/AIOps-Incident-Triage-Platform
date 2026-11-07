#!/usr/bin/env bash
# Fails if anything that bills by the hour is still there. It asks AWS directly
# rather than trusting Terraform, which only knows what it created.
set -euo pipefail

REGION="${AWS_REGION:-eu-central-1}"
CLUSTER="${CLUSTER:-incidentlens}"
found=0

check() {
  local what=$1 result
  shift
  result=$("$@" --region "$REGION" --output text)
  if [[ -n "$result" && "$result" != "None" ]]; then
    printf '  \033[31mLEFT\033[0m %s: %s\n' "$what" "$result"
    found=1
  else
    printf '  \033[32mgone\033[0m %s\n' "$what"
  fi
}

check "EKS cluster" aws eks list-clusters --query "clusters[?@=='$CLUSTER']"
check "EC2 instances" aws ec2 describe-instances \
  --filters "Name=tag:eks:cluster-name,Values=$CLUSTER" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId'
check "NAT gateways" aws ec2 describe-nat-gateways \
  --filter "Name=tag:Project,Values=incidentlens" "Name=state,Values=pending,available,deleting" \
  --query 'NatGateways[].NatGatewayId'
check "elastic IPs" aws ec2 describe-addresses \
  --filters "Name=tag:Project,Values=incidentlens" --query 'Addresses[].AllocationId'
check "load balancers" aws elbv2 describe-load-balancers \
  --query "LoadBalancers[?contains(LoadBalancerName, 'k8s-') || contains(DNSName, '$CLUSTER')].LoadBalancerArn"
check "classic load balancers" aws elb describe-load-balancers \
  --query 'LoadBalancerDescriptions[].LoadBalancerName'
check "EBS volumes" aws ec2 describe-volumes \
  --filters "Name=tag:kubernetes.io/cluster/$CLUSTER,Values=owned" --query 'Volumes[].VolumeId'

if ((found)); then
  echo "something is still billing: remove it before you stop" >&2
  exit 1
fi
echo "nothing billable left"

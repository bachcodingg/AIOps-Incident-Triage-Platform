mock_provider "aws" {}

variables {
  name                = "test"
  subnet_ids          = ["subnet-1", "subnet-2"]
  public_access_cidrs = ["203.0.113.10/32"]
}

run "nodes_are_spot_and_multi_pool" {
  command = plan

  assert {
    condition     = aws_eks_node_group.spot.capacity_type == "SPOT"
    error_message = "the cost model assumes spot nodes"
  }

  assert {
    condition     = length(aws_eks_node_group.spot.instance_types) >= 2
    error_message = "one spot pool is too easy to lose"
  }
}

run "control_plane_logs_expire" {
  command = plan

  assert {
    condition     = aws_cloudwatch_log_group.cluster.retention_in_days == 7
    error_message = "logs must expire so nothing billable accumulates"
  }

  assert {
    condition     = aws_cloudwatch_log_group.cluster.name == "/aws/eks/test/cluster"
    error_message = "the group must have the exact name EKS writes to, or EKS creates its own"
  }
}

run "api_is_restricted_and_uses_access_entries" {
  command = plan

  assert {
    condition     = aws_eks_cluster.this.access_config[0].authentication_mode == "API"
    error_message = "use access entries, not the aws-auth ConfigMap"
  }

  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].public_access_cidrs == toset(["203.0.113.10/32"])
    error_message = "public access must be limited to the given CIDRs"
  }
}

run "refuses_an_api_open_to_the_internet" {
  command = plan

  variables {
    public_access_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.public_access_cidrs]
}

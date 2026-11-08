mock_provider "aws" {}

variables {
  name              = "test"
  oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-central-1.amazonaws.com/id/ABC"
  oidc_issuer       = "oidc.eks.eu-central-1.amazonaws.com/id/ABC"
  aws_region        = "eu-central-1"
  aws_account_id    = "123456789012"
}

run "each_role_trusts_exactly_one_service_account" {
  command = plan

  assert {
    condition = (
      jsondecode(aws_iam_role.irsa["loki"].assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-central-1.amazonaws.com/id/ABC:sub"]
      == "system:serviceaccount:monitoring:loki"
    )
    error_message = "the loki role must trust only monitoring/loki"
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.irsa["remediator"].assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-central-1.amazonaws.com/id/ABC:sub"]
      == "system:serviceaccount:aiops:remediator"
    )
    error_message = "the remediator role must trust only aiops/remediator"
  }
}

run "external_secrets_reads_only_its_prefix" {
  command = plan

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.irsa["external-secrets"].policy).Statement[0].Resource
      == "arn:aws:secretsmanager:eu-central-1:123456789012:secret:incidentlens/*"
    )
    error_message = "External Secrets must not be able to read other secrets"
  }
}

run "remediator_cannot_change_anything_on_aws" {
  command = plan

  assert {
    condition = alltrue([
      for action in jsondecode(aws_iam_role_policy.irsa["remediator"].policy).Statement[0].Action :
      startswith(action, "ec2:Describe")
    ])
    error_message = "the remediator's AWS role must stay read-only"
  }
}

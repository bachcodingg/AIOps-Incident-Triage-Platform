mock_provider "aws" {}

variables {
  name              = "test"
  github_repository = "owner/repo"
  state_bucket_arn  = "arn:aws:s3:::state"
  lock_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/locks"
}

run "only_pull_requests_of_this_repo_can_assume_the_role" {
  command = plan

  # The trust policy embeds this ARN, which a plan would otherwise leave unknown.
  override_resource {
    target          = aws_iam_openid_connect_provider.github
    override_during = plan
    values = {
      arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    }
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.plan.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]
      == "repo:owner/repo:pull_request"
    )
    error_message = "the trust policy must be pinned to this repository"
  }
}

run "the_plan_role_cannot_write_state" {
  command = plan

  assert {
    condition     = !strcontains(aws_iam_role_policy.state.policy, "s3:PutObject")
    error_message = "a plan must not be able to change state"
  }
}

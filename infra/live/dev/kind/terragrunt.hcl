# Local environment: no backend and no provider. State stays in Terragrunt's
# cache, and the cluster itself is disposable.
terraform {
  source = "${get_terragrunt_dir()}/../../../modules/kind"
}

inputs = {
  cluster_name = "incidentlens"
  config_path  = abspath("${get_terragrunt_dir()}/../../../kind/cluster.yaml")
  kind_command = get_env("KIND", "kind")
}

# The `dev` environment: no cloud, no credentials, no cost. It wraps the kind CLI
# so Terragrunt and `make cluster` use the same infra/kind/cluster.yaml.

resource "terraform_data" "cluster" {
  input = {
    name = var.cluster_name
    kind = var.kind_command
  }

  triggers_replace = [filesha256(var.config_path)]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      '${self.input.kind}' get clusters | grep -qx '${self.input.name}' ||
        '${self.input.kind}' create cluster --name '${self.input.name}' --config '${var.config_path}' --wait 120s
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["bash", "-c"]
    command     = "'${self.input.kind}' delete cluster --name '${self.input.name}'"
  }
}

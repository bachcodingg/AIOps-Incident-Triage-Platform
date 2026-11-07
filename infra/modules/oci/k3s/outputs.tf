output "public_ip" {
  value = oci_core_instance.k3s.public_ip
}

output "kubeconfig_command" {
  description = "Fetches the kubeconfig once k3s has started."
  value       = "ssh ubuntu@${oci_core_instance.k3s.public_ip} sudo cat /etc/rancher/k3s/k3s.yaml | sed s/127.0.0.1/${oci_core_instance.k3s.public_ip}/"
}

# One Always Free Ampere A1 VM running k3s, the always-on demo target.
# A single node has no redundancy, which is the price of being free.

data "oci_identity_availability_domains" "this" {
  compartment_id = var.compartment_ocid
}

data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = var.ubuntu_version
  shape                    = local.shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

locals {
  shape = "VM.Standard.A1.Flex"

  public_ports = [80, 443]
  admin_ports  = [22, 6443]

  ingress_rules = concat(
    [for port in local.public_ports : { port = port, source = "0.0.0.0/0" }],
    flatten([for port in local.admin_ports : [for cidr in var.admin_cidrs : { port = port, source = cidr }]]),
  )
}

resource "oci_core_vcn" "this" {
  compartment_id = var.compartment_ocid
  display_name   = var.name
  cidr_blocks    = [var.vcn_cidr]
  dns_label      = "incidentlens"
}

resource "oci_core_internet_gateway" "this" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = var.name
}

resource "oci_core_route_table" "this" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = var.name

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.this.id
  }
}

resource "oci_core_security_list" "this" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.this.id
  display_name   = var.name

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  dynamic "ingress_security_rules" {
    for_each = local.ingress_rules

    content {
      protocol = "6" # TCP
      source   = ingress_security_rules.value.source

      tcp_options {
        min = ingress_security_rules.value.port
        max = ingress_security_rules.value.port
      }
    }
  }
}

resource "oci_core_subnet" "this" {
  compartment_id    = var.compartment_ocid
  vcn_id            = oci_core_vcn.this.id
  display_name      = var.name
  cidr_block        = cidrsubnet(var.vcn_cidr, 8, 0)
  route_table_id    = oci_core_route_table.this.id
  security_list_ids = [oci_core_security_list.this.id]
  dns_label         = "nodes"
}

resource "oci_core_instance" "k3s" {
  compartment_id      = var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.this.availability_domains[0].name
  display_name        = var.name
  shape               = local.shape

  launch_options {
    is_pv_encryption_in_transit_enabled = true
  }

  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gb
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu.images[0].id
    boot_volume_size_in_gbs = var.boot_volume_gb
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.this.id
    assign_public_ip = true
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data           = base64encode(templatefile("${path.module}/cloud-init.yaml.tftpl", { k3s_version = var.k3s_version }))
  }

  # A newer base image must not replace the node and its data.
  lifecycle {
    ignore_changes = [source_details[0].source_id]
  }
}

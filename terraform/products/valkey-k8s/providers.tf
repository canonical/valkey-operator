provider "juju" {
  controller_addresses = var.juju_controller != null ? var.juju_controller.endpoint : null
  username             = var.juju_controller != null ? var.juju_controller.username : null
  password             = var.juju_controller != null ? var.juju_controller.password : null
  ca_certificate       = var.juju_controller != null ? var.juju_controller.ca : null
}

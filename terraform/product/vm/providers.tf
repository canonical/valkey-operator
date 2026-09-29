provider "juju" {
  controller_addresses = try(var.juju_controller.endpoint, null)
  username             = try(var.juju_controller.username, null)
  password             = try(var.juju_controller.password, null)
  ca_certificate       = try(var.juju_controller.ca, null)
}

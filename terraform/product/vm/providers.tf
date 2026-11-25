provider "juju" {
  controller_addresses = try(var.juju_controller.endpoint, null)
  username             = try(var.juju_controller.username, null)
  password             = try(var.juju_controller.password, null)
  ca_certificate       = try(var.juju_controller.ca, null)

  # Lets juju_integration consume offers from targets that set controller.
  offering_controllers = {
    for k, v in var.juju_offering_controllers : k => {
      controller_addresses = v.endpoint
      username             = v.username
      password             = v.password
      ca_certificate       = v.ca
    }
  }
}

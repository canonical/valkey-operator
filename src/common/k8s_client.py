# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""K8sClient utility class to connect to the Kubernetes API server."""

import logging

from lightkube.core.client import Client
from lightkube.core.exceptions import ApiError
from lightkube.models.core_v1 import ServicePort, ServiceSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Pod, Service
from lightkube.types import PatchType

from common.exceptions import KubernetesClientError

logger = logging.getLogger(__name__)


class K8sClient:
    """Expose Kubernetes API commands to the charm."""

    def __init__(self, namespace: str, app_name: str):
        self.namespace = namespace
        self.app_name = app_name
        self.client = Client()

    def ensure_endpoint_service(self, role: str, port: int) -> None:
        """Create or update a K8s service.

        Args:
            role(str): name of the role to create the service for, e.g "primary" or "replicas"
            port(int): the port number to set for the service
        """
        service_name = f"{self.app_name}-{role}"
        service_port = ServicePort(port=port, targetPort=port)

        try:
            service = self.client.get(res=Service, name=service_name, namespace=self.namespace)
            if service.spec and service.spec.ports != [service_port]:
                service.spec.ports = [service_port]
                self.client.patch(Service, service_name, service, patch_type=PatchType.MERGE)
                logger.info("Updated Kubernetes service %s to port %s", service_name, port)
            return
        except ApiError as e:
            # 404 will be raised if service does not exist yet
            if e.status.code != 404:
                raise KubernetesClientError from e

        try:
            pod0 = self.client.get(
                res=Pod,
                name=self.app_name + "-0",
                namespace=self.namespace,
            )
        except ApiError as e:
            raise KubernetesClientError from e

        if pod0.metadata is None:
            raise KubernetesClientError(f"Pod {self.app_name}-0 has no metadata")

        service = Service(
            apiVersion="v1",
            kind="Service",
            metadata=ObjectMeta(
                namespace=self.namespace,
                name=service_name,
                ownerReferences=pod0.metadata.ownerReferences,
            ),
            spec=ServiceSpec(
                selector={"application-name": self.app_name, "role": role},
                ports=[service_port],
                type="ClusterIP",
            ),
        )

        try:
            self.client.create(service)
            logger.info("Created Kubernetes service %s for port %s", service_name, port)
        except ApiError as e:
            logger.error("Kubernetes service creation failed: %s", e)
            raise KubernetesClientError from e

    def update_pod_label(self, pod_name: str, role: str) -> None:
        """Create or update a label for a pod.

        Args:
            pod_name(str): the name of the pod
            role(str): name of the role to create the label for, e.g "primary" or "replicas"
        """
        try:
            pod = self.client.get(Pod, pod_name, namespace=self.namespace)
        except ApiError as e:
            raise KubernetesClientError from e

        if pod.metadata is None:
            raise KubernetesClientError(f"Pod {pod_name} has no metadata")

        if not pod.metadata.labels:
            pod.metadata.labels = {}

        if pod.metadata.labels.get("role") == role:
            return

        logger.info("Updating pod %s to role %s", pod_name, role)
        pod.metadata.labels["application-name"] = self.app_name
        pod.metadata.labels["role"] = role
        try:
            self.client.patch(Pod, pod_name, pod)
        except ApiError as e:
            logger.error("Failed to update Kubernetes pod labels: %s", e)
            raise KubernetesClientError from e

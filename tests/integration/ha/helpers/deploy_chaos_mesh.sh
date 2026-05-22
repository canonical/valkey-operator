#!/bin/bash

# Utility script to install chaosmesh in the K8S cluster, so test can use it to simulate
# infrastructure failures
# source: https://github.com/canonical/mongo-single-kernel-library/blob/8/edge/tests/integration/helpers/scripts/deploy_chaos_mesh.sh

chaos_mesh_ns=$1
chaos_mesh_version="2.4.1"

if [ -z "${chaos_mesh_ns}" ]; then
    exit 1
fi

deploy_chaos_mesh() {
    if [ "$(microk8s.helm repo list | grep -c 'chaos-mesh')" != "1" ]; then
        echo "adding chaos-mesh microk8s.helm repo"
        microk8s.helm repo add chaos-mesh https://charts.chaos-mesh.org
    fi

    echo "installing chaos-mesh"
    # --wait blocks until the controller-manager Deployment and the chaos-daemon
    # DaemonSet report Ready, so the admission webhook (mnetworkchaos.kb.io) is
    # serving and the daemon can program rules before any test applies a
    # NetworkChaos. Without it the test races chaos-mesh startup: the webhook
    # refuses connections and the loss rule is enforced late (packets leak).
    microk8s.helm install chaos-mesh chaos-mesh/chaos-mesh --namespace="${chaos_mesh_ns}" --set chaosDaemon.runtime=containerd --set chaosDaemon.socketPath=/var/snap/microk8s/common/run/containerd.sock --set dashboard.create=false --version "${chaos_mesh_version}" --set clusterScoped=false --set controllerManager.targetNamespace="${chaos_mesh_ns}" --wait --timeout 5m0s
    # Brief extra settle for the webhook's serving cert/endpoints to propagate
    # after the pod is Ready; the apply itself is also retried (see helpers.py).
    sleep 10
}

echo "namespace=${chaos_mesh_ns}"
chmod 0700 ~/.kube/config
deploy_chaos_mesh

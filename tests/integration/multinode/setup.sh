#!/usr/bin/env bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Build a 3-node MicroK8s cluster from LXD VMs for the node-loss tests
# (testing.md, Multi-node environment), and bootstrap a Juju controller on it.
#
# Run on the LXD host from the repository root. The first VM is the control
# plane and runs the Juju controller and the tests. The other two join as
# workers, so the test can cut one off without losing the controller.
#
# Environment: MULTINODE_PREFIX (default litellm-mn), MICROK8S_CHANNEL
# (default 1.36-strict/stable), JUJU_CHANNEL (default 3.6/stable), VM_MEMORY
# (default 6GiB), VM_CPUS (default 4).
set -euo pipefail

PREFIX=${MULTINODE_PREFIX:-litellm-mn}
# The channel concierge installs: older channels' CAs fail the Valkey charm's
# strict certificate checks.
MICROK8S_CHANNEL=${MICROK8S_CHANNEL:-1.36-strict/stable}
JUJU_CHANNEL=${JUJU_CHANNEL:-3.6/stable}
VM_MEMORY=${VM_MEMORY:-6GiB}
VM_CPUS=${VM_CPUS:-4}
NODES=("$PREFIX-0" "$PREFIX-1" "$PREFIX-2")
CONTROLLER="$PREFIX"

run() { lxc exec "$1" -- bash -lc "export PATH=\$PATH:/snap/bin; $2"; }

for node in "${NODES[@]}"; do
    if ! lxc info "$node" >/dev/null 2>&1; then
        lxc launch ubuntu:24.04 "$node" --vm \
            -c limits.cpu="$VM_CPUS" -c limits.memory="$VM_MEMORY" -d root,size=40GiB
    fi
done
confinement=$([[ $MICROK8S_CHANNEL == *-strict/* ]] || echo --classic)
for node in "${NODES[@]}"; do
    until lxc exec "$node" -- cloud-init status --wait >/dev/null 2>&1; do sleep 5; done
    run "$node" "snap list microk8s >/dev/null 2>&1 || snap install microk8s $confinement --channel=$MICROK8S_CHANNEL"
    run "$node" "microk8s status --wait-ready >/dev/null"
done

primary=${NODES[0]}
for node in "${NODES[@]:1}"; do
    if ! run "$primary" "microk8s kubectl get node $node" >/dev/null 2>&1; then
        join=$(run "$primary" "microk8s add-node --format short" | head -n 1)
        run "$node" "$join --worker"
    fi
done
run "$primary" "until [ \$(microk8s kubectl get nodes --no-headers | grep -c ' Ready') -eq 3 ]; do sleep 5; done"
run "$primary" "microk8s enable dns hostpath-storage rbac"

# The tests on the first VM reach the others over SSH to cut their network.
run "$primary" "test -f /root/.ssh/id_ed25519 || ssh-keygen -q -t ed25519 -N '' -f /root/.ssh/id_ed25519"
key=$(run "$primary" "cat /root/.ssh/id_ed25519.pub")
for node in "${NODES[@]:1}"; do
    run "$node" "grep -qF '$key' /root/.ssh/authorized_keys || echo '$key' >> /root/.ssh/authorized_keys"
done

run "$primary" "snap list juju >/dev/null 2>&1 || snap install juju --channel=$JUJU_CHANNEL"
run "$primary" "snap list astral-uv >/dev/null 2>&1 || snap install astral-uv --classic"
# The Juju snap is strictly confined: it needs this directory and cannot read ~/.kube.
run "$primary" "mkdir -p /root/.local/share/juju"
run "$primary" "juju clouds --client 2>/dev/null | grep -q '^multinode ' || microk8s config | juju add-k8s multinode --client"
run "$primary" "juju controllers 2>/dev/null | grep -q '^$CONTROLLER' || juju bootstrap multinode $CONTROLLER"

echo "nodes: ${NODES[*]}"
echo "controller: $CONTROLLER (on $primary)"

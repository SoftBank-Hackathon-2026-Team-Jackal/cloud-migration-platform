"""Ticket 26: NetworkPolicy parts of the fake EKS/GKE clusters (kubectl stand-ins)."""

AWS_NODE = {
    "spec": {
        "template": {
            "spec": {
                "containers": [
                    {"name": "aws-node"},
                    {"name": "aws-eks-nodeagent", "args": ["--enable-network-policy=true"]},
                ]
            }
        }
    }
}


class NetworkPolicyFake:
    """Mixin: `applied` NetworkPolicies are managed; `existing_policies` (name → managed) are
    ones already in the Namespace; `enforcing=False` hides the enforcement DaemonSet."""

    enforcing = True

    def get_object(self, kind, name, namespace=None):
        if not self.enforcing or kind != "daemonset" or namespace != "kube-system":
            return None
        return {"aws-node": AWS_NODE, "anetd": {"metadata": {"name": "anetd"}}}.get(name)

    def network_policies(self, namespace):
        found = dict(getattr(self, "existing_policies", {}))
        for manifest in self.applied:
            if manifest["kind"] == "NetworkPolicy":
                found[manifest["metadata"]["name"]] = True
        return found

    def delete_network_policy(self, namespace, name):
        self._log("delete-networkpolicy", namespace, name)
        self.existing_policies.pop(name)

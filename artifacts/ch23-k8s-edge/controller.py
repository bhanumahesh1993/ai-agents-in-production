"""A small controller: an ``Agent`` object in, four objects and a status out.

Kubernetes is a machine for reconciling desired state, and an agent has a
desired state, so describe it declaratively and let a controller do the
work. This one is deliberately about two hundred lines. It reconciles an
``Agent`` into a worker Deployment, a NetworkPolicy derived from
``spec.egress``, a ConfigMap holding the pinned model snapshot and budget,
and a status subresource reporting the running version.

That is enough to demonstrate the pattern honestly, *including its failure
mode*, which :meth:`AgentController.admit` and :meth:`AgentController.reconcile`
reproduce: edit the resource while a run is in flight and the run finishes
on its admission version while the next one starts on the new one.

There is no cluster here and no ``kubectl``. The API server is a dict,
which is enough to show what the controller writes and to assert that it
never widens egress, never unpins a snapshot, and never moves a run to a
version it did not start on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from manifests import AgentSpec, ManifestError, admission_problems
from northstar_contracts import content_hash

__all__ = [
    "ADMISSION_LABEL",
    "AgentController",
    "AgentStatus",
    "AdmittedRun",
]

#: The label the controller stamps on the worker pods, and the selector the
#: NetworkPolicy matches on. One string, in one place, because a policy
#: whose selector drifts from its workload is a policy that matches nothing
#: and reports success.
ADMISSION_LABEL = "agents.northstar.dev/name"


@dataclass(frozen=True)
class AgentStatus:
    """The status subresource. What ``kubectl get agents`` shows.

    ``config_hash`` is Chapter 26's effective configuration hash: the model
    snapshot, the budget, the tool set, the policy reference, and the
    egress mode, hashed together. Two pods reporting different hashes for
    the same version is a fleet with a split brain, and this is the field
    that makes it visible.
    """

    name: str
    running_version: str
    config_hash: str
    ready_replicas: int
    observed_generation: int
    egress: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "name": self.name,
            "runningVersion": self.running_version,
            "configHash": self.config_hash,
            "readyReplicas": self.ready_replicas,
            "observedGeneration": self.observed_generation,
            "egress": self.egress,
            "message": self.message,
        }


@dataclass
class AdmittedRun:
    """One run, pinned to the version it was admitted on.

    Chapter 26's run-version pinning, expressed here because it is what
    makes a rolling deploy a drain rather than an interruption. A run does
    not move to a new version mid-flight; it finishes on the one it
    started on, and the next run starts on the new one.
    """

    run_id: str
    version: str
    config_hash: str
    finished: bool = False


@dataclass
class AgentController:
    """Reconciles ``Agent`` objects into the four things a worker needs.

    Args:
        objects: The API server, as a dict of kind to name to object.
            Real enough to assert on and small enough to read.
    """

    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    generation: int = 0
    runs: dict[str, AdmittedRun] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------ admission

    def admit(self, document: dict[str, Any]) -> AgentSpec:
        """Validate an ``Agent`` before it reaches the cluster.

        Fails closed. A resource with no policy reference, a floating model
        snapshot, or anything other than deny-by-default egress is refused
        here rather than discovered by an incident.

        Raises:
            ManifestError: On anything admission rejects.
        """
        problems = admission_problems(document)
        if problems:
            self._emit("Rejected", document, {"problems": problems})
            raise ManifestError("; ".join(problems))
        return AgentSpec.from_manifest(document)

    # ---------------------------------------------------------- reconcile

    def reconcile(self, document: dict[str, Any]) -> AgentStatus:
        """Bring the cluster to the state the ``Agent`` describes.

        Creates or updates the Deployment, the NetworkPolicy implied by
        ``spec.egress``, and the ConfigMap holding the pinned snapshot and
        budget, then writes the status. Idempotent: reconciling the same
        object twice produces the same objects and the same hash, which is
        what makes a controller safe to run in a loop.
        """
        spec = self.admit(document)
        self.generation += 1
        config_hash = content_hash(spec.config_hash_inputs())[:16]

        self._put("Deployment", self._deployment(spec, config_hash))
        self._put("NetworkPolicy", self._network_policy(spec))
        self._put("ConfigMap", self._config_map(spec, config_hash))

        status = AgentStatus(
            name=spec.name,
            running_version=spec.version,
            config_hash=config_hash,
            ready_replicas=spec.replicas,
            observed_generation=self.generation,
            egress=spec.egress,
            message=f"reconciled {len(spec.tool_names)} tool reference(s)",
        )
        self._put("Agent", {"metadata": {"name": spec.name},
                            "status": status.to_dict()})
        self._emit("Reconciled", document, status.to_dict())
        return status

    # ------------------------------------------------------- the four objects

    def _deployment(self, spec: AgentSpec, config_hash: str) -> dict[str, Any]:
        """The worker Deployment.

        Three details are the chapter's advice made concrete. Autoscaling
        is on queue age and active sessions rather than CPU, because a
        worker holding forty sessions through a human wait is idle by CPU
        and busy by every measure that matters. There is a disruption
        budget and a draining preStop hook, so a rolling deploy does not
        terminate a pod mid-mutation. And the pod is scheduled onto the
        agent pool, never the GPU pool.
        """
        return {
            "kind": "Deployment",
            "metadata": {
                "name": f"{spec.name}-worker",
                "labels": {ADMISSION_LABEL: spec.name},
                "annotations": {"agents.northstar.dev/config-hash": config_hash},
            },
            "spec": {
                "replicas": spec.replicas,
                "selector": {ADMISSION_LABEL: spec.name},
                "nodeSelector": {"northstar.dev/pool": "agents"},
                "terminationGracePeriodSeconds": 900,
                "podDisruptionBudget": {"minAvailable": 1},
                "lifecycle": {
                    "preStop": (
                        "stop accepting runs; wait for in-flight steps to "
                        "reach a checkpoint; exit"
                    )
                },
                "autoscaling": {
                    "metrics": ["queue_age_seconds", "active_sessions"],
                    "note": "never CPU: a human wait uses none of it",
                },
                "env": {
                    "NORTHSTAR_AGENT_VERSION": spec.version,
                    "NORTHSTAR_CONFIG_HASH": config_hash,
                    "NORTHSTAR_POLICY_REF": spec.policy_ref,
                },
            },
        }

    def _network_policy(self, spec: AgentSpec) -> dict[str, Any]:
        """The per-agent egress policy, derived from ``spec.egress``.

        The allowlist is exactly the MCP servers the resource references,
        plus the two infrastructure endpoints a worker cannot run without.
        Nothing else. An agent will fetch a URL a customer supplied, and a
        namespace default-allow turns that into the exfiltration path.
        """
        allowed = [*spec.mcp_servers(), "postgres", "otel-collector"]
        return {
            "kind": "NetworkPolicy",
            "metadata": {"name": f"{spec.name}-egress"},
            "spec": {
                "podSelector": {"matchLabels": {ADMISSION_LABEL: spec.name}},
                "policyTypes": ["Egress"],
                "defaultDeny": spec.egress == "deny-by-default",
                "egress": [
                    {"to": [{"podSelector": {"matchLabels": {"app": app}}}]}
                    for app in allowed
                ],
            },
        }

    def _config_map(self, spec: AgentSpec, config_hash: str) -> dict[str, Any]:
        """The pinned model snapshot, the budget, and the policy reference."""
        return {
            "kind": "ConfigMap",
            "metadata": {"name": f"{spec.name}-config"},
            "data": {
                "version": spec.version,
                "model.snapshot": spec.snapshot,
                "model.quantization": str(spec.model.get("quantization", "")),
                "budget.cents": str(spec.budget.get("cents")),
                "budget.turns": str(spec.budget.get("turns")),
                "policyRef": spec.policy_ref,
                "configHash": config_hash,
            },
        }

    # -------------------------------------------------------- run pinning

    def start_run(self, agent: str, run_id: str) -> AdmittedRun:
        """Admit a run against the version currently reconciled.

        Raises:
            KeyError: If the agent has never been reconciled. A run
                admitted against an agent the cluster does not know is a
                run with no version, no budget, and no policy.
        """
        status = self.status(agent)
        run = AdmittedRun(run_id, status.running_version, status.config_hash)
        self.runs[run_id] = run
        self._emit("RunAdmitted", {"metadata": {"name": agent}},
                   {"run_id": run_id, "version": run.version})
        return run

    def version_for(self, run_id: str) -> str:
        """The version a run executes on, whatever the resource now says.

        This is the failure the artifact reproduces rather than hides: edit
        the resource mid-run and the in-flight run finishes on its
        admission version while the next one starts on the new one. That is
        correct, and it is also why the deploy retention floor equals the
        maximum run duration.
        """
        return self.runs[run_id].version

    def finish_run(self, run_id: str) -> AdmittedRun:
        """Mark a run finished, so a drain knows when it may proceed."""
        run = self.runs[run_id]
        run.finished = True
        return run

    def draining(self) -> list[str]:
        """Runs still in flight. A drain waits for this list to empty."""
        return sorted(r.run_id for r in self.runs.values() if not r.finished)

    # ------------------------------------------------------------- queries

    def status(self, agent: str) -> AgentStatus:
        """The status subresource for one agent.

        Raises:
            KeyError: If the agent has not been reconciled.
        """
        stored = self.objects.get("Agent", {}).get(agent)
        if stored is None:
            raise KeyError(f"no reconciled agent {agent!r}")
        data = stored["status"]
        return AgentStatus(
            name=data["name"],
            running_version=data["runningVersion"],
            config_hash=data["configHash"],
            ready_replicas=data["readyReplicas"],
            observed_generation=data["observedGeneration"],
            egress=data["egress"],
            message=data.get("message", ""),
        )

    def get(self, kind: str, name: str) -> dict[str, Any] | None:
        """One reconciled object, or ``None``.

        A registry, an inventory, and centralized revocation are queries
        against the API server when agents are API objects, which is the
        fourth argument for this approach and the cheapest one to verify.
        """
        return self.objects.get(kind, {}).get(name)

    def inventory(self) -> list[dict[str, Any]]:
        """Every agent the cluster knows, with the fields an incident needs."""
        return [
            {
                "name": name,
                "version": obj["status"]["runningVersion"],
                "configHash": obj["status"]["configHash"],
                "egress": obj["status"]["egress"],
            }
            for name, obj in sorted(self.objects.get("Agent", {}).items())
        ]

    # ------------------------------------------------------------ internals

    def _put(self, kind: str, obj: dict[str, Any]) -> None:
        """Create or replace one object."""
        self.objects.setdefault(kind, {})[obj["metadata"]["name"]] = obj

    def _emit(
        self,
        reason: str,
        document: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Record a controller event, which is what `kubectl describe` shows."""
        self.events.append(
            {
                "reason": reason,
                "object": (document.get("metadata") or {}).get("name"),
                **payload,
            }
        )

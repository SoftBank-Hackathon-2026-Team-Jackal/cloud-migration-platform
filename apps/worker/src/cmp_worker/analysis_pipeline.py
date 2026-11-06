"""The real analysis steps behind the AnalysisWorkflow Activities (ticket 19).

Each step takes and returns small JSON values. Big results (scans, requirements bytes, mapping
rows, the report) go to the artifact store and only their references travel, so Workflow
history holds names, counts and references, never source, `.env` values or scan dumps.
The source workspace is local to this worker process, so these steps are meant to run in one
worker (the same commit is reused; another worker simply fetches it again).
"""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cmp_agents.catalog import load_catalog
from cmp_agents.llm import LLMClient
from cmp_agents.mapper import MapperInput
from cmp_agents.mapper import map_usage as run_mapper
from cmp_agents.mapping import check_evidence as check_mapping_evidence
from cmp_contracts.analysis import (
    AnalysisInput,
    AnalysisReport,
    ExternalEntry,
    MappingRow,
    PrecheckReport,
    UsageProfile,
)
from cmp_contracts.provisioning import VersionRef
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_providers.env_file import EnvFileError, EnvFileStore, resolve_env_names
from cmp_providers.env_plan_builder import build_env_plan
from cmp_scanners.assembly import (
    build_requirements as assemble_requirements,
)
from cmp_scanners.assembly import (
    check_dockerfile,
    serialize_requirements,
)
from cmp_scanners.assembly import (
    precheck as run_precheck,
)
from cmp_scanners.aws_infra import AwsClients, AwsInfraScan, AwsScanError
from cmp_scanners.aws_infra import scan_aws_infra as run_aws_scan
from cmp_scanners.code import CodeScan
from cmp_scanners.code import scan_code as run_code_scan
from cmp_scanners.go_adapter import GoAdapter
from cmp_scanners.manifests import ManifestScan
from cmp_scanners.manifests import scan_manifests as run_manifest_scan
from cmp_scanners.masking import mask_text
from cmp_scanners.network import build_network, merge_network_intents
from cmp_scanners.routes import scan_go_routes
from cmp_scanners.source import fetch_source as checkout_source

from cmp_worker.artifact_store import ArtifactRef, ArtifactStore
from cmp_worker.source_access import (
    NoSourceTokenResolver,
    SourceTokenResolver,
    github_repository,
)


class AnalysisPipeline:
    def __init__(
        self,
        *,
        store: ArtifactStore,
        workspace_root: Path | str,
        skills_root: Path | str,
        catalog_dir: Path | str,
        llm_factory: Callable[[str, str], LLMClient] | None = None,
        aws_clients_factory: Callable[[AnalysisInput], AwsClients] | None = None,
        allow_local_repositories: bool = False,
        source_resolver: SourceTokenResolver | None = None,
        env_store: EnvFileStore | None = None,
    ) -> None:
        self.env_store = env_store
        self.source_resolver = source_resolver or NoSourceTokenResolver()
        self.store = store
        self.workspace_root = Path(workspace_root)
        self.skills_root = Path(skills_root)
        self.catalog_dir = Path(catalog_dir)
        self.llm_factory = llm_factory
        self.aws_clients_factory = aws_clients_factory
        self.allow_local = allow_local_repositories

    # ------------------------------------------------------------------ helpers

    def _put(self, value: Any, migration_id: str, kind: str) -> dict[str, Any]:
        ref = self.store.put_json(value, ref_id=f"{kind}-{migration_id}", kind=kind)
        return ref.model_dump()

    def _get(self, ref: dict[str, Any]) -> Any:
        return self.store.get_json(ArtifactRef.model_validate(ref))

    @staticmethod
    def _input(inp: dict[str, Any]) -> AnalysisInput:
        return AnalysisInput.model_validate(inp)

    # -------------------------------------------------------------------- steps

    def fetch_source(self, inp: dict[str, Any]) -> dict[str, Any]:
        data = self._input(inp)
        if not data.repository or not data.requested_commit:
            raise ValueError("SOURCE_INPUT_MISSING")
        token = None
        if data.source_access_ref is not None:
            # The token goes only to a plain github.com repository, and only now, to clone.
            github_repository(data.repository)
            credential = self.source_resolver.resolve(
                data.source_access_ref, host="github.com", purpose="READ"
            )
            token = credential.token
        snapshot = checkout_source(
            data.repository,
            requested_commit=data.requested_commit,
            migration_id=data.migration_id,
            workspace_root=self.workspace_root,
            token=token,
            allow_local=self.allow_local,
        )
        env_names = snapshot.env_names
        if data.env_file_ref is not None:
            # ENV-05: the uploaded `.env` replaces the repository's `.env*` files. Names only.
            if self.env_store is None:
                raise EnvFileError("ENV_FILE_STORE_UNCONFIGURED")
            env_names = {"env_file_ref": resolve_env_names(self.env_store, data.env_file_ref)}
        return {
            "workspace": snapshot.workspace,
            "commit": snapshot.commit,
            "commit_verified": snapshot.commit_verified,
            "env_names": env_names,
        }

    def scan_code(self, inp: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        scan = run_code_scan(Path(source["workspace"]), source["env_names"], [GoAdapter()])
        return self._put(scan, self._input(inp).migration_id, "code-scan")

    def scan_manifests(self, inp: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        data = self._input(inp)
        workspace = Path(source["workspace"]).resolve()
        target = (workspace / data.manifests_dir).resolve() if data.manifests_dir else workspace
        if target != workspace and workspace not in target.parents:
            raise ValueError("MANIFESTS_OUTSIDE_WORKSPACE")
        return self._put(run_manifest_scan(target), data.migration_id, "manifest-scan")

    def scan_aws_infra(self, inp: dict[str, Any]) -> dict[str, Any]:
        data = self._input(inp)
        if not data.cluster_name or self.aws_clients_factory is None:
            return {"status": "NOT_COLLECTED", "reason": "AWS_SCAN_NOT_CONFIGURED"}
        try:
            clients = self.aws_clients_factory(data)
            scan = run_aws_scan(clients, cluster_name=data.cluster_name, role_arns=data.role_arns)
        except AwsScanError as error:
            return {"status": "FAILED", "code": error.code, "permission": error.permission}
        return {"status": "COLLECTED", "ref": self._put(scan, data.migration_id, "aws-scan")}

    def build_requirements(
        self,
        inp: dict[str, Any],
        source: dict[str, Any],
        code_ref: dict[str, Any],
        manifests_ref: dict[str, Any],
        infra: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._input(inp)
        code = CodeScan.model_validate(self._get(code_ref))
        manifests = ManifestScan.model_validate(self._get(manifests_ref))
        built = assemble_requirements(manifests, code, source["env_names"])
        infra_scan = (
            AwsInfraScan.model_validate(self._get(infra["ref"]))
            if infra.get("status") == "COLLECTED"
            else None
        )
        requirements = built.requirements
        network = None
        database_images: dict[str, str] = {}
        if requirements is not None:
            apps = [r.id for r in requirements.resources if r.kind == "APPLICATION"]
            databases = [r.id for r in requirements.resources if r.kind == "POSTGRESQL"]
            network = build_network(infra_scan, code, application_ids=apps, database_ids=databases)
            merged = merge_network_intents(requirements.network_intent, network.intents)
            requirements = RequirementsV2.model_validate(
                {
                    **requirements.model_dump(mode="json"),
                    "network_intent": [i.model_dump(mode="json") for i in merged],
                }
            )
            database_images = {
                db_id: image.image
                for db_id, image in zip(databases, manifests.postgres_images, strict=False)
            }
        requirements_ref = None
        if requirements is not None:
            payload, _ = serialize_requirements(requirements)
            requirements_ref = self.store.put(
                payload, ref_id=f"requirements-{data.migration_id}", kind="requirements"
            ).model_dump()
        env_plan_ref = None
        if data.env_file_ref is not None and requirements is not None:
            env_plan_ref = self._env_plan(data, source, code, manifests, requirements)
        assembly = {
            "env_exceptions": built.env_exceptions.model_dump(mode="json"),
            "blocks": [b.model_dump(mode="json") for b in built.blocks],
            "unmapped_items": built.unmapped_items,
            "assumptions": built.assumptions,
            "dockerfile": check_dockerfile(source["workspace"]).model_dump(mode="json"),
            "network": network.model_dump(mode="json") if network else None,
            "database_images": database_images,
            "http_routes": [r.model_dump(mode="json") for r in scan_go_routes(source["workspace"])],
            **({"env_plan_ref": env_plan_ref} if env_plan_ref is not None else {}),
        }
        return {
            "requirements_ref": requirements_ref,
            "assembly_ref": self._put(assembly, data.migration_id, "assembly"),
            "blocks": assembly["blocks"],
            "unmapped_items": built.unmapped_items,
            "code_ref": code_ref,
            "manifests_ref": manifests_ref,
        }

    def _env_plan(
        self,
        data: AnalysisInput,
        source: dict[str, Any],
        code: CodeScan,
        manifests: ManifestScan,
        requirements: RequirementsV2,
    ) -> dict[str, Any]:
        """ENV-05: one disposition per key, from names only (the same rules as the exceptions)."""
        postgres = {p.resource for p in manifests.postgres_images}
        declared = {n for names in source["env_names"].values() for n in names} | {
            n
            for w in manifests.workloads
            if w.kind in {"Deployment", "StatefulSet"} and f"{w.kind}/{w.name}" not in postgres
            for n in w.env_names
        }
        reads = {s.target for s in code.call_sites if s.kind == "ENV_READ"}
        plan = build_env_plan(
            data.env_file_ref,
            declared,
            reads,
            has_database=any(r.kind == "POSTGRESQL" for r in requirements.resources),
        )
        return self._put(plan, data.migration_id, "env-plan")

    def precheck(
        self,
        inp: dict[str, Any],
        source: dict[str, Any],
        code_ref: dict[str, Any],
        manifests_ref: dict[str, Any],
    ) -> dict[str, Any]:
        code = CodeScan.model_validate(self._get(code_ref))
        manifests = ManifestScan.model_validate(self._get(manifests_ref))
        report = run_precheck(source["workspace"], manifests, code)
        migration_id = self._input(inp).migration_id
        return {
            "ref": self._put(report, migration_id, "precheck"),
            "findings": len(report.findings),
        }

    def _evidence_summary(
        self, code: CodeScan, manifests: ManifestScan, assembly: dict, findings: list[str]
    ) -> str:
        lines = ["## Code"]
        lines += [f"{s.kind} {s.target} {s.file}:{s.line}" for s in code.call_sites]
        lines += [f"EXTENSION {e.name} {e.file}:{e.line}" for e in code.extension_creations]
        lines += ["## Manifests"]
        lines += [f"WORKLOAD {w.kind}/{w.name} {w.evidence}" for w in manifests.workloads]
        lines += [f"{d.kind} {d.resource} {d.evidence}" for d in manifests.cloud_dependencies]
        lines += [
            f"POSTGRES_IMAGE {p.resource} {p.image} {p.evidence}" for p in manifests.postgres_images
        ]
        lines += [
            f"UNCONVERTIBLE {u.kind} {u.resource} {u.detail}" for u in manifests.unconvertible
        ]
        lines += ["## Precheck", *findings, "## Environment"]
        exceptions = assembly["env_exceptions"]
        lines += [f"EXCLUDED {n}" for n in exceptions["excluded"]]
        lines += [f"REPLACED {n} -> {t}" for n, t in exceptions["replaced"].items()]
        lines += [f"MISSING {n}" for n in exceptions["missing"]]
        network = assembly.get("network") or {}
        lines += ["## Network"]
        lines += [
            f"ENTRY {e['protocol']} {e['port']} -> {e['target_resource_id']}:{e['target_port']}"
            for e in network.get("external_entries", [])
        ]
        return "\n".join(lines)

    def map_usage(
        self,
        inp: dict[str, Any],
        source: dict[str, Any],
        code_ref: dict[str, Any],
        manifests_ref: dict[str, Any],
        built: dict[str, Any],
        precheck_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.llm_factory is None:
            return {"status": "NOT_RUN", "rows_ref": None, "reason": "LLM_NOT_CONFIGURED"}
        code = CodeScan.model_validate(self._get(code_ref))
        manifests = ManifestScan.model_validate(self._get(manifests_ref))
        findings = self._get(precheck_result["ref"])["findings"] if precheck_result else []
        summary = self._evidence_summary(
            code, manifests, self._get(built["assembly_ref"]), findings
        )
        catalog = load_catalog(self.catalog_dir, skills_root=self.skills_root)
        result = run_mapper(
            MapperInput(evidence_summary=mask_text(summary)),
            llm=self.llm_factory(self._input(inp).migration_id, "mapper"),
            catalog=catalog,
            skills_root=self.skills_root,
        )
        if result.status != "MAPPED":
            return {"status": result.status, "rows_ref": None, "reason": result.failure_reason}
        rows = [r.model_dump(mode="json") for r in result.rows]
        migration_id = self._input(inp).migration_id
        return {
            "status": "MAPPED",
            "rows_ref": self._put(rows, migration_id, "mapping-rows"),
            "attempts": result.attempts,
        }

    def check_evidence(
        self,
        inp: dict[str, Any],
        source: dict[str, Any],
        built: dict[str, Any],
        infra: dict[str, Any],
        precheck_result: dict[str, Any],
        mapped: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._input(inp)
        manifests = ManifestScan.model_validate(self._get(built["manifests_ref"]))
        assembly = self._get(built["assembly_ref"])
        catalog = load_catalog(self.catalog_dir, skills_root=self.skills_root)
        rows = (
            [MappingRow.model_validate(r) for r in self._get(mapped["rows_ref"])]
            if mapped.get("rows_ref")
            else []
        )
        prefix = f"{data.manifests_dir}/" if data.manifests_dir else ""
        evidence = [
            *(w.evidence for w in manifests.workloads),
            *(c.evidence for w in manifests.workloads for c in w.containers),
            *(d.evidence for d in manifests.cloud_dependencies),
            *(p.evidence for p in manifests.postgres_images),
            *(u.evidence for u in manifests.unconvertible),
            *(r.evidence for r in manifests.network_rules),
            *(v.evidence for v in manifests.volume_claims),
            *(c.evidence for c in manifests.configmaps),
        ]
        locators = {*evidence, *(prefix + e for e in evidence)}
        checked = check_mapping_evidence(
            rows,
            workspace=source["workspace"],
            manifest_locators=locators,
            catalog_capabilities=catalog.capabilities,
        )
        network = assembly.get("network") or {}
        risks = list(assembly["assumptions"])
        if infra.get("status") == "NOT_COLLECTED":
            risks.append("INFRA_NOT_COLLECTED: the AWS infrastructure scan did not run")
        elif infra.get("status") == "FAILED":
            risks.append(f"INFRA_SCAN_FAILED: {infra.get('code')}")
        if mapped.get("status") != "MAPPED":
            risks.append(f"MAPPING_NOT_RUN: {mapped.get('reason') or mapped.get('status')}")
        code = CodeScan.model_validate(self._get(built["code_ref"]))
        if code.env_comparison.partial:
            risks.append("ENV_COMPARISON_PARTIAL: some source files were not scanned")
        unmapped = [
            *assembly["unmapped_items"],
            *network.get("notes", []),
            *(f"UNSCANNABLE: {u.file} ({u.reason})" for u in code.unscannable),
        ]
        requirements_sha = (built.get("requirements_ref") or {}).get("sha256", "")
        version = hashlib.sha256(
            json.dumps(
                [source["commit"], requirements_sha, [r.model_dump(mode="json") for r in checked]],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:12]
        report = AnalysisReport(
            migration_id=data.migration_id,
            ref=VersionRef(id="analysis", version=version),
            base_commit=source["commit"],
            commit_verified=source["commit_verified"],
            usage_profile=UsageProfile(
                usages=sorted(f"{r.capability}: {r.usage}" for r in checked)
            ),
            mapping=checked,
            precheck=PrecheckReport.model_validate(
                {"findings": self._get(precheck_result["ref"])["findings"]}
            ),
            external_entries=[
                ExternalEntry.model_validate(e) for e in network.get("external_entries", [])
            ],
            source_cidrs=network.get("source_cidrs", []),
            source_db_images=assembly["database_images"],
            unmapped_items=unmapped,
            unverified_risks=risks,
        )
        return {
            "ref": report.ref.model_dump(),
            "mapping": [r.model_dump(mode="json") for r in report.mapping],
            "report_ref": self._put(report, data.migration_id, "analysis-report"),
            "commit": source["commit"],
        }

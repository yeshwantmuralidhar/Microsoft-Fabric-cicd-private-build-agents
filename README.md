# Fabric CI/CD with Build Agents Inside Your Network

Deploy a standalone Microsoft Fabric Notebook from Azure Repos to a test
workspace with public access blocked. Run Azure DevOps YAML jobs on a Windows
Azure VM inside your network, reach Fabric through a workspace-level private
endpoint, and authenticate with the VM's system-assigned managed identity.

**Start here:** [Beginner-friendly end-to-end runbook](<./Fabric CICD with Agents Inside your Network.md>).
Follow its 13 phases in order and complete each checkpoint before continuing.

## Architecture

- **Source control:** Azure Repos stores the Notebook definition and deployment files.
- **Execution:** a dedicated self-hosted Azure DevOps agent runs on a Windows Azure VM.
- **Networking:** private DNS resolves the workspace API to its private endpoint.
- **Authentication:** the VM obtains a managed identity token from its metadata endpoint.
- **Runtime:** Python 3.15 uses only the standard library; no pip installation is required.
- **Target:** one standalone Notebook with no Lakehouse, Environment, or connection dependencies.

The agent-to-Fabric connection is private. Azure DevOps job polling, source
checkout, and approved software updates still require outbound connectivity.

## Files

| File | Purpose |
| --- | --- |
| [End-to-end runbook](<./Fabric CICD with Agents Inside your Network.md>) | Prerequisites, setup, checkpoints, troubleshooting, security, rollback, and production considerations |
| [azure-pipelines-deploy.yml](./azure-pipelines-deploy.yml) | Three-stage, manually queued deployment pipeline with publishing disabled by default |
| [deploy_fabric.py](./deploy_fabric.py) | Managed-identity REST client, read-only workspace check, and Notebook create/update deployment |
| [requirements.txt](./requirements.txt) | Documents the dependency-free standard-library implementation |
| [test_deploy_fabric.py](./test_deploy_fabric.py) | Offline unit tests with mocked network calls |
| [workspace-private-link.json](./workspace-private-link.json) | Parameterized ARM template for the Fabric workspace private-link resource |

Environment screenshots are kept local and excluded from this public repository.
Use the runbook checkpoints to collect evidence for your own environment.

## Prerequisites

Use the runbook's worksheet to record IDs, names, paths, and endpoint IPs.
Arrange access for the Azure/network, Windows, Fabric, Entra, and Azure DevOps
administrators involved in setup.

- A development workspace for authoring and Git synchronization.
- A test workspace on supported F SKU capacity with no Power BI semantic models
  and no Fabric deployment pipeline assignment.
- Approved workspace-level Private Link, private DNS, and VM network configuration.
- A Windows Azure VM with Python 3.15, Git, and a registered self-hosted agent.
- The VM identity enabled, included in the approved security group, allowed by
  the Fabric API tenant setting, and granted target workspace Contributor access.
- An Azure Repos repository and a reviewed branch containing the deployment files.

Refer to the runbook for current tenant settings, portal paths, permissions, and
Microsoft Learn references.

## Getting started

1. Follow the runbook to configure the private endpoint, VM, DNS, and agent pool.
2. Enable the VM identity and grant the documented Fabric permissions.
3. Install Python 3.15 and Git for all users on the VM.
4. Commit the Fabric-generated Notebook definition under `workspace` in Azure Repos.
5. Place the pipeline, deployer, requirements, and unit tests at repository root.
6. Replace the five placeholders in [azure-pipelines-deploy.yml](./azure-pipelines-deploy.yml).
7. Create an Azure DevOps pipeline using that existing YAML file.
8. Authorize only the intended pipeline to use the dedicated pool.
9. Queue a read-only run with the publish checkbox unchecked.
10. Restrict public workspace access only after private access succeeds.
11. Repeat the read-only run, then enable publishing for a reviewed deployment.
12. Collect private-routing evidence and repeat a Notebook update as the runbook describes.

### Pipeline configuration

| YAML variable | Replace with |
| --- | --- |
| `POOL_NAME` | Dedicated project-accessible self-hosted pool name |
| `EXPECTED_AGENT_NAME` | Exact registered agent name; all stages demand this agent |
| `FABRIC_WORKSPACE_ID` | Target test workspace UUID |
| `EXPECTED_PRIVATE_IPS` | Comma-separated approved workspace API private endpoint IPv4 addresses, not the VM IP |
| `PYTHON_EXE` | Full path to the all-users Python 3.15 executable |

The publish task reads Notebook definitions from `workspace` beneath the
checked-out repository root. Update the task if your source directory differs.

### Pipeline stages

| Stage | Checks or actions | Fabric changes |
| --- | --- | --- |
| `preflight` | Agent details, Python 3.15, expected DNS IPs, TCP 443 destination, and mocked unit tests | None |
| `read_only` | Read target workspace details with the VM's managed identity | None |
| `publish` | Create or update Notebook definitions after preceding stages succeed and `deploy` is true | Yes |

The boolean `deploy` parameter defaults to **false**. A normal initial run should
show successful preflight/read-only stages and a skipped publish stage.
The checkbox controls execution; it is not a security approval.

## Run offline tests

Run from the repository root on the VM. Replace both example placeholders with
your actual local path and Python executable:

```powershell
Set-Location "REPLACE-WITH-LOCAL-REPO"
& "REPLACE-WITH-PYTHON-EXE" -B -m unittest -v test_deploy_fabric
```

The tests mock network calls and do not contact Fabric or VM metadata.
They validate script behavior, not live permissions, routing, or Notebook execution.

## Deployment behavior and boundaries

- Match target Notebooks by case-insensitive display name.
- Create an item when no matching name exists; update the definition when it does.
- Treat publishing as an overwrite of an existing matching Notebook's definition.
- Preserve Fabric-generated `.platform` and Notebook content files.
- Poll asynchronous operations and check that the published names exist.
- Review actual target content to verify the intended version was deployed.
- Do not expect dependency rebinding, parameter substitution, item deletion,
  Notebook execution, automatic retries, or automatic rollback.
- Inspect any unresolved Fabric operation before retrying a timed-out deployment.

Renaming a source Notebook can create another target item rather than rename
the existing one. The deployer rejects `parameter.yml`.

## Security and private-routing evidence

Protect repository changes and pool authorization: code running on the agent VM
can request tokens for its managed identity. Do not run untrusted code there.
The registration PAT is only for agent setup; keep it out of pipelines and Git.

A successful job alone does not prove private routing. Retain:

- The expected agent/machine details and private DNS/TCP destination IPs.
- The workspace policy blocking public access without public IP exceptions.
- Successful private-agent workspace reads and publishing under that policy.
- An authorized outside-network client's blocked access.
- Network flow evidence when required by your audit process.

For troubleshooting, recovery, decommissioning, and production hardening,
continue with Parts D and E of the [runbook](<./Fabric CICD with Agents Inside your Network.md>).

# openhands

![Version: 0.4.0](https://img.shields.io/badge/Version-0.4.0-informational?style=flat-square) ![Type: application](https://img.shields.io/badge/Type-application-informational?style=flat-square) ![AppVersion: 0.40.0](https://img.shields.io/badge/AppVersion-0.40.0-informational?style=flat-square)

A Helm chart for deploying OpenHands with Kubernetes runtime support.

## Installation

### From OCI Registry (Recommended)

```bash
helm install openhands oci://ghcr.io/all-hands-ai/openhands --version 0.4.0
```

### From Source

```bash
git clone https://github.com/All-Hands-AI/OpenHands.git
cd OpenHands/charts
helm install openhands ./openhands
```

## Configuration

### Runtime Image Override

You can override the runtime image used by OpenHands in several ways:

1. **Using runtimeImage values (recommended):**
```yaml
runtimeImage:
  repository: ghcr.io/all-hands-ai/runtime
  tag: "0.40.0-ubuntu"  # or "0.40.0-nikolaik"
```

2. **Using direct configuration:**
```yaml
config:
  sandboxRuntimeContainerImage: "ghcr.io/all-hands-ai/runtime:0.40.0-ubuntu"
```

### Available Runtime Images

- `ghcr.io/all-hands-ai/runtime:0.40.0-nikolaik` - Based on nikolaik/python-nodejs (default)
- `ghcr.io/all-hands-ai/runtime:0.40.0-ubuntu` - Based on Ubuntu 24.04

## Values

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| affinity | object | `{}` |  |
| args | list | `[]` | Additional command line arguments for the OpenHands controller/server. |
| config.runtime | string | `"kubernetes"` | Set the RUNTIME env var to use kubernetes runtime. |
| config.sandboxRuntimeContainerImage | string | `""` | Image to use for runtime pods. If not set, will use runtimeImage.repository:runtimeImage.tag |
| env | list | `[]` | Additional environment variables for the deployment. |
| fullnameOverride | string | `""` |  |
| image.pullPolicy | string | `"IfNotPresent"` | The image pull policy to use for the deployment. |
| image.repository | string | `"ghcr.io/all-hands-ai/openhands"` | The container image to use for the deployment. |
| image.tag | string | `"0.40.0"` | The tag to use for the deployment. |
| imagePullSecrets | list | `[]` | Optional image pull secrets for private registries. |
| ingress.annotations | object | `{}` |  |
| ingress.className | string | `""` |  |
| ingress.enabled | bool | `false` |  |
| ingress.hosts[0].host | string | `"chart-example.local"` |  |
| ingress.hosts[0].paths[0].path | string | `"/"` |  |
| ingress.hosts[0].paths[0].pathType | string | `"ImplementationSpecific"` |  |
| ingress.tls | list | `[]` |  |
| kubernetesConfig.imagePullSecret | string | `nil` | Optional name of image pull secret for private registries |
| kubernetesConfig.ingressDomain | string | `"localhost"` | Domain for runtime ingress resources |
| kubernetesConfig.ingressTlsSecret | string | `nil` | Optional name of TLS secret for ingress |
| kubernetesConfig.namespace | string | `"default"` | The Kubernetes namespace to use for openhands runtime. |
| kubernetesConfig.nodeSelectorKey | string | `nil` | Optional node selector key for runtime pod scheduling |
| kubernetesConfig.nodeSelectorVal | string | `nil` | Optional node selector value for runtimepod scheduling |
| kubernetesConfig.privileged | bool | `true` | Runtime pods runs with root access for Docker support. |
| kubernetesConfig.pvcStorageClass | string | `nil` | Storage class for runtime persistent volume claims |
| kubernetesConfig.pvcStorageSize | string | `"2Gi"` | Size of the persistent volume claim for runtime pods. |
| kubernetesConfig.resourceCpuRequest | string | `"1"` | CPU request for runtime pods |
| kubernetesConfig.resourceMemoryLimit | string | `"2Gi"` | Memory limit for runtime pods |
| kubernetesConfig.resourceMemoryRequest | string | `"1Gi"` | Memory request for runtime pods |
| kubernetesConfig.tolerations | list | `[]` | Pod tolerations for runtime pods Will be converted to YAML string for SANDBOX__KUBERNETES__TOLERATIONS_YAML |
| livenessProbe.httpGet.path | string | `"/alive"` |  |
| livenessProbe.httpGet.port | string | `"http"` |  |
| nameOverride | string | `""` |  |
| nodeSelector | object | `{}` |  |
| podAnnotations | object | `{}` |  |
| podLabels | object | `{}` |  |
| podSecurityContext | object | `{}` |  |
| readinessProbe.httpGet.path | string | `"/"` |  |
| readinessProbe.httpGet.port | string | `"http"` |  |
| resources | object | `{}` |  |
| runtimeImage.pullPolicy | string | `"IfNotPresent"` | The image pull policy to use for runtime pods. |
| runtimeImage.repository | string | `"ghcr.io/all-hands-ai/runtime"` | The container image repository to use for runtime pods. |
| runtimeImage.tag | string | `"0.40.0-nikolaik"` | The tag to use for runtime pods. |
| securityContext | object | `{}` |  |
| service.port | int | `3000` |  |
| service.type | string | `"ClusterIP"` |  |
| serviceAccount.annotations | object | `{}` | Annotations to add to the service account |
| serviceAccount.automount | bool | `true` | Automatically mount a ServiceAccount's API credentials |
| serviceAccount.name | string | `""` |  |
| tolerations | list | `[]` |  |
| volumeMounts | list | `[]` | Additional volumeMounts on the output Deployment definition. |
| volumes | list | `[]` | Additional volumes on the output Deployment definition. |

----------------------------------------------
Autogenerated from chart metadata using [helm-docs v1.14.2](https://github.com/norwoodj/helm-docs/releases/v1.14.2)

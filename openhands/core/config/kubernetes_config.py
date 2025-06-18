from pydantic import BaseModel, Field


class KubernetesConfig(BaseModel):
    """Configuration for Kubernetes runtime.

    Attributes:
        namespace: The Kubernetes namespace to use for OpenHands resources
        ingress_domain: Domain for ingress resources
        pvc_storage_size: Size of the persistent volume claim (e.g. "2Gi")
        pvc_storage_class: Storage class for persistent volume claims
        resource_cpu_request: CPU request for runtime pods
        resource_memory_request: Memory request for runtime pods
        resource_memory_limit: Memory limit for runtime pods
        image_pull_secret: Optional name of image pull secret for private registries
        ingress_tls_secret: Optional name of TLS secret for ingress
        node_selector_key: Optional node selector key for pod scheduling
        node_selector_val: Optional node selector value for pod scheduling
        tolerations_yaml: Optional YAML string defining pod tolerations
        annotations: Custom annotations to add to the runtime pod
    """

    namespace: str = Field(
        default='default',
        description='The Kubernetes namespace to use for OpenHands resources',
    )
    ingress_domain: str = Field(
        default='localhost', description='Domain for ingress resources'
    )
    pvc_storage_size: str = Field(
        default='2Gi', description='Size of the persistent volume claim'
    )
    pvc_storage_class: str | None = Field(
        default=None, description='Storage class for persistent volume claims'
    )
    resource_cpu_request: str = Field(
        default='1', description='CPU request for runtime pods'
    )
    resource_memory_request: str = Field(
        default='1Gi', description='Memory request for runtime pods'
    )
    resource_memory_limit: str = Field(
        default='2Gi', description='Memory limit for runtime pods'
    )
    image_pull_secret: str | None = Field(
        default=None,
        description='Optional name of image pull secret for private registries',
    )
    ingress_tls_secret: str | None = Field(
        default=None, description='Optional name of TLS secret for ingress'
    )
    node_selector_key: str | None = Field(
        default=None, description='Optional node selector key for pod scheduling'
    )
    node_selector_val: str | None = Field(
        default=None, description='Optional node selector value for pod scheduling'
    )
    tolerations_yaml: str | None = Field(
        default=None, description='Optional YAML string defining pod tolerations'
    )
    privileged: bool = Field(
        default=False,
        description='Run the runtime sandbox container in privileged mode for use with docker-in-docker',
    )
    psc_run_as_user: str | None = Field(
        default=None,
        description='Optional user ID to run the runtime sandbox container as',
    )
    psc_run_as_group: str | None = Field(
        default=None,
        description='Optional group ID to run the runtime sandbox container as',
    )
    psc_fs_group: str | None = Field(
        default=None,
        description='Optional file system group ID for the runtime sandbox container',
    )
    psc_allow_privilege_escalation: str | None = Field(
        default=None,
        description='Optional setting to allow privilege escalation in the runtime sandbox container',
    )
    working_dir: str = Field(
        default="/openhands/code/",
        description='Optional working directory for the runtime sandbox container',
    )
    annotations: dict[str, str] = Field(
        default_factory=dict,
        description='Custom annotations to add to the runtime pod',
    )
    # Redis coordination settings
    redis_coordination_enabled: bool = Field(
        default=True,
        description='Enable Redis coordination to prevent race conditions in multi-replica deployments',
    )
    redis_coordination_timeout: int = Field(
        default=30,
        description='Timeout in seconds to wait for Redis coordination locks',
    )
    redis_coordination_retry_attempts: int = Field(
        default=3,
        description='Number of retry attempts for pod creation when coordination is enabled',
    )

    model_config = {'extra': 'forbid'}

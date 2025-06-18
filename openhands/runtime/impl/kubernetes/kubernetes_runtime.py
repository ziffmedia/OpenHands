from functools import lru_cache
from typing import Callable, Optional
from uuid import UUID

import os
import tenacity
import yaml
from kubernetes import client, config
from kubernetes.client.models import (
    V1Container,
    V1ContainerPort,
    V1EnvVar,
    V1HTTPIngressPath,
    V1HTTPIngressRuleValue,
    V1Ingress,
    V1IngressBackend,
    V1IngressRule,
    V1IngressServiceBackend,
    V1IngressSpec,
    V1IngressTLS,
    V1ObjectMeta,
    V1PersistentVolumeClaim,
    V1PersistentVolumeClaimSpec,
    V1PersistentVolumeClaimVolumeSource,
    V1Pod,
    V1PodSpec,
    V1PodSecurityContext,
    V1ResourceRequirements,
    V1SecurityContext,
    V1Service,
    V1ServiceBackendPort,
    V1ServicePort,
    V1ServiceSpec,
    V1Toleration,
    V1Volume,
    V1VolumeMount,
)

from openhands.core.config import OpenHandsConfig
from openhands.core.exceptions import (
    AgentRuntimeDisconnectedError,
    AgentRuntimeNotFoundError,
)
from openhands.core.logger import DEBUG, DEBUG_RUNTIME
from openhands.core.logger import openhands_logger as logger
from openhands.events import EventStream
from openhands.runtime.impl.action_execution.action_execution_client import (
    ActionExecutionClient,
)
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.utils.command import get_action_execution_server_startup_command
from openhands.runtime.utils.log_streamer import LogStreamer
from openhands.utils.async_utils import call_sync_from_async
from openhands.utils.shutdown_listener import add_shutdown_listener
from openhands.utils.tenacity_stop import stop_if_should_exit

POD_NAME_PREFIX = 'openhands-runtime-'
POD_LABEL = 'openhands-runtime'


class KubernetesRuntime(ActionExecutionClient):
    """
    A Kubernetes runtime for OpenHands that works with Kind.

    This runtime creates pods in a Kubernetes cluster to run the agent code.
    It uses the Kubernetes Python client to create and manage the pods.

    Args:
        config (OpenHandsConfig): The application configuration.
        event_stream (EventStream): The event stream to subscribe to.
        sid (str, optional): The session ID. Defaults to 'default'.
        plugins (list[PluginRequirement] | None, optional): List of plugin requirements. Defaults to None.
        env_vars (dict[str, str] | None, optional): Environment variables to set. Defaults to None.
        status_callback (Callable | None, optional): Callback for status updates. Defaults to None.
        attach_to_existing (bool, optional): Whether to attach to an existing pod. Defaults to False.
        headless_mode (bool, optional): Whether to run in headless mode. Defaults to True.
    """

    _shutdown_listener_id: UUID | None = None
    _namespace: str = ''

    def __init__(
        self,
        config: OpenHandsConfig,
        event_stream: EventStream,
        sid: str = 'default',
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
        status_callback: Callable | None = None,
        attach_to_existing: bool = False,
        headless_mode: bool = True,
    ):
        if not KubernetesRuntime._shutdown_listener_id:
            KubernetesRuntime._shutdown_listener_id = add_shutdown_listener(
                lambda: KubernetesRuntime._cleanup_k8s_resources(
                    namespace=self._k8s_namespace,
                    remove_pvc=True,
                    conversation_id=self.sid,
                )  # this is when you ctrl+c.
            )
        self.config = config
        self._runtime_initialized: bool = False
        self.status_callback = status_callback

        # Load and validate Kubernetes configuration
        if self.config.sandbox.kubernetes is None:
            raise ValueError(
                'Kubernetes configuration is required when using KubernetesRuntime. '
                'Please add a [sandbox.kubernetes] section to your configuration.'
            )

        self._k8s_config = self.config.sandbox.kubernetes
        self._k8s_namespace = self._k8s_config.namespace
        KubernetesRuntime._namespace = self._k8s_namespace

        # Initialize ports with default values in the required range
        self._container_port = 8080  # Default internal container port
        self._vscode_port = 8081  # Default VSCode port.
        self._app_ports: list[int] = [
            30082,
            30083,
        ]  # Default app ports in valid range # The agent prefers these when exposing an application.

        self.k8s_client, self.k8s_networking_client = self._init_kubernetes_client()

        self.pod_image = self.config.sandbox.runtime_container_image
        if not self.pod_image:
            # If runtime_container_image isn't set, use the base_container_image as a fallback
            self.pod_image = self.config.sandbox.base_container_image

        self.pod_name = POD_NAME_PREFIX + sid

        # Initialize the API URL with the initial port value
        self.k8s_local_url = f'http://{self._get_svc_name(self.pod_name)}.{self._k8s_namespace}.svc.cluster.local'
        self.api_url = f'{self.k8s_local_url}:{self._container_port}'

        # Buffer for container logs
        self.log_streamer: Optional[LogStreamer] = None

        super().__init__(
            config,
            event_stream,
            sid,
            plugins,
            env_vars,
            status_callback,
            attach_to_existing,
            headless_mode,
        )

    @staticmethod
    def _get_svc_name(pod_name: str) -> str:
        """Get the service name for the pod."""
        return f'{pod_name}-svc'

    @staticmethod
    def _get_vscode_svc_name(pod_name: str) -> str:
        """Get the VSCode service name for the pod."""
        return f'{pod_name}-svc-code'

    @staticmethod
    def _get_vscode_ingress_name(pod_name: str) -> str:
        """Get the VSCode ingress name for the pod."""
        return f'{pod_name}-ingress-code'

    @staticmethod
    def _get_vscode_tls_secret_name(pod_name: str) -> str:
        """Get the TLS secret name for the VSCode ingress."""
        return f'{pod_name}-tls-secret'

    @staticmethod
    def _get_pvc_name(pod_name: str) -> str:
        """Get the PVC name for the pod."""
        return f'{pod_name}-pvc'

    @staticmethod
    def _get_pod_name(sid: str) -> str:
        """Get the pod name for the session."""
        return POD_NAME_PREFIX + sid

    @property
    def action_execution_server_url(self):
        return self.api_url

    @property
    def node_selector(self) -> dict[str, str] | None:
        if (
            not self._k8s_config.node_selector_key
            or not self._k8s_config.node_selector_val
        ):
            return None
        return {self._k8s_config.node_selector_key: self._k8s_config.node_selector_val}

    @property
    def tolerations(self) -> list[V1Toleration] | None:
        if not self._k8s_config.tolerations_yaml:
            return None
        tolerations_yaml_str = self._k8s_config.tolerations_yaml
        tolerations = []
        try:
            tolerations_data = yaml.safe_load(tolerations_yaml_str)
            if isinstance(tolerations_data, list):
                for toleration in tolerations_data:
                    tolerations.append(V1Toleration(**toleration))
            else:
                logger.error(
                    f'Invalid tolerations format. Should be type list: {tolerations_yaml_str}. Expected a list.'
                )
                return None
        except yaml.YAMLError as e:
            logger.error(
                f'Error parsing tolerations YAML: {tolerations_yaml_str}. Error: {e}'
            )
            return None
        return tolerations

    async def connect(self):
        """Connect to the runtime by creating or attaching to a pod."""
        self.log('info', f'Connecting to runtime with conversation ID: {self.sid}')
        self.log('info', f'self._attach_to_existing: {self.attach_to_existing}')
        self.send_status_message('STATUS$STARTING_RUNTIME')
        self.log('info', f'Using API URL {self.api_url}')

        try:
            await call_sync_from_async(self._attach_to_pod)
        except client.rest.ApiException as e:
            # we are not set to attach to existing, ignore error and init k8s resources.
            if self.attach_to_existing:
                self.log(
                    'error',
                    f'Pod {self.pod_name} not found or cannot connect to it.',
                )
                raise AgentRuntimeDisconnectedError from e

            self.log('info', f'Starting runtime with image: {self.pod_image}')
            try:
                await call_sync_from_async(self._init_k8s_resources)
                self.log(
                    'info',
                    f'Pod started: {self.pod_name}. VSCode URL: {self.vscode_url}',
                )
            except Exception as init_error:
                self.log('error', f'Failed to initialize k8s resources: {init_error}')
                raise AgentRuntimeNotFoundError(
                    f'Failed to initialize kubernetes resources: {init_error}'
                ) from init_error

        if DEBUG_RUNTIME:
            # Log streamer for kubernetes pods
            # This is a simplified version that just uses the pod name
            self.log_streamer = LogStreamer(self.pod_name, self.log)
        else:
            self.log_streamer = None

        if not self.attach_to_existing:
            self.log('info', 'Waiting for pod to become ready ...')
            self.send_status_message('STATUS$WAITING_FOR_CLIENT')

        try:
            await call_sync_from_async(self._wait_until_ready)
        except Exception as alive_error:
            self.log('error', f'Failed to connect to runtime: {alive_error}')
            self.send_error_message(
                'ERROR$RUNTIME_CONNECTION',
                f'Failed to connect to runtime: {alive_error}',
            )
            raise AgentRuntimeDisconnectedError(
                f'Failed to connect to runtime: {alive_error}'
            ) from alive_error

        if not self.attach_to_existing:
            self.log('info', 'Runtime is ready.')

        if not self.attach_to_existing:
            await call_sync_from_async(self.setup_initial_env)

        self.log(
            'info',
            f'Pod initialized with plugins: {[plugin.name for plugin in self.plugins]}. VSCode URL: {self.vscode_url}',
        )
        if not self.attach_to_existing:
            self.send_status_message(' ')
        self._runtime_initialized = True

    def _attach_to_pod(self):
        """Attach to an existing pod."""
        try:
            pod = self.k8s_client.read_namespaced_pod(
                name=self.pod_name, namespace=self._k8s_namespace
            )

            if pod.status.phase != 'Running':
                try:
                    self._wait_until_ready()
                except TimeoutError:
                    raise AgentRuntimeDisconnectedError(
                        f'Pod {self.pod_name} exists but failed to become ready.'
                    )

            self.log('info', f'Successfully attached to pod {self.pod_name}')
            return True

        except client.rest.ApiException as e:
            self.log('error', f'Failed to attach to pod: {e}')
            raise

    @tenacity.retry(
        stop=tenacity.stop_after_delay(180) | stop_if_should_exit(),
        retry=tenacity.retry_if_exception_type(TimeoutError),
        reraise=True,
        wait=tenacity.wait_fixed(3),
    )
    def _wait_until_ready(self):
        """Wait until the runtime server is alive by checking the pod status in Kubernetes."""
        self.log('info', f'Checking if pod {self.pod_name} is ready in Kubernetes')
        pod = self.k8s_client.read_namespaced_pod(
            name=self.pod_name, namespace=self._k8s_namespace
        )

        # Log detailed pod status for debugging
        self.log('info', f'Pod phase: {pod.status.phase}')
        if pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                self.log('info', f'Container {container_status.name} ready: {container_status.ready}')
                if container_status.state.waiting:
                    self.log('error', f'Container {container_status.name} waiting: {container_status.state.waiting.reason} - {container_status.state.waiting.message}')
                if container_status.state.terminated:
                    self.log('error', f'Container {container_status.name} terminated: {container_status.state.terminated.reason} - {container_status.state.terminated.message}')

        # Log pod events for debugging
        try:
            events = self.k8s_client.list_namespaced_event(
                namespace=self._k8s_namespace,
                field_selector=f'involvedObject.name={self.pod_name}'
            )
            for event in events.items[-5:]:  # Show last 5 events
                self.log('warning', f'Pod event: {event.type} - {event.reason}: {event.message}')
        except Exception as e:
            self.log('warning', f'Could not fetch pod events: {e}')

        if pod.status.phase == 'Running' and pod.status.conditions:
            for condition in pod.status.conditions:
                if condition.type == 'Ready' and condition.status == 'True':
                    self.log('info', f'Pod {self.pod_name} is ready!')
                    return True  # Exit the function if the pod is ready

        # More detailed error reporting for non-ready pods
        if pod.status.phase == 'Failed':
            debug_info = self._get_pod_debug_info()
            self.log('error', f'Pod failed - Debug info:\n{debug_info}')
            raise RuntimeError(f'Pod {self.pod_name} failed to start. Check pod events and logs.')
        elif pod.status.phase == 'Pending':
            # Check if it's a scheduling issue
            if pod.status.conditions:
                for condition in pod.status.conditions:
                    if condition.type == 'PodScheduled' and condition.status == 'False':
                        self.log('error', f'Pod scheduling failed: {condition.reason} - {condition.message}')
            self.log('warning', f'Pod {self.pod_name} is still pending. This could be due to resource constraints, image pull issues, or scheduling problems.')

        self.log(
            'info',
            f'Pod {self.pod_name} is not ready yet. Current phase: {pod.status.phase}',
        )
        raise TimeoutError(f'Pod {self.pod_name} is not in Running state yet.')

    @staticmethod
    @lru_cache(maxsize=1)
    def _init_kubernetes_client() -> tuple[client.CoreV1Api, client.NetworkingV1Api]:
        """Initialize the Kubernetes client."""
        try:
            config.load_incluster_config()  # Even local usage with mirrord technically uses an incluster config.
            return client.CoreV1Api(), client.NetworkingV1Api()
        except Exception as ex:
            logger.error(
                'Failed to initialize Kubernetes client. Make sure you have kubectl configured correctly or are running in a Kubernetes cluster.',
            )
            raise ex

    @staticmethod
    def _cleanup_k8s_resources(
        namespace: str, remove_pvc: bool = False, conversation_id: str = ''
    ):
        """Clean up Kubernetes resources with our prefix in the namespace.

        :param remove_pvc: If True, also remove persistent volume claims (defaults to False).
        """
        try:
            k8s_api, k8s_networking_api = KubernetesRuntime._init_kubernetes_client()

            pod_name = KubernetesRuntime._get_pod_name(conversation_id)
            service_name = KubernetesRuntime._get_svc_name(pod_name)
            vscode_service_name = KubernetesRuntime._get_vscode_svc_name(pod_name)
            ingress_name = KubernetesRuntime._get_vscode_ingress_name(pod_name)
            pvc_name = KubernetesRuntime._get_pvc_name(pod_name)

            # Delete PVC if requested
            if remove_pvc:
                try:
                    k8s_api.delete_namespaced_persistent_volume_claim(
                        name=pvc_name,
                        namespace=namespace,
                        body=client.V1DeleteOptions(),
                    )
                    logger.info(f'Deleted PVC {pvc_name}')
                except client.rest.ApiException as e:
                    if e.status == 404:
                        logger.info(f'PVC {pvc_name} not found, already deleted')
                    else:
                        logger.error(f'Error deleting PVC {pvc_name}: {e}')

            try:
                k8s_api.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    body=client.V1DeleteOptions(),
                )
                logger.info(f'Deleted pod {pod_name}')
            except client.rest.ApiException as e:
                if e.status == 404:
                    logger.info(f'Pod {pod_name} not found, already deleted')
                else:
                    logger.error(f'Error deleting pod {pod_name}: {e}')

            try:
                k8s_api.delete_namespaced_service(
                    name=service_name,
                    namespace=namespace,
                )
                logger.info(f'Deleted service {service_name}')
            except client.rest.ApiException as e:
                if e.status == 404:
                    logger.info(f'Service {service_name} not found, already deleted')
                else:
                    logger.error(f'Error deleting service {service_name}: {e}')

            try:
                k8s_api.delete_namespaced_service(
                    name=vscode_service_name, namespace=namespace
                )
                logger.info(f'Deleted service {vscode_service_name}')
            except client.rest.ApiException as e:
                if e.status == 404:
                    logger.info(f'Service {vscode_service_name} not found, already deleted')
                else:
                    logger.error(f'Error deleting service {vscode_service_name}: {e}')

            try:
                k8s_networking_api.delete_namespaced_ingress(
                    name=ingress_name, namespace=namespace
                )
                logger.info(f'Deleted ingress {ingress_name}')
            except client.rest.ApiException as e:
                if e.status == 404:
                    logger.info(f'Ingress {ingress_name} not found, already deleted')
                else:
                    logger.error(f'Error deleting ingress {ingress_name}: {e}')

            logger.info('Cleaned up Kubernetes resources')
        except Exception as e:
            logger.error(f'Error cleaning up k8s resources: {e}')

    def _get_pvc_manifest(self):
        """Create a PVC manifest for the runtime pod."""
        # Create PVC
        pvc = V1PersistentVolumeClaim(
            api_version='v1',
            kind='PersistentVolumeClaim',
            metadata=V1ObjectMeta(
                name=self._get_pvc_name(self.pod_name), namespace=self._k8s_namespace
            ),
            spec=V1PersistentVolumeClaimSpec(
                access_modes=['ReadWriteOnce'],
                resources=client.V1ResourceRequirements(
                    requests={'storage': self._k8s_config.pvc_storage_size}
                ),
                storage_class_name=self._k8s_config.pvc_storage_class,
            ),
        )

        return pvc

    def _get_vscode_service_manifest(self):
        """Create a service manifest for the VSCode server."""

        vscode_service_spec = V1ServiceSpec(
            selector={'app': POD_LABEL, 'session': self.sid},
            type='ClusterIP',
            ports=[
                V1ServicePort(
                    port=self._vscode_port,
                    target_port='vscode',
                    name='code',
                )
            ],
        )

        vscode_service = V1Service(
            metadata=V1ObjectMeta(name=self._get_vscode_svc_name(self.pod_name)),
            spec=vscode_service_spec,
        )
        return vscode_service

    def _get_runtime_service_manifest(self):
        """Create a service manifest for the runtime pod execution-server."""
        service_spec = V1ServiceSpec(
            selector={'app': POD_LABEL, 'session': self.sid},
            type='ClusterIP',
            ports=[
                V1ServicePort(
                    port=self._container_port,
                    target_port='http',
                    name='execution-server',
                )
            ],
        )

        service = V1Service(
            metadata=V1ObjectMeta(name=self._get_svc_name(self.pod_name)),
            spec=service_spec,
        )
        return service

    def _get_runtime_pod_manifest(self):
        """Create a pod manifest for the runtime sandbox."""
        # Prepare environment variables
        environment = [
            V1EnvVar(name='port', value=str(self._container_port)),
            V1EnvVar(name='PYTHONUNBUFFERED', value='1'),
            V1EnvVar(name='VSCODE_PORT', value=str(self._vscode_port)),
        ]

        if self.config.debug or DEBUG:
            environment.append(V1EnvVar(name='DEBUG', value='true'))

        # Add runtime startup env vars
        for key, value in self.config.sandbox.runtime_startup_env_vars.items():
            environment.append(V1EnvVar(name=key, value=value))

        # Add SANDBOX_ENV_ variables
        for key, value in self.initial_env_vars.items():
            environment.append(V1EnvVar(name=key, value=value))

        # Prepare volume mounts if workspace is configured
        volume_mounts = [
            V1VolumeMount(
                name='workspace-volume',
                mount_path=self.config.workspace_mount_path_in_sandbox,
            ),
        ]
        volumes = [
            V1Volume(
                name='workspace-volume',
                persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                    claim_name=self._get_pvc_name(self.pod_name)
                ),
            )
        ]

        # Prepare container ports
        container_ports = [
            V1ContainerPort(container_port=self._container_port, name='http'),
        ]

        if self.vscode_enabled:
            container_ports.append(
                V1ContainerPort(container_port=self._vscode_port, name='vscode')
            )

        for port in self._app_ports:
            container_ports.append(V1ContainerPort(container_port=port))

        # Define the readiness probe
        health_check = client.V1Probe(
            http_get=client.V1HTTPGetAction(
                path='/alive',
                port=self._container_port,  # Or the port your application listens on
            ),
            initial_delay_seconds=5,  # Adjust as needed
            period_seconds=10,  # Adjust as needed
            timeout_seconds=5,  # Adjust as needed
            success_threshold=1,
            failure_threshold=3,
        )

        if os.environ.get('CUSTOM_RUNTIME_COMMAND'):
          command = os.environ.get('CUSTOM_RUNTIME_COMMAND').split()
        else:
          # Prepare command
          # Entry point command for generated sandbox runtime pod.
          command = get_action_execution_server_startup_command(
              server_port=self._container_port,
              plugins=self.plugins,
              app_config=self.config,
              override_user_id=0,  # if we use the default of app_config.run_as_openhands then we cant edit files in vscode due to file perms.
              override_username='root',
          )

        # Prepare resource requirements based on config with fallback to smaller defaults
        try:
            memory_limit = self._k8s_config.resource_memory_limit
            cpu_request = self._k8s_config.resource_cpu_request
            memory_request = self._k8s_config.resource_memory_request
        except AttributeError:
            # Fallback to more conservative defaults if config is incomplete
            memory_limit = '2Gi'
            cpu_request = '500m'
            memory_request = '1Gi'
            self.log('warning', 'Using fallback resource limits due to incomplete config')

        resources = V1ResourceRequirements(
            limits={'memory': memory_limit},
            requests={
                'cpu': cpu_request,
                'memory': memory_request,
            },
        )

        self.log('info', f'Pod resources: requests={resources.requests}, limits={resources.limits}')

        # Set security context for the container with more permissive defaults if needed
        try:
            privileged = self._k8s_config.privileged
        except AttributeError:
            privileged = False
            self.log('warning', 'Privileged mode not configured, defaulting to False')

        security_context = V1SecurityContext(
            privileged=privileged,
            # Add some defaults that often help with permission issues
            run_as_user=0 if privileged else None,
            allow_privilege_escalation=privileged,
        )

        # Create the container definition
        container = V1Container(
            name='runtime',
            image=self.pod_image,
            command=command,
            env=environment,
            ports=container_ports,
            volume_mounts=volume_mounts,
            working_dir=self._k8s_config.working_dir,
            resources=resources,
            readiness_probe=health_check,
            security_context=security_context,
            image_pull_policy='IfNotPresent',  # Try to use local image first to avoid pull issues
        )

        self.log('info', f'Container image: {self.pod_image}')
        self.log('info', f'Container command: {" ".join(command) if command else "default"}')
        self.log('info', f'Working directory: {self._k8s_config.working_dir}')

        # Create the pod definition
        image_pull_secrets = None
        if self._k8s_config.image_pull_secret:
            image_pull_secrets = [
                client.V1LocalObjectReference(name=self._k8s_config.image_pull_secret)
            ]

        # Build pod security context with safer defaults
        pod_security_context = V1PodSecurityContext()
        try:
            if self._k8s_config.psc_run_as_user is not None:
                pod_security_context.run_as_user = int(self._k8s_config.psc_run_as_user)
            if self._k8s_config.psc_run_as_group is not None:
                pod_security_context.run_as_group = int(self._k8s_config.psc_run_as_group)
            if self._k8s_config.psc_fs_group is not None:
                pod_security_context.fs_group = int(self._k8s_config.psc_fs_group)
            if self._k8s_config.psc_allow_privilege_escalation is not None:
                pod_security_context.allow_privilege_escalation = self._k8s_config.psc_allow_privilege_escalation.lower() == 'true'
        except (AttributeError, ValueError) as e:
            self.log('warning', f'Error setting pod security context: {e}, using defaults')
            # Use safer defaults if configuration is missing or invalid
            if privileged:
                pod_security_context.run_as_user = 0
                pod_security_context.allow_privilege_escalation = True

        self.log('info', f'Pod security context: run_as_user={getattr(pod_security_context, "run_as_user", None)}, fs_group={getattr(pod_security_context, "fs_group", None)}')

        pod = V1Pod(
            metadata=V1ObjectMeta(
                name=self.pod_name,
                labels={'app': POD_LABEL, 'session': self.sid},
                annotations=self._k8s_config.annotations
            ),
            spec=V1PodSpec(
                containers=[container],
                volumes=volumes,
                restart_policy='Never',
                image_pull_secrets=image_pull_secrets,
                node_selector=self.node_selector,
                tolerations=self.tolerations,
                security_context=pod_security_context,
            ),
        )

        return pod

    def _get_vscode_ingress_manifest(self):
        """Create an ingress manifest for the VSCode server."""

        tls = []
        if self._k8s_config.ingress_tls_secret:
            runtime_tls = V1IngressTLS(
                hosts=[self.ingress_domain],
                secret_name=self._k8s_config.ingress_tls_secret,
            )
            tls = [runtime_tls]

        rules = [
            V1IngressRule(
                host=self.ingress_domain,
                http=V1HTTPIngressRuleValue(
                    paths=[
                        V1HTTPIngressPath(
                            path='/',
                            path_type='Prefix',
                            backend=V1IngressBackend(
                                service=V1IngressServiceBackend(
                                    port=V1ServiceBackendPort(
                                        number=self._vscode_port,
                                    ),
                                    name=self._get_vscode_svc_name(self.pod_name),
                                )
                            ),
                        )
                    ]
                ),
            )
        ]
        ingress_spec = V1IngressSpec(rules=rules, tls=tls)

        ingress = V1Ingress(
            api_version='networking.k8s.io/v1',
            metadata=V1ObjectMeta(
                name=self._get_vscode_ingress_name(self.pod_name),
                annotations={
                    'external-dns.alpha.kubernetes.io/hostname': self.ingress_domain
                },
            ),
            spec=ingress_spec,
        )

        return ingress

    def _pvc_exists(self):
        """Check if the PVC already exists."""
        try:
            pvc = self.k8s_client.read_namespaced_persistent_volume_claim(
                name=self._get_pvc_name(self.pod_name), namespace=self._k8s_namespace
            )
            return pvc is not None
        except client.rest.ApiException as e:
            if e.status == 404:
                return False
            self.log('error', f'Error checking PVC existence: {e}')

    def _init_k8s_resources(self):
        """Initialize the Kubernetes resources."""
        self.log('info', 'Preparing to start pod...')
        self.send_status_message('STATUS$PREPARING_CONTAINER')

        self.log('info', f'Runtime will be accessible at {self.api_url}')
        self.log('info', f'Using image: {self.pod_image}')
        self.log('info', f'Namespace: {self._k8s_namespace}')
        self.log('info', f'Resource limits: CPU={self._k8s_config.resource_cpu_request}, Memory={self._k8s_config.resource_memory_limit}')

        pod = self._get_runtime_pod_manifest()
        service = self._get_runtime_service_manifest()
        vscode_service = self._get_vscode_service_manifest()
        pvc_manifest = self._get_pvc_manifest()
        ingress = self._get_vscode_ingress_manifest()

        # Create the pod in Kubernetes
        try:
            if not self._pvc_exists():
                # Create PVC if it doesn't exist
                try:
                    self.k8s_client.create_namespaced_persistent_volume_claim(
                        namespace=self._k8s_namespace, body=pvc_manifest
                    )
                    self.log('info', f'Created PVC {self._get_pvc_name(self.pod_name)}')
                except client.rest.ApiException as pvc_error:
                    self.log('error', f'Failed to create PVC: {pvc_error}')
                    if 'StorageClass' in str(pvc_error):
                        self.log('error', f'Storage class "{self._k8s_config.pvc_storage_class}" may not exist in the cluster')
                    raise
            else:
                self.log('info', f'PVC {self._get_pvc_name(self.pod_name)} already exists')

            try:
                self.k8s_client.create_namespaced_pod(
                    namespace=self._k8s_namespace, body=pod
                )
            except client.rest.ApiException as pod_error:
                self.log('error', f'Failed to create pod: {pod_error}')
                if 'Forbidden' in str(pod_error):
                    self.log('error', 'Pod creation forbidden - check RBAC permissions and security policies')
                elif 'invalid' in str(pod_error).lower():
                    self.log('error', 'Pod manifest is invalid - check resource limits, security context, and image name')
                raise
            self.log('info', f'Created pod {self.pod_name}.')
            # Create a service to expose the pod for external access
            self.k8s_client.create_namespaced_service(
                namespace=self._k8s_namespace, body=service
            )
            self.log('info', f'Created service {self._get_svc_name(self.pod_name)}')

            # Create second service service for the vscode server.
            self.k8s_client.create_namespaced_service(
                namespace=self._k8s_namespace, body=vscode_service
            )
            self.log(
                'info', f'Created service {self._get_vscode_svc_name(self.pod_name)}'
            )

            # create the vscode ingress.
            self.k8s_networking_client.create_namespaced_ingress(
                namespace=self._k8s_namespace, body=ingress
            )
            self.log(
                'info',
                f'Created ingress {self._get_vscode_ingress_name(self.pod_name)}',
            )

            # Wait for the pod to be running
            self._wait_until_ready()

        except client.rest.ApiException as e:
            self.log('error', f'Failed to create pod and services: {e}')
            raise
        except RuntimeError as e:
            self.log('error', f'Port forwarding failed: {e}')
            raise

    def close(self):
        """Close the runtime and clean up resources."""
        # this is called when a single conversation question is answered or a tab is closed.
        self.log(
            'info',
            f'Closing runtime and cleaning up resources for conersation ID: {self.sid}',
        )
        # Call parent class close method first
        super().close()

        # Close log streamer if it exists
        if self.log_streamer:
            self.log_streamer.close()

        # Return early if we should keep the runtime alive or if we're attaching to existing
        if self.config.sandbox.keep_runtime_alive or self.attach_to_existing:
            self.log(
                'info', 'Keeping runtime alive due to configuration or attach mode'
            )
            return

        try:
            self._cleanup_k8s_resources(
                namespace=self._k8s_namespace,
                remove_pvc=False,
                conversation_id=self.sid,
            )
        except Exception as e:
            self.log('error', f'Error closing runtime: {e}')

    @property
    def ingress_domain(self) -> str:
        """Get the ingress domain for the runtime."""
        return f'{self.sid}.{self._k8s_config.ingress_domain}'

    @property
    def vscode_url(self) -> str | None:
        """Get the URL for VSCode server if enabled."""
        if not self.vscode_enabled:
            return None
        token = super().get_vscode_token()
        if not token:
            return None

        protocol = 'https' if self._k8s_config.ingress_tls_secret else 'http'
        vscode_url = f'{protocol}://{self.ingress_domain}/?tkn={token}&folder={self.config.workspace_mount_path_in_sandbox}'
        self.log('info', f'VSCode URL: {vscode_url}')
        return vscode_url

    @property
    def web_hosts(self) -> dict[str, int]:
        """Get web hosts dict mapping for browser access."""
        hosts = {}
        for idx, port in enumerate(self._app_ports):
            hosts[f'{self.k8s_local_url}:{port}'] = port
        return hosts

    @classmethod
    async def delete(cls, conversation_id: str):
        """Delete resources associated with a conversation."""
        # This is triggered when you actually do the delete in the UI on the convo.
        try:
            cls._cleanup_k8s_resources(
                namespace=cls._namespace,
                remove_pvc=True,
                conversation_id=conversation_id,
            )

        except Exception as e:
            logger.error(
                f'Error deleting resources for conversation {conversation_id}: {e}'
            )

    def _get_pod_debug_info(self) -> str:
        """Get detailed debugging information about the pod."""
        try:
            debug_info = []

            # Get pod details
            pod = self.k8s_client.read_namespaced_pod(
                name=self.pod_name, namespace=self._k8s_namespace
            )

            debug_info.append(f"Pod Status: {pod.status.phase}")
            debug_info.append(f"Pod IP: {pod.status.pod_ip}")
            debug_info.append(f"Host IP: {pod.status.host_ip}")
            debug_info.append(f"Start Time: {pod.status.start_time}")

            # Container statuses
            if pod.status.container_statuses:
                for container_status in pod.status.container_statuses:
                    debug_info.append(f"Container {container_status.name}:")
                    debug_info.append(f"  Ready: {container_status.ready}")
                    debug_info.append(f"  Restart Count: {container_status.restart_count}")
                    if container_status.state.waiting:
                        debug_info.append(f"  Waiting: {container_status.state.waiting.reason} - {container_status.state.waiting.message}")
                    elif container_status.state.running:
                        debug_info.append(f"  Running since: {container_status.state.running.started_at}")
                    elif container_status.state.terminated:
                        debug_info.append(f"  Terminated: {container_status.state.terminated.reason} - {container_status.state.terminated.message}")
                        debug_info.append(f"  Exit Code: {container_status.state.terminated.exit_code}")

            # Pod conditions
            if pod.status.conditions:
                debug_info.append("Pod Conditions:")
                for condition in pod.status.conditions:
                    debug_info.append(f"  {condition.type}: {condition.status} - {condition.reason}")
                    if condition.message:
                        debug_info.append(f"    Message: {condition.message}")

            # Pod events
            try:
                events = self.k8s_client.list_namespaced_event(
                    namespace=self._k8s_namespace,
                    field_selector=f'involvedObject.name={self.pod_name}'
                )
                if events.items:
                    debug_info.append("Recent Events:")
                    for event in sorted(events.items, key=lambda x: x.first_timestamp or x.event_time)[-10:]:
                        timestamp = event.first_timestamp or event.event_time
                        debug_info.append(f"  {timestamp} - {event.type}: {event.reason} - {event.message}")
            except Exception as e:
                debug_info.append(f"Could not fetch events: {e}")

            # Try to get container logs if available
            try:
                logs = self.k8s_client.read_namespaced_pod_log(
                    name=self.pod_name,
                    namespace=self._k8s_namespace,
                    container='runtime',
                    tail_lines=20
                )
                if logs:
                    debug_info.append("Container Logs (last 20 lines):")
                    debug_info.append(logs)
            except Exception as e:
                debug_info.append(f"Could not fetch container logs: {e}")

            return "\n".join(debug_info)

        except Exception as e:
            return f"Error getting pod debug info: {e}"

    def _check_runtime_health(self) -> bool:
        """Check if the runtime server is actually responding to health checks."""
        try:
            import requests
            health_url = f'{self.api_url}/alive'
            response = requests.get(health_url, timeout=5)
            if response.status_code == 200:
                self.log('info', f'Runtime health check passed at {health_url}')
                return True
            else:
                self.log('warning', f'Runtime health check failed with status {response.status_code}')
                return False
        except Exception as e:
            self.log('warning', f'Runtime health check failed: {e}')
            return False

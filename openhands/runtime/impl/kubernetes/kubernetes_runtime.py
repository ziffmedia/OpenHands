from functools import lru_cache
from typing import Callable, Optional
from uuid import UUID

import asyncio
import os
import tenacity
import time
import yaml
from kubernetes import client, config
from openhands.core.shutdown_manager import remove_shutdown_listener, add_shutdown_listener
from kubernetes.client.models import (
    V1Container,
    V1ContainerPort,
    V1ConfigMapVolumeSource,
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

from openhands.utils.tenacity_stop import stop_if_should_exit
from openhands.utils.redis_coordination import get_coordinator

POD_NAME_PREFIX = 'openhands-runtime-'
POD_LABEL = 'openhands-runtime'

# Class-level dictionary to track delayed cleanup tasks for each pod
_delayed_cleanup_tasks: dict[str, asyncio.Task] = {}


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

    _coordinator = None

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
        self._shutdown_listener_id: UUID | None = None

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
        self._shutdown_listener_id = add_shutdown_listener(
            lambda: KubernetesRuntime._cleanup_k8s_resources(
                namespace=self._k8s_namespace,
                remove_pvc=True,
                conversation_id=self.sid,
            )
        )

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

        # Initialize Redis coordinator for multi-replica coordination
        if self._coordinator is None:
            self._coordinator = await get_coordinator()

        # Cancel any pending delayed cleanup for this pod
        self._cancel_delayed_cleanup()

        # Use Redis coordination to prevent race conditions between replicas
        pod_key = f"k8s-pod:{self._k8s_namespace}:{self.pod_name}"        # Perform startup cleanup to handle stale states
        await self._startup_cleanup()

        # Clean up old pods if auto-cleanup is enabled
        await self._cleanup_old_pods_if_needed()

        # Try the enhanced create-or-attach logic with Redis coordination
        try:
            success = await self._create_or_attach_with_coordination(pod_key)
            if not success:
                # Fallback to legacy behavior if coordination fails
                self.log('warning', 'Redis coordination failed, falling back to legacy pod creation')
                await self._fallback_pod_creation()
        except Exception as e:
            self.log('error', f'Pod creation/attachment failed: {e}')
            raise

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
            await call_sync_from_async(self.setup_initial_env)

        self.log(
            'info',
            f'Pod initialized with plugins: {[plugin.name for plugin in self.plugins]}. VSCode URL: {self.vscode_url}',
        )
        if not self.attach_to_existing:
            self.send_status_message(' ')
        self._runtime_initialized = True

    def add_env_vars(self, env_vars: dict[str, str]) -> None:
        """
        Adds environment variables to the Kubernetes runtime environment.

        For KubernetesRuntime, environment variables are handled at the pod creation level
        via the pod manifest. During initialization, we store the variables to be included
        in the pod environment. Once the runtime is initialized, we can optionally
        forward them to the running container via the action execution client.

        This overrides the base Runtime behavior which tries to run shell commands
        before the runtime is fully initialized.
        """
        if not env_vars:
            return

        # Noop - Kubernetes already has the environment variables set at pod creation time

    def _attach_to_pod(self):
        """Attach to an existing pod."""
        try:
            pod = self.k8s_client.read_namespaced_pod(
                name=self.pod_name, namespace=self._k8s_namespace
            )

            self.log('info', f'Found existing pod {self.pod_name} in phase: {pod.status.phase}')

            if pod.status.phase == 'Pending':
                self.log('info', f'Pod {self.pod_name} is pending, waiting for it to become ready')
                self._wait_until_ready()
            elif pod.status.phase == 'Running':
                # Check if pod is actually ready
                if pod.status.container_statuses:
                    all_ready = all(status.ready for status in pod.status.container_statuses)
                    if not all_ready:
                        self.log('info', f'Pod {self.pod_name} is running but containers not ready, waiting')
                        self._wait_until_ready()
                    else:
                        self.log('info', f'Pod {self.pod_name} is running and ready')
                else:
                    # Wait a bit longer to ensure pod is fully ready
                    self._wait_until_ready()
            elif pod.status.phase == 'Failed':
                raise AgentRuntimeDisconnectedError(
                    f'Pod {self.pod_name} is in Failed state. Check pod logs for details.'
                )
            elif pod.status.phase == 'Succeeded':
                raise AgentRuntimeDisconnectedError(
                    f'Pod {self.pod_name} has completed execution and cannot be attached to.'
                )
            else:
                try:
                    self._wait_until_ready()
                except TimeoutError:
                    raise AgentRuntimeDisconnectedError(
                        f'Pod {self.pod_name} exists but failed to become ready within timeout.'
                    )

            self.log('info', f'Successfully attached to pod {self.pod_name}')
            return True

        except client.rest.ApiException as e:
            if e.status == 404:
                self.log('debug', f'Pod {self.pod_name} not found')
            else:
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
            environment.append(V1EnvVar(name=key.upper(), value=value))

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

        # Process configmap volumes if configured
        if self._k8s_config.configmap_volumes:
            configmap_mounts = self._k8s_config.configmap_volumes.split(',')
            for mount in configmap_mounts:
                parts = mount.strip().split(':')
                if len(parts) >= 2:
                    # Support two formats:
                    # 1. configmap_name:mount_path[:mode] - mounts entire configmap as directory
                    # 2. configmap_name:key:mount_path[:mode] - mounts specific key as file using subPath

                    if len(parts) == 2 or (len(parts) == 3 and parts[2].strip() in ['ro', 'rw']):
                        # Format 1: configmap_name:mount_path[:mode]
                        configmap_name = parts[0].strip()
                        mount_path = parts[1].strip()
                        mount_mode = parts[2].strip() if len(parts) == 3 else None
                        sub_path = None
                        key = None
                    elif len(parts) >= 3:
                        # Format 2: configmap_name:key:mount_path[:mode]
                        configmap_name = parts[0].strip()
                        key = parts[1].strip()
                        mount_path = parts[2].strip()
                        mount_mode = parts[3].strip() if len(parts) == 4 else None
                        sub_path = key
                    else:
                        logger.warning(f'Invalid configmap volume format: {mount}. Expected format: "configmap_name:mount_path[:mode]" or "configmap_name:key:mount_path[:mode]"')
                        continue

                    # Generate unique volume name
                    volume_name = f'configmap-{configmap_name.replace("_", "-")}'
                    if key:
                        volume_name += f'-{key.replace(".", "-").replace("_", "-")}'

                    # Add volume mount
                    volume_mount = V1VolumeMount(
                        name=volume_name,
                        mount_path=mount_path,
                        read_only=True if mount_mode == 'ro' else None,
                    )
                    if sub_path:
                        volume_mount.sub_path = sub_path

                    volume_mounts.append(volume_mount)

                    # Add volume
                    volumes.append(
                        V1Volume(
                            name=volume_name,
                            config_map=V1ConfigMapVolumeSource(
                                name=configmap_name
                            ),
                        )
                    )

                    if sub_path:
                        logger.debug(f'ConfigMap volume mount: {configmap_name}:{key} to {mount_path} (subPath)')
                    else:
                        logger.debug(f'ConfigMap volume mount: {configmap_name} to {mount_path} (directory)')
                else:
                    logger.warning(f'Invalid configmap volume format: {mount}. Expected format: "configmap_name:mount_path[:mode]" or "configmap_name:key:mount_path[:mode]"')

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
            f'Closing runtime and cleaning up resources for conversation ID: {self.sid}',
        )
        # Call parent class close method first
        super().close()

        # Close log streamer if it exists
        if self.log_streamer:
            self.log_streamer.close()

        # When Redis coordination is enabled, we should NOT clean up the Kubernetes pod
        # immediately because other replicas might want to attach to it.
        # Only clean up the local state and Redis coordination state.
        if (self._coordinator and self._coordinator.enabled and
            getattr(self._k8s_config, 'redis_coordination_enabled', True) and
            getattr(self._k8s_config, 'redis_coordination_keep_pod_alive', True)):
            # Unregister this replica from the pod connections
            try:
                pod_key = f"k8s-pod:{self._k8s_namespace}:{self.pod_name}"
                call_sync_from_async(self._unregister_replica_connection)(pod_key)

                # Check if there are any other active replicas connected
                active_replicas = call_sync_from_async(self._get_active_replica_count)(pod_key)

                if active_replicas == 0:
                    # No other replicas connected - schedule delayed cleanup instead of immediate cleanup
                    delay_seconds = getattr(self._k8s_config, 'websocket_disconnect_delay', 3600)
                    self.log('info', f'No other replicas connected to pod {self.pod_name}, scheduling cleanup in {delay_seconds}s')
                    self._schedule_delayed_cleanup(delay_seconds)
                else:
                    self.log('info', f'Replica disconnecting from pod {self.pod_name}, but {active_replicas} other replicas still connected')

            except Exception as e:
                self.log('warning', f'Failed to update Redis state during close: {e}')

            # Don't perform legacy cleanup when coordination is enabled
            return

        # Legacy behavior: clean up resources immediately if coordination is disabled
        # Return early if we should keep the runtime alive or if we're attaching to existing
        if self.config.sandbox.keep_runtime_alive or self.attach_to_existing:
            self.log(
                'info', 'Keeping runtime alive due to configuration or attach mode'
            )
            return

        # Schedule delayed cleanup if Redis coordination is disabled
        delay_seconds = getattr(self._k8s_config, 'websocket_disconnect_delay', 3600)
        self.log('info', f'Redis coordination disabled, scheduling cleanup in {delay_seconds}s')
        self._schedule_delayed_cleanup(delay_seconds)

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
            # Clean up Redis state if coordinator is available
            coordinator = await get_coordinator()
            if coordinator and coordinator.enabled:
                try:
                    pod_name = cls._get_pod_name(conversation_id)
                    pod_key = f"k8s-pod:{cls._namespace}:{pod_name}"
                    await coordinator.delete_resource_state(pod_key)
                    logger.debug(f'Cleaned up Redis state for conversation {conversation_id}')
                except Exception as e:
                    logger.warning(f'Failed to clean up Redis state for conversation {conversation_id}: {e}')

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

    def _log_coordination_metrics(self, action: str, pod_key: str, state: dict | None = None):
        """
        Log coordination metrics for monitoring and debugging.

        Args:
            action: The action being performed (e.g., 'attach_success', 'create_start', 'lock_acquired')
            pod_key: The Redis key for the pod
            state: Optional state information
        """
        metrics = {
            'action': action,
            'pod_name': self.pod_name,
            'namespace': self._k8s_namespace,
            'replica_id': f"{os.getpid()}-{id(self)}",
            'timestamp': time.time(),
        }

        if state:
            metrics['redis_state'] = state

        # Log as structured data for easy parsing by monitoring systems
        self.log('info', f'COORDINATION_METRIC: {metrics}')

    async def _get_coordination_status(self) -> dict:
        """
        Get comprehensive coordination status for debugging.

        Returns:
            Dictionary with coordination status information
        """
        pod_key = f"k8s-pod:{self._k8s_namespace}:{self.pod_name}"
        status = {
            'pod_name': self.pod_name,
            'namespace': self._k8s_namespace,
            'replica_id': f"{os.getpid()}-{id(self)}",
            'coordinator_enabled': self._coordinator and self._coordinator.enabled,
            'redis_state': None,
            'kubernetes_state': None,
            'coordination_config': {
                'enabled': getattr(self._k8s_config, 'redis_coordination_enabled', True),
                'timeout': getattr(self._k8s_config, 'redis_coordination_timeout', 30),
                'retry_attempts': getattr(self._k8s_config, 'redis_coordination_retry_attempts', 3),
            }
        }

        # Get Redis state
        if self._coordinator and self._coordinator.enabled:
            try:
                status['redis_state'] = await self._get_pod_status_from_redis(pod_key)
            except Exception as e:
                status['redis_error'] = str(e)

        # Get Kubernetes state
        try:
            pod = self.k8s_client.read_namespaced_pod(
                name=self.pod_name, namespace=self._k8s_namespace
            )
            status['kubernetes_state'] = {
                'phase': pod.status.phase,
                'pod_ip': pod.status.pod_ip,
                'start_time': str(pod.status.start_time) if pod.status.start_time else None,
                'ready': (pod.status.conditions and any(
                    c.type == 'Ready' and c.status == 'True'
                    for c in pod.status.conditions
                )) if pod.status.conditions else False
            }
        except client.rest.ApiException as e:
            if e.status == 404:
                status['kubernetes_state'] = {'phase': 'NotFound'}
            else:
                status['kubernetes_error'] = str(e)
        except Exception as e:
            status['kubernetes_error'] = str(e)

        return status

    async def _create_or_attach_with_coordination(self, pod_key: str) -> bool:
        """
        Enhanced create-or-attach logic using Redis coordination.

        Args:
            pod_key: Redis key for coordinating this pod

        Returns:
            True if successful, False if coordination failed
        """
        if not self._coordinator or not self._coordinator.enabled:
            self.log('warning', 'Redis coordinator not available')
            return False

        # Use configuration settings for coordination behavior
        if not getattr(self._k8s_config, 'redis_coordination_enabled', True):
            self.log('info', 'Redis coordination disabled by configuration')
            return False

        max_attempts = getattr(self._k8s_config, 'redis_coordination_retry_attempts', 3)
        coordination_timeout = getattr(self._k8s_config, 'redis_coordination_timeout', 30)

        for attempt in range(max_attempts):
            self.log('info', f'Pod coordination attempt {attempt + 1}/{max_attempts}')

            # Step 1: Try to attach to existing pod
            try:
                await call_sync_from_async(self._attach_to_pod)
                self.log('info', f'Successfully attached to existing pod {self.pod_name}')
                self._log_coordination_metrics('attach_success', pod_key)
                # Register this replica as connected to the pod
                await self._register_replica_connection(pod_key)
                return True
            except client.rest.ApiException:
                # Pod doesn't exist or isn't ready, continue
                pass

            # Handle attach_to_existing mode
            if self.attach_to_existing:
                self.log('error', f'Pod {self.pod_name} not found or cannot connect to it.')
                raise AgentRuntimeDisconnectedError(f'Pod {self.pod_name} not found')

            # Step 2: Check if another replica is creating the pod
            existing_state = await self._coordinator.get_resource_state(pod_key)
            if existing_state:
                status = existing_state.get('status')
                timestamp = existing_state.get('timestamp', 0)
                age_seconds = time.time() - timestamp

                if status == 'creating' and age_seconds < 300:  # 5 minutes
                    self.log('info', f'Another replica is creating pod {self.pod_name} (age: {age_seconds:.1f}s), waiting...')
                    # Wait with exponential backoff
                    wait_time = min(2 ** attempt, 10)
                    await asyncio.sleep(wait_time)
                    continue
                elif status == 'created':
                    self.log('info', f'Pod {self.pod_name} was created by another replica, attempting to attach')
                    # Try to attach one more time
                    try:
                        await call_sync_from_async(self._attach_to_pod)
                        self.log('info', f'Successfully attached to pod created by another replica')
                        self._log_coordination_metrics('attach_success', pod_key)
                        return True
                    except client.rest.ApiException:
                        self.log('warning', f'Pod marked as created but cannot attach, clearing state')
                        await self._coordinator.delete_resource_state(pod_key)
                elif status == 'failed' or age_seconds > 300:
                    self.log('info', f'Previous creation attempt failed or timed out, clearing state')
                    await self._coordinator.delete_resource_state(pod_key)

            # Step 3: Try to acquire lock for creation
            if await self._try_create_pod_with_lock(pod_key, coordination_timeout):
                return True

            # If all attempts fail, wait before retry
            if attempt < max_attempts - 1:
                wait_time = min(2 ** attempt, 10)
                self.log('info', f'Creation attempt failed, waiting {wait_time}s before retry')
                await asyncio.sleep(wait_time)

        self.log('error', f'Failed to create or attach to pod after {max_attempts} attempts')
        return False

    async def _try_create_pod_with_lock(self, pod_key: str, coordination_timeout: int = 300) -> bool:
        """
        Try to create a pod with distributed locking.

        Args:
            pod_key: Redis key for coordinating this pod

        Returns:
            True if pod was successfully created
        """
        lock_acquired = False
        try:
            # Try to acquire lock with configurable timeout
            self.log('info', f'Attempting to acquire lock for pod creation: {pod_key}')
            lock_acquired = await self._coordinator.acquire_lock(pod_key, timeout=coordination_timeout)

            if not lock_acquired:
                self.log('info', f'Could not acquire lock for {pod_key}')
                return False

            # Double-check if pod exists now that we have the lock
            try:
                await call_sync_from_async(self._attach_to_pod)
                self.log('info', f'Pod {self.pod_name} was created while waiting for lock, attached successfully')
                self._log_coordination_metrics('attach_success', pod_key)
                return True
            except client.rest.ApiException:
                pass  # Pod still doesn't exist, proceed to create

            # Mark that we're creating the pod
            replica_id = f"{os.getpid()}-{id(self)}"  # More unique identifier
            await self._coordinator.set_resource_state(pod_key, {
                'status': 'creating',
                'replica_id': replica_id,
                'timestamp': time.time(),
                'pod_name': self.pod_name
            }, ttl=600)

            self.log('info', f'Starting runtime with image: {self.pod_image}')

            # Create the pod
            await call_sync_from_async(self._init_k8s_resources)

            # Wait for pod to become ready and healthy
            if not await self._wait_for_pod_with_timeout():
                raise RuntimeError(f'Pod {self.pod_name} failed to become ready within timeout')

            # Mark pod as created successfully
            await self._coordinator.set_resource_state(pod_key, {
                'status': 'created',
                'replica_id': replica_id,
                'timestamp': time.time(),
                'pod_name': self.pod_name
            }, ttl=3600)

            # Register this replica as connected to the pod
            await self._register_replica_connection(pod_key)

            self.log('info', f'Pod started: {self.pod_name}. VSCode URL: {self.vscode_url}')
            self._log_coordination_metrics('create_success', pod_key)
            return True

        except Exception as init_error:
            # Mark creation as failed
            try:
                await self._coordinator.set_resource_state(pod_key, {
                    'status': 'failed',
                    'replica_id': f"{os.getpid()}-{id(self)}",
                    'timestamp': time.time(),
                    'error': str(init_error)
                }, ttl=300)  # Shorter TTL for failed states
            except Exception:
                pass  # Don't fail on Redis errors during cleanup

            self.log('error', f'Failed to initialize k8s resources: {init_error}')
            raise AgentRuntimeNotFoundError(
                f'Failed to initialize kubernetes resources: {init_error}'
            ) from init_error

        finally:
            if lock_acquired:
                await self._coordinator.release_lock(pod_key)
                self.log('debug', f'Released lock for {pod_key}')

        return False

    async def _fallback_pod_creation(self):
        """
        Fallback pod creation when Redis coordination is not available.
        This implements a simple retry mechanism with exponential backoff.
        """
        self.log('warning', 'Using fallback pod creation without Redis coordination')

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                # Try to attach to existing pod first
                try:
                    await call_sync_from_async(self._attach_to_pod)
                    self.log('info', f'Successfully attached to existing pod {self.pod_name}')
                    break
                except client.rest.ApiException:
                    if self.attach_to_existing:
                        self.log('error', f'Pod {self.pod_name} not found or cannot connect to it.')
                        raise AgentRuntimeDisconnectedError(f'Pod {self.pod_name} not found')

                # Try to create the pod
                self.log('info', f'Creating pod {self.pod_name} (attempt {attempt + 1}/{max_attempts})')
                await call_sync_from_async(self._init_k8s_resources)
                self.log('info', f'Pod started: {self.pod_name}. VSCode URL: {self.vscode_url}')
                break

            except client.rest.ApiException as e:
                if 'AlreadyExists' in str(e):
                    # Another replica created the pod, try to attach
                    self.log('info', 'Pod already exists, attempting to attach')
                    try:
                        await call_sync_from_async(self._attach_to_pod)
                        self.log('info', f'Successfully attached to existing pod {self.pod_name}')
                        break
                    except client.rest.ApiException:
                        if attempt < max_attempts - 1:
                            wait_time = 2 ** attempt
                            self.log('info', f'Attach failed, retrying in {wait_time}s')
                            await asyncio.sleep(wait_time)
                        else:
                            raise
                else:
                    # Other error, propagate
                    raise
            except Exception as e:
                if attempt < max_attempts - 1:
                    wait_time = 2 ** attempt
                    self.log('warning', f'Pod creation failed: {e}, retrying in {wait_time}s')
                    await asyncio.sleep(wait_time)
                else:
                    raise

    async def _get_pod_status_from_redis(self, pod_key: str) -> dict | None:
        """
        Get pod status from Redis with additional metadata.

        Args:
            pod_key: Redis key for the pod state

        Returns:
            Dictionary with pod status and metadata, or None if not found
        """
        if not self._coordinator or not self._coordinator.enabled:
            return None

        try:
            state = await self._coordinator.get_resource_state(pod_key)
            if state:
                # Add computed fields
                timestamp = state.get('timestamp', 0)
                state['age_seconds'] = time.time() - timestamp if timestamp else 0
                state['age_human'] = self._format_duration(state['age_seconds'])

            return state
        except Exception as e:
            self.log('warning', f'Failed to get pod status from Redis: {e}')
            return None

    def _format_duration(self, seconds: float) -> str:
        """Format duration in human-readable format."""
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            return f"{seconds/60:.1f}m"
        else:
            return f"{seconds/3600:.1f}h"

    async def _cleanup_stale_redis_states(self):
        """
        Clean up stale Redis states for pods that no longer exist.
        This should be called periodically or during startup.
        """
        if not self._coordinator or not self._coordinator.enabled:
            return

        try:
            # This is a simplified cleanup - in a production system you might
            # want to scan for all pod keys and clean up stale ones
            pod_key = f"k8s-pod:{self._k8s_namespace}:{self.pod_name}"
            state = await self._get_pod_status_from_redis(pod_key)

            if state and state.get('age_seconds', 0) > 3600:  # 1 hour
                # Check if pod actually exists
                try:
                    await call_sync_from_async(lambda: self.k8s_client.read_namespaced_pod(
                        name=self.pod_name, namespace=self._k8s_namespace
                    ))
                except client.rest.ApiException as e:
                    if e.status == 404:
                        # Pod doesn't exist, clean up Redis state
                        await self._coordinator.delete_resource_state(pod_key)
                        self.log('info', f'Cleaned up stale Redis state for non-existent pod {self.pod_name}')

        except Exception as e:
            self.log('warning', f'Error during Redis cleanup: {e}')

    async def _verify_pod_health(self) -> bool:
        """
        Verify that the pod is healthy and ready to accept connections.

        Returns:
            True if pod is healthy, False otherwise
        """
        try:
            # First check Kubernetes pod status
            pod = self.k8s_client.read_namespaced_pod(
                name=self.pod_name, namespace=self._k8s_namespace
            )

            if pod.status.phase != 'Running':
                self.log('debug', f'Pod {self.pod_name} not in Running state: {pod.status.phase}')
                return False

            # Check if all containers are ready
            if pod.status.container_statuses:
                for status in pod.status.container_statuses:
                    if not status.ready:
                        self.log('debug', f'Container {status.name} not ready')
                        return False

            # Check if pod conditions indicate readiness
            if pod.status.conditions:
                ready_condition = next(
                    (c for c in pod.status.conditions if c.type == 'Ready'),
                    None
                )
                if not ready_condition or ready_condition.status != 'True':
                    self.log('debug', f'Pod ready condition not met')
                    return False

            # Additional health check: try to connect to the runtime API
            try:
                # This will timeout quickly if the service isn't ready
                response = await call_sync_from_async(lambda: self._check_runtime_health())
                if response:
                    self.log('debug', f'Pod {self.pod_name} health check passed')
                    return True
                else:
                    self.log('debug', f'Pod {self.pod_name} health check failed')
                    return False
            except Exception as e:
                self.log('debug', f'Pod {self.pod_name} health check error: {e}')
                return False

        except Exception as e:
            self.log('warning', f'Error verifying pod health: {e}')
            return False

    async def _wait_for_pod_with_timeout(self, timeout_seconds: int = 180) -> bool:
        """
        Wait for pod to become ready with configurable timeout.

        Args:
            timeout_seconds: Maximum time to wait for pod readiness

        Returns:
            True if pod becomes ready, False if timeout
        """
        start_time = time.time()
        last_log_time = 0

        while time.time() - start_time < timeout_seconds:
            try:
                if await self._verify_pod_health():
                    self.log('info', f'Pod {self.pod_name} is ready and healthy')
                    return True

                # Log progress every 10 seconds
                current_time = time.time()
                if current_time - last_log_time > 10:
                    elapsed = current_time - start_time
                    self.log('info', f'Waiting for pod {self.pod_name} to become ready... ({elapsed:.1f}s/{timeout_seconds}s)')
                    last_log_time = current_time

                await asyncio.sleep(2)

            except Exception as e:
                self.log('warning', f'Error while waiting for pod: {e}')
                await asyncio.sleep(5)

        self.log('error', f'Timeout waiting for pod {self.pod_name} to become ready after {timeout_seconds}s')
        return False

    async def _startup_cleanup(self):
        """
        Perform startup cleanup to handle any stale states from previous runs.
        This should be called early in the connect process.
        """
        if not self._coordinator or not self._coordinator.enabled:
            return

        try:
            pod_key = f"k8s-pod:{self._k8s_namespace}:{self.pod_name}"
            state = await self._get_pod_status_from_redis(pod_key)

            if state:
                self.log('info', f'Found existing Redis state for pod {self.pod_name}: {state.get("status")} (age: {state.get("age_human", "unknown")})')

                # Check if the pod actually exists in Kubernetes
                try:
                    pod = self.k8s_client.read_namespaced_pod(
                        name=self.pod_name, namespace=self._k8s_namespace
                    )

                    # Pod exists, check if state is consistent
                    if state.get('status') == 'creating' and pod.status.phase == 'Running':
                        # Pod was created but state wasn't updated
                        self.log('info', f'Updating stale "creating" state to "created" for running pod')
                        await self._coordinator.set_resource_state(pod_key, {
                            'status': 'created',
                            'replica_id': f"{os.getpid()}-{id(self)}",
                            'timestamp': time.time(),
                            'pod_name': self.pod_name
                        }, ttl=3600)
                    elif state.get('status') == 'failed':
                        # Pod exists but marked as failed, clear the state
                        self.log('info', f'Clearing "failed" state for existing pod')
                        await self._coordinator.delete_resource_state(pod_key)

                except client.rest.ApiException as e:
                    if e.status == 404:
                        # Pod doesn't exist but we have Redis state, clean it up
                        self.log('info', f'Cleaning up Redis state for non-existent pod {self.pod_name}')
                        await self._coordinator.delete_resource_state(pod_key)
                    else:
                        self.log('warning', f'Error checking pod existence during cleanup: {e}')

        except Exception as e:
            self.log('warning', f'Error during startup cleanup: {e}')

    async def _should_cleanup_pod(self, pod_key: str) -> bool:
        """
        Determine if a pod should be cleaned up based on various criteria.

        Args:
            pod_key: Redis key for the pod state

        Returns:
            True if pod should be cleaned up, False otherwise
        """
        if not self._coordinator or not self._coordinator.enabled:
            return False

        try:
            state = await self._get_pod_status_from_redis(pod_key)
            if not state:
                # No Redis state, check if pod exists in Kubernetes
                try:
                    self.k8s_client.read_namespaced_pod(
                        name=self.pod_name, namespace=self._k8s_namespace
                    )
                    # Pod exists but no Redis state - this might be an orphaned pod
                    # For safety, don't clean it up automatically
                    return False
                except client.rest.ApiException as e:
                    if e.status == 404:
                        # Pod doesn't exist, no cleanup needed
                        return False
                    return False

            # Check pod age - clean up very old pods (configurable threshold)
            age_threshold = getattr(self._k8s_config, 'pod_cleanup_age_threshold', 3600)  # 1 hour default
            if state.get('age_seconds', 0) > age_threshold:
                self.log('info', f'Pod {self.pod_name} is older than {age_threshold}s, marking for cleanup')
                return True

            # Check if pod is in failed state for too long
            if state.get('status') == 'failed' and state.get('age_seconds', 0) > 300:  # 5 minutes
                self.log('info', f'Pod {self.pod_name} has been in failed state for too long, marking for cleanup')
                return True

            return False

        except Exception as e:
            self.log('warning', f'Error checking if pod should be cleaned up: {e}')
            return False

    async def _cleanup_old_pods_if_needed(self):
        """
        Clean up old or stale pods if coordination is enabled and cleanup is configured.
        This should be called periodically or during startup.
        """
        if not (self._coordinator and self._coordinator.enabled and
                getattr(self._k8s_config, 'redis_coordination_enabled', True)):
            return

        # Only perform cleanup if explicitly enabled
        if not getattr(self._k8s_config, 'redis_coordination_auto_cleanup', False):
            return

        try:
            pod_key = f"k8s-pod:{self._k8s_namespace}:{self.pod_name}"

            if await self._should_cleanup_pod(pod_key):
                self.log('info', f'Auto-cleaning up old pod {self.pod_name}')

                # Clean up both Redis state and Kubernetes resources
                await self._coordinator.delete_resource_state(pod_key)

                try:
                    self._cleanup_k8s_resources(
                        namespace=self._k8s_namespace,
                        remove_pvc=True,
                        conversation_id=self.sid,
                    )
                    self.log('info', f'Successfully cleaned up old pod {self.pod_name}')
                except Exception as e:
                    self.log('warning', f'Error cleaning up old pod resources: {e}')

        except Exception as e:
            self.log('warning', f'Error during automatic pod cleanup: {e}')

    async def _register_replica_connection(self, pod_key: str):
        """
        Register this replica as connected to the pod.

        Args:
            pod_key: Redis key for the pod state
        """
        if not self._coordinator or not self._coordinator.enabled:
            return

        try:
            replica_id = f"{os.getpid()}-{id(self)}"
            connection_key = f"{pod_key}:connections"

            # Add this replica to the set of connected replicas
            await self._coordinator.redis_client.sadd(connection_key, replica_id)
            await self._coordinator.redis_client.expire(connection_key, 7200)  # 2 hours TTL

            # Also set a heartbeat for this replica
            heartbeat_key = f"{pod_key}:heartbeat:{replica_id}"
            await self._coordinator.redis_client.setex(heartbeat_key, 300, time.time())  # 5 minute TTL

            self.log('debug', f'Registered replica {replica_id} connection to pod {self.pod_name}')

        except Exception as e:
            self.log('warning', f'Failed to register replica connection: {e}')

    async def _unregister_replica_connection(self, pod_key: str):
        """
        Unregister this replica from the pod connections.

        Args:
            pod_key: Redis key for the pod state
        """
        if not self._coordinator or not self._coordinator.enabled:
            return

        try:
            replica_id = f"{os.getpid()}-{id(self)}"
            connection_key = f"{pod_key}:connections"
            heartbeat_key = f"{pod_key}:heartbeat:{replica_id}"

            # Remove this replica from the set of connected replicas
            await self._coordinator.redis_client.srem(connection_key, replica_id)
            await self._coordinator.redis_client.delete(heartbeat_key)

            self.log('debug', f'Unregistered replica {replica_id} connection from pod {self.pod_name}')

        except Exception as e:
            self.log('warning', f'Failed to unregister replica connection: {e}')

    async def _get_active_replica_count(self, pod_key: str) -> int:
        """
        Get the number of active replicas connected to the pod.

        Args:
            pod_key: Redis key for the pod state

        Returns:
            Number of active replicas
        """
        if not self._coordinator or not self._coordinator.enabled:
            return 0

        try:
            connection_key = f"{pod_key}:connections"

            # Get all connected replicas
            replicas = await self._coordinator.redis_client.smembers(connection_key)
            if not replicas:
                return 0

            # Check which replicas have recent heartbeats
            active_count = 0
            for replica_id in replicas:
                heartbeat_key = f"{pod_key}:heartbeat:{replica_id}"
                heartbeat = await self._coordinator.redis_client.get(heartbeat_key)
                if heartbeat:
                    # Check if heartbeat is recent (within last 10 minutes)
                    try:
                        heartbeat_time = float(heartbeat)
                        if time.time() - heartbeat_time < 600:  # 10 minutes
                            active_count += 1
                        else:
                            # Remove stale replica from connections
                            await self._coordinator.redis_client.srem(connection_key, replica_id)
                    except (ValueError, TypeError):
                        # Invalid heartbeat, remove replica
                        await self._coordinator.redis_client.srem(connection_key, replica_id)
                else:
                    # No heartbeat, remove replica from connections
                    await self._coordinator.redis_client.srem(connection_key, replica_id)

            return active_count

        except Exception as e:
            self.log('warning', f'Failed to get active replica count: {e}')
            return 0

    async def _maintain_replica_heartbeat(self, pod_key: str):
        """
        Maintain heartbeat for this replica while it's connected to the pod.
        Should be called periodically to keep the connection active.

        Args:
            pod_key: Redis key for the pod state
        """
        if not self._coordinator or not self._coordinator.enabled:
            return

        try:
            replica_id = f"{os.getpid()}-{id(self)}"
            heartbeat_key = f"{pod_key}:heartbeat:{replica_id}"

            # Update heartbeat timestamp
            await self._coordinator.redis_client.setex(heartbeat_key, 300, time.time())  # 5 minute TTL

        except Exception as e:
            self.log('warning', f'Failed to maintain replica heartbeat: {e}')

    def _schedule_delayed_cleanup(self, delay_seconds: int):
        """
        Schedule delayed cleanup of Kubernetes resources after WebSocket disconnection.

        Args:
            delay_seconds: Number of seconds to wait before cleanup
        """
        global _delayed_cleanup_tasks

        # Cancel any existing delayed cleanup for this pod
        self._cancel_delayed_cleanup()

        # Schedule new delayed cleanup
        pod_key = self.pod_name
        self.log('info', f'Scheduling delayed cleanup for pod {self.pod_name} in {delay_seconds} seconds')

        # Create async task for delayed cleanup
        task = asyncio.create_task(self._execute_delayed_cleanup(delay_seconds))
        _delayed_cleanup_tasks[pod_key] = task

    def _cancel_delayed_cleanup(self):
        """
        Cancel any pending delayed cleanup for this pod.
        """
        global _delayed_cleanup_tasks

        pod_key = self.pod_name
        if pod_key in _delayed_cleanup_tasks:
            task = _delayed_cleanup_tasks[pod_key]
            if not task.done():
                task.cancel()
                self.log('info', f'Cancelled delayed cleanup for pod {self.pod_name}')
            del _delayed_cleanup_tasks[pod_key]

    async def _execute_delayed_cleanup(self, delay_seconds: int):
        """
        Execute delayed cleanup after waiting for the specified delay.

        Args:
            delay_seconds: Number of seconds to wait before cleanup
        """
        global _delayed_cleanup_tasks

        try:
            # Wait for the specified delay
            await asyncio.sleep(delay_seconds)

            # Check if there are still no active replicas after the delay
            if self._coordinator and self._coordinator.enabled:
                pod_key = f"k8s-pod:{self._k8s_namespace}:{self.pod_name}"
                active_replicas = await self._get_active_replica_count(pod_key)

                if active_replicas > 0:
                    self.log('info', f'Pod {self.pod_name} has {active_replicas} active replicas, skipping cleanup')
                    return

                # Clean up Redis state
                await self._coordinator.delete_resource_state(pod_key)

            # Clean up Kubernetes resources
            self.log('info', f'Executing delayed cleanup for pod {self.pod_name} after {delay_seconds}s delay')
            try:
                self._cleanup_k8s_resources(
                    namespace=self._k8s_namespace,
                    remove_pvc=True,
                    conversation_id=self.sid,
                )

                if self._shutdown_listener_id:
                    remove_shutdown_listener(self._shutdown_listener_id)
                    self._shutdown_listener_id = None # Clear the ID after removal

                self.log('info', f'Successfully completed delayed cleanup for pod {self.pod_name}')
            except Exception as e:
                self.log('error', f'Error during delayed cleanup for pod {self.pod_name}: {e}')

        except asyncio.CancelledError:
            self.log('info', f'Delayed cleanup for pod {self.pod_name} was cancelled')
            raise
        except Exception as e:
            self.log('error', f'Unexpected error during delayed cleanup for pod {self.pod_name}: {e}')
        finally:
            # Remove task from global tracking
            if self.pod_name in _delayed_cleanup_tasks:
                del _delayed_cleanup_tasks[self.pod_name]

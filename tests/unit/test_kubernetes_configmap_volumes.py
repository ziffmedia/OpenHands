import pytest
from unittest.mock import MagicMock, patch
from kubernetes.client.models import V1ConfigMapVolumeSource, V1Volume, V1VolumeMount

from openhands.core.config.kubernetes_config import KubernetesConfig
from openhands.core.config.sandbox_config import SandboxConfig
from openhands.core.config import OpenHandsConfig


def test_configmap_volumes_processing():
    """Test that configmap volumes are correctly processed in the Kubernetes runtime."""
    from openhands.runtime.impl.kubernetes.kubernetes_runtime import KubernetesRuntime

    # Create mock config with configmap volumes
    k8s_config = KubernetesConfig(
        configmap_volumes='ca-certs:/etc/ssl/certs/ca-certificates.crt:ro,my-config:/app/config'
    )
    sandbox_config = SandboxConfig(kubernetes=k8s_config)
    config = OpenHandsConfig(sandbox=sandbox_config)

    # Mock the runtime initialization to avoid actual k8s calls
    with patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.config.load_incluster_config'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.config.load_kube_config'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.client.CoreV1Api'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.client.NetworkingV1Api'):

        runtime = KubernetesRuntime.__new__(KubernetesRuntime)
        runtime.config = config
        runtime._k8s_config = k8s_config
        runtime.pod_name = 'test-pod'
        runtime.initial_env_vars = {}
        runtime._container_port = 8000
        runtime.vscode_enabled = False
        runtime._app_ports = []
        runtime.plugins = []
        runtime.sid = 'test-session'

        # Mock the pod creation method partially to test volume processing
        with patch.object(runtime, '_get_pvc_name', return_value='test-pvc'):
            pod_manifest = runtime._get_pod_manifest()

            # Check that configmap volumes were added
            volumes = pod_manifest.spec.volumes
            volume_mounts = pod_manifest.spec.containers[0].volume_mounts

            # Should have workspace volume + 2 configmap volumes = 3 total
            assert len(volumes) == 3
            assert len(volume_mounts) == 3

            # Check ca-certs configmap volume
            ca_certs_volume = next((v for v in volumes if v.name == 'configmap-ca-certs'), None)
            assert ca_certs_volume is not None
            assert ca_certs_volume.config_map.name == 'ca-certs'

            # Check ca-certs volume mount
            ca_certs_mount = next((vm for vm in volume_mounts if vm.name == 'configmap-ca-certs'), None)
            assert ca_certs_mount is not None
            assert ca_certs_mount.mount_path == '/etc/ssl/certs/ca-certificates.crt'
            assert ca_certs_mount.read_only is True

            # Check my-config configmap volume
            my_config_volume = next((v for v in volumes if v.name == 'configmap-my-config'), None)
            assert my_config_volume is not None
            assert my_config_volume.config_map.name == 'my-config'

            # Check my-config volume mount
            my_config_mount = next((vm for vm in volume_mounts if vm.name == 'configmap-my-config'), None)
            assert my_config_mount is not None
            assert my_config_mount.mount_path == '/app/config'
            assert my_config_mount.read_only is None  # no mode specified, so default


def test_configmap_volumes_invalid_format():
    """Test that invalid configmap volume formats are handled gracefully."""
    from openhands.runtime.impl.kubernetes.kubernetes_runtime import KubernetesRuntime

    # Create mock config with invalid configmap volume format
    k8s_config = KubernetesConfig(
        configmap_volumes='invalid-format,also-invalid:,valid-config:/valid/path'
    )
    sandbox_config = SandboxConfig(kubernetes=k8s_config)
    config = OpenHandsConfig(sandbox=sandbox_config)

    # Mock the runtime initialization
    with patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.config.load_incluster_config'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.config.load_kube_config'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.client.CoreV1Api'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.client.NetworkingV1Api'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.logger') as mock_logger:

        runtime = KubernetesRuntime.__new__(KubernetesRuntime)
        runtime.config = config
        runtime._k8s_config = k8s_config
        runtime.pod_name = 'test-pod'
        runtime.initial_env_vars = {}
        runtime._container_port = 8000
        runtime.vscode_enabled = False
        runtime._app_ports = []
        runtime.plugins = []
        runtime.sid = 'test-session'

        with patch.object(runtime, '_get_pvc_name', return_value='test-pvc'):
            pod_manifest = runtime._get_pod_manifest()

            # Should have workspace volume + 1 valid configmap volume = 2 total
            volumes = pod_manifest.spec.volumes
            volume_mounts = pod_manifest.spec.containers[0].volume_mounts

            assert len(volumes) == 2  # workspace + valid-config
            assert len(volume_mounts) == 2

            # Check that warnings were logged for invalid formats
            mock_logger.warning.assert_called()
            warning_calls = [call for call in mock_logger.warning.call_args_list
                           if 'Invalid configmap volume format' in str(call)]
            assert len(warning_calls) == 2  # Two invalid formats


def test_no_configmap_volumes():
    """Test that when no configmap volumes are specified, only workspace volume is created."""
    from openhands.runtime.impl.kubernetes.kubernetes_runtime import KubernetesRuntime

    # Create config without configmap volumes
    k8s_config = KubernetesConfig()
    sandbox_config = SandboxConfig(kubernetes=k8s_config)
    config = OpenHandsConfig(sandbox=sandbox_config)

    with patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.config.load_incluster_config'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.config.load_kube_config'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.client.CoreV1Api'), \
         patch('openhands.runtime.impl.kubernetes.kubernetes_runtime.client.NetworkingV1Api'):

        runtime = KubernetesRuntime.__new__(KubernetesRuntime)
        runtime.config = config
        runtime._k8s_config = k8s_config
        runtime.pod_name = 'test-pod'
        runtime.initial_env_vars = {}
        runtime._container_port = 8000
        runtime.vscode_enabled = False
        runtime._app_ports = []
        runtime.plugins = []
        runtime.sid = 'test-session'

        with patch.object(runtime, '_get_pvc_name', return_value='test-pvc'):
            pod_manifest = runtime._get_pod_manifest()

            # Should have only workspace volume
            volumes = pod_manifest.spec.volumes
            volume_mounts = pod_manifest.spec.containers[0].volume_mounts

            assert len(volumes) == 1
            assert len(volume_mounts) == 1
            assert volumes[0].name == 'workspace-volume'
            assert volume_mounts[0].name == 'workspace-volume'

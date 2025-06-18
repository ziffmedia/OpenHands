import pytest
from pydantic import ValidationError

from openhands.core.config.kubernetes_config import KubernetesConfig
from openhands.core.config.sandbox_config import SandboxConfig


def test_kubernetes_config_defaults():
    """Test that KubernetesConfig has correct default values."""
    config = KubernetesConfig()
    assert config.namespace == 'default'
    assert config.ingress_domain == 'localhost'
    assert config.pvc_storage_size == '2Gi'
    assert config.pvc_storage_class is None
    assert config.resource_cpu_request == '1'
    assert config.resource_memory_request == '1Gi'
    assert config.resource_memory_limit == '2Gi'
    assert config.image_pull_secret is None
    assert config.ingress_tls_secret is None
    assert config.node_selector_key is None
    assert config.node_selector_val is None
    assert config.tolerations_yaml is None
    assert config.privileged is False
    assert config.annotations == {}


def test_kubernetes_config_custom_values():
    """Test that KubernetesConfig accepts custom values."""
    config = KubernetesConfig(
        namespace='test-ns',
        ingress_domain='test.example.com',
        pvc_storage_size='5Gi',
        pvc_storage_class='fast',
        resource_cpu_request='2',
        resource_memory_request='2Gi',
        resource_memory_limit='4Gi',
        image_pull_secret='pull-secret',
        ingress_tls_secret='tls-secret',
        node_selector_key='zone',
        node_selector_val='us-east-1',
        tolerations_yaml='- key: special\n  value: true',
        privileged=True,
    )

    assert config.namespace == 'test-ns'
    assert config.ingress_domain == 'test.example.com'
    assert config.pvc_storage_size == '5Gi'
    assert config.pvc_storage_class == 'fast'
    assert config.resource_cpu_request == '2'
    assert config.resource_memory_request == '2Gi'
    assert config.resource_memory_limit == '4Gi'
    assert config.image_pull_secret == 'pull-secret'
    assert config.ingress_tls_secret == 'tls-secret'
    assert config.node_selector_key == 'zone'
    assert config.node_selector_val == 'us-east-1'
    assert config.tolerations_yaml == '- key: special\n  value: true'
    assert config.privileged is True


def test_kubernetes_config_in_sandbox_config():
    """Test that KubernetesConfig works correctly when used in SandboxConfig."""
    k8s_config = KubernetesConfig(namespace='test-ns')
    sandbox_config = SandboxConfig(kubernetes=k8s_config)

    assert sandbox_config.kubernetes is not None
    assert sandbox_config.kubernetes.namespace == 'test-ns'
    assert sandbox_config.kubernetes.ingress_domain == 'localhost'  # default value


def test_kubernetes_config_validation():
    """Test that KubernetesConfig validates input correctly."""
    # Test that extra fields are not allowed
    with pytest.raises(ValidationError):
        KubernetesConfig(extra_field='not allowed')


def test_kubernetes_config_annotations():
    """Test that KubernetesConfig properly handles annotations."""
    # Test empty annotations (default)
    config = KubernetesConfig()
    assert config.annotations == {}

    # Test custom annotations
    annotations = {
        'monitoring.io/scrape': 'true',
        'app.kubernetes.io/version': '1.0.0',
        'custom.annotation/value': 'test-value'
    }
    config = KubernetesConfig(annotations=annotations)
    assert config.annotations == annotations

    # Test annotations in SandboxConfig
    k8s_config = KubernetesConfig(annotations={'test.io/label': 'value'})
    sandbox_config = SandboxConfig(kubernetes=k8s_config)
    assert sandbox_config.kubernetes.annotations == {'test.io/label': 'value'}


def test_kubernetes_config_configmap_volumes():
    """Test that KubernetesConfig properly handles configmap volumes."""
    # Test default (no configmap volumes)
    config = KubernetesConfig()
    assert config.configmap_volumes is None

    # Test single configmap volume with key (subPath)
    config = KubernetesConfig(configmap_volumes='ca-certs:ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro')
    assert config.configmap_volumes == 'ca-certs:ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro'

    # Test single configmap volume as directory
    config = KubernetesConfig(configmap_volumes='my-config:/app/config')
    assert config.configmap_volumes == 'my-config:/app/config'

    # Test multiple configmap volumes
    multiple_volumes = 'ca-certs:ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro,my-config:/app/config'
    config = KubernetesConfig(configmap_volumes=multiple_volumes)
    assert config.configmap_volumes == multiple_volumes

    # Test configmap volumes in SandboxConfig
    k8s_config = KubernetesConfig(configmap_volumes='test-cm:test-key:/test/path')
    sandbox_config = SandboxConfig(kubernetes=k8s_config)
    assert sandbox_config.kubernetes.configmap_volumes == 'test-cm:test-key:/test/path'

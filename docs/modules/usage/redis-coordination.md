# Redis Coordination for Kubernetes Runtime

## Overview

The OpenHands Kubernetes runtime now includes Redis-based coordination to prevent race conditions when running multiple replicas of the OpenHands application. This feature ensures that only one replica creates a Kubernetes pod for each user session, while other replicas automatically attach to the existing pod.

## Problem Statement

When running OpenHands with multiple replicas (for high availability or load distribution), each replica would independently try to create and manage Kubernetes pods for user sessions. This led to:

- **Race conditions**: Multiple replicas trying to create the same pod simultaneously
- **Resource conflicts**: Competing for the same pod names and services
- **503 errors**: Failed readiness probes due to conflicting pod states
- **Inconsistent behavior**: Users experiencing different behavior depending on which replica handled their request

## Solution

The Redis coordination system implements a distributed locking mechanism that ensures:

1. **Atomic pod creation**: Only one replica can create a pod at a time
2. **Automatic attachment**: Other replicas automatically attach to existing pods
3. **State tracking**: Comprehensive tracking of pod lifecycle states
4. **Graceful degradation**: Falls back to legacy behavior when Redis is unavailable
5. **Cleanup mechanisms**: Automatic cleanup of stale states

## Architecture

### Coordination Flow

```
Replica 1              Redis                 Replica 2              Kubernetes
    |                    |                       |                      |
    |-- Try attach ------|                       |-- Try attach --------|
    |    (pod not found) |                       |    (pod not found)   |
    |                    |                       |                      |
    |-- Acquire lock --->|                       |-- Try acquire lock ->|
    |<-- Lock acquired --|                       |<-- Lock denied ------|
    |                    |                       |                      |
    |-- Set creating --->|                       |-- Wait for state --->|
    |                    |                       |<-- Status: creating -|
    |-- Create pod ------|---------------------> |                      |
    |                    |                       |                      |
    |-- Set created ---->|                       |-- Get state -------->|
    |-- Release lock --->|                       |<-- Status: created --|
    |                    |                       |                      |
    |                    |                       |-- Try attach ------->|
    |                    |                       |<-- Success ----------|
```

### State Management

The system tracks pod states in Redis with the following lifecycle:

- **creating**: Pod creation is in progress by one replica
- **created**: Pod successfully created and verified as healthy
- **failed**: Pod creation failed (temporary state, cleaned up automatically)
- **NotFound**: No Redis state exists (initial state)

### Key Components

1. **RedisCoordinator**: Core coordination logic (`openhands/utils/redis_coordination.py`)
2. **Enhanced KubernetesRuntime**: Updated runtime with coordination logic
3. **Configuration**: New configuration options for coordination behavior
4. **Health Checks**: Pod health verification before marking as created
5. **Cleanup Mechanisms**: Automatic cleanup of stale states

## Configuration

### Enable Coordination

Redis coordination is enabled by default when Redis is available. Configure in `config.toml`:

```toml
[sandbox.kubernetes]
# Enable Redis coordination (default: true)
redis_coordination_enabled = true

# Lock timeout in seconds (default: 30)
redis_coordination_timeout = 30

# Number of retry attempts (default: 3)
redis_coordination_retry_attempts = 3
```

### Redis Setup

Ensure Redis is configured for your deployment:

```toml
# Environment variables
REDIS_HOST=redis-server:6379
REDIS_PASSWORD=your-password
```

Or using Kubernetes service discovery:
```bash
kubectl create service clusterip redis --tcp=6379:6379
```

## Usage

### Multi-Replica Deployment

1. **Deploy Redis**: Ensure Redis is available to all replicas
2. **Scale replicas**: Scale your OpenHands deployment to multiple replicas
3. **Monitor logs**: Check coordination metrics in application logs

Example Kubernetes deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: openhands
spec:
  replicas: 3  # Multiple replicas
  selector:
    matchLabels:
      app: openhands
  template:
    metadata:
      labels:
        app: openhands
    spec:
      containers:
      - name: openhands
        image: openhands:latest
        env:
        - name: REDIS_HOST
          value: "redis:6379"
        - name: RUNTIME
          value: "kubernetes"
```

### Monitoring

The system provides comprehensive logging and metrics:

#### Coordination Metrics

```
COORDINATION_METRIC: {
  "action": "attach_success",
  "pod_name": "openhands-runtime-session-123",
  "namespace": "default",
  "replica_id": "12345-67890",
  "timestamp": 1703123456.789
}
```

#### Log Messages

- `Successfully attached to existing pod`: Replica attached to existing pod
- `Acquired lock for pod creation`: Replica acquired creation lock
- `Another replica is creating pod`: Waiting for another replica's creation
- `Redis coordination failed, falling back`: Using legacy behavior

## Troubleshooting

### Common Issues

#### Redis Unavailable
```
WARNING: Redis coordinator not available
WARNING: Redis coordination failed, falling back to legacy pod creation
```

**Solution**: Check Redis connectivity and credentials

#### Lock Timeout
```
WARNING: Could not acquire lock for k8s-pod:default:openhands-runtime-session-123
```

**Solution**: Increase `redis_coordination_timeout` or check for stuck locks

#### Stale States
```
INFO: Cleaning up Redis state for non-existent pod
```

**Solution**: Automatic cleanup handles this, but check for orphaned pods

### Debug Commands

#### Check Redis States
```bash
# Connect to Redis and check coordination states
redis-cli
> KEYS openhands:resource:k8s-pod:*
> GET openhands:resource:k8s-pod:default:openhands-runtime-session-123
```

#### Check Pod Status
```bash
# List OpenHands runtime pods
kubectl get pods -l app=openhands-runtime

# Check specific pod details
kubectl describe pod openhands-runtime-session-123
```

#### View Coordination Status
Check application logs for coordination metrics and status updates.

### Performance Considerations

#### Redis Performance
- Use Redis cluster for high availability
- Configure appropriate timeouts and retries
- Monitor Redis memory usage and connection counts

#### Lock Efficiency
- Default lock timeout (30s) balances responsiveness and safety
- Reduce retry attempts if experiencing high contention
- Monitor lock acquisition times

## Advanced Configuration

### Custom Redis Setup

For production deployments, consider:

```yaml
# Redis with persistence and clustering
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis-cluster
spec:
  serviceName: redis-cluster
  replicas: 3
  selector:
    matchLabels:
      app: redis-cluster
  template:
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        command:
        - redis-server
        - --cluster-enabled
        - --cluster-require-full-coverage
        - --cluster-node-timeout
        - "5000"
        - --cluster-config-file
        - /data/nodes.conf
        - --cluster-announce-ip
        - $(POD_IP)
```

### Monitoring and Alerting

Set up monitoring for:
- Redis availability and performance
- Lock acquisition times and failures
- Pod creation success rates
- Coordination state consistency

Example Prometheus metrics:
```
openhands_coordination_lock_acquisitions_total
openhands_coordination_pod_creations_total
openhands_coordination_attach_successes_total
openhands_redis_coordination_enabled
```

## Migration Guide

### From Single Replica

1. **Deploy Redis**: Add Redis to your infrastructure
2. **Update configuration**: Enable coordination in config
3. **Scale gradually**: Increase replicas one at a time
4. **Monitor behavior**: Watch logs for coordination activity

### Disable Coordination

To temporarily disable coordination:

```toml
[sandbox.kubernetes]
redis_coordination_enabled = false
```

This falls back to the original behavior (not recommended for multi-replica deployments).

## Security Considerations

### Redis Security
- Use Redis AUTH with strong passwords
- Configure Redis to listen only on internal networks
- Use TLS for Redis connections in production
- Implement Redis ACLs for access control

### Kubernetes RBAC
Ensure proper RBAC permissions for pod creation and management:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: openhands-runtime
rules:
- apiGroups: [""]
  resources: ["pods", "services", "persistentvolumeclaims"]
  verbs: ["create", "get", "list", "watch", "delete"]
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["create", "get", "list", "watch", "delete"]
```

## Future Enhancements

Potential improvements for the coordination system:

1. **Automatic failover**: Handle replica failures more gracefully
2. **Load balancing**: Distribute pod creation across healthy replicas
3. **Metrics export**: Export detailed metrics to monitoring systems
4. **Configuration validation**: Validate coordination settings at startup
5. **Redis sentinel support**: Support Redis high-availability configurations

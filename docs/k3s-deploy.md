# Plane MCP Server on k3s — runbook

This is the end-to-end recipe that wires the three pieces together:

- **Image** built by [`Dockerfile.k3s`](../Dockerfile.k3s:1) and pushed by
  [`.github/workflows/build-harbor.yml`](../.github/workflows/build-harbor.yml:1)
  to your Harbor registry.
- **Helm chart** in a separate repo (`plane-mcp-server-chart`) — when
  published, install via `helm install plane-mcp plane-mcp/plane-mcp-server`.
- **k3s** cluster with cert-manager + nginx ingress controller.

## 1. Chart repo location

The chart lives in its own repo (`plane-mcp-server-chart`) so its lifecycle
(versions, CI, README) is independent of the upstream Python package:

```
plane-mcp-server-chart/
├── .github/workflows/helm-lint.yml
├── README.md
├── LICENSE
├── CHANGELOG.md
├── .gitignore
└── plane-mcp-server/         # the chart itself
    ├── Chart.yaml
    ├── values.yaml
    └── templates/            # deployment, service, ingress, pdb, redis, secrets…
```

To consume it locally without publishing to an OCI/HTTP repo:

```bash
# Run from inside the chart repo
helm install plane-mcp ./plane-mcp-server \
  --namespace plane-mcp --create-namespace \
  -f my-values.yaml
```

Once you push the chart repo to GitHub and create a `gh-pages` branch, expose it
as a normal Helm repo with `helm/chart-releaser` (or `chartpress`).

## 2. One-time: configure the registry push

In the Python repo (Settings → Secrets and variables → Actions):

| Kind | Name | Value |
|---|---|---|
| Variable | `HARBOR_REGISTRY` | `harbor.yangx.tech` |
| Variable | `HARBOR_PROJECT` | `plane` |
| Variable | `HARBOR_IMAGE_NAME` | `plane-mcp-server` (optional) |
| Secret | `HARBOR_USERNAME` | robot account login |
| Secret | `HARBOR_PASSWORD` | robot account token |

Push a tag to trigger:

```bash
git tag v0.3.2 && git push origin v0.3.2
# or
gh workflow run build-harbor.yml -f tag=dev-main
```

Verify the image landed:

```bash
curl -sS -u "${HARBOR_USERNAME}:${HARBOR_PASSWORD}" \
  "https://${HARBOR_REGISTRY}/api/v2.0/projects/plane/repositories/plane-mcp-server/artifacts?with_tag=true" | jq .
```

## 3. One-time: cluster prerequisites

k3s ships without nginx ingress or cert-manager. Install them once:

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.2/deploy/static/provider/baremetal/deploy.yaml
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.3/cert-manager.yaml

cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ops@yangx.tech
    privateKeySecretRef: { name: letsencrypt-prod }
    solvers:
      - http01: { ingress: { class: nginx } }
EOF
```

## 4. Deploy

```bash
helm install plane-mcp ./plane-mcp-server \
  --namespace plane-mcp \
  --set image.repository=harbor.yangx.tech/plane/plane-mcp-server \
  --set image.tag=0.3.2 \
  --set auth.mode=header \
  --set auth.secretPlane.apiKey=<PAT> \
  --set auth.secretPlane.workspaceSlug=tuxmai \
  --set auth.planeBaseUrl=https://task.yangx.tech \
  --set ingress.hosts[0].host=mcp.yangx.tech
```

## 5. Verify

```bash
# Pods are ready
kubectl -n plane-mcp get pods -l app.kubernetes.io/name=plane-mcp-server

# In-cluster reachability
kubectl -n plane-mcp port-forward svc/plane-mcp-server 8211:8211 &
curl -sS -H 'Authorization: Bearer <PAT>' \
        -H 'X-Workspace-slug: tuxmai' \
        -X POST http://127.0.0.1:8211/http/api-key/mcp \
        -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
# Expected: 30

# Through ingress (after cert-manager issues a cert)
curl -sS -H 'Authorization: Bearer <PAT>' \
        -H 'X-Workspace-slug: tuxmai' \
        https://mcp.yangx.tech/plane/http/api-key/mcp \
        -X POST -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
```

## 6. Connect an MCP client

```json
{
  "mcpServers": {
    "plane": {
      "command": "npx",
      "args": ["mcp-remote@latest", "https://mcp.yangx.tech/plane/http/api-key/mcp"],
      "headers": {
        "Authorization": "Bearer <PAT>",
        "X-Workspace-slug": "tuxmai"
      }
    }
  }
}
```

## 7. Operations

| Symptom | Check |
|---|---|
| `CrashLoopBackOff` | `kubectl logs` — usually an invalid PAT or unreachable `PLANE_BASE_URL`. |
| `ImagePullBackOff` | Image pull secret missing for a private registry. Add `imagePullSecrets:` to values. |
| `tools/list` returns auth error | PAT expired or workspace slug typo. The PAT is shipped via Secret, not env — check `kubectl get secret -o jsonpath='{.data.api-key}' | base64 -d`. |
| OAuth callback 404 | The ingress rewrite target is wrong, or `PLANE_OAUTH_ALLOWED_REDIRECT_URIS` doesn't include the actual callback. See [`plane_mcp/server.py`](../plane_mcp/server.py:19). |
| Redis-backed pods lose tokens on rolling deploys | Switch to a managed Redis with `auth.secretOAuth.redis.external.enabled=true`. |

## 8. Tear down

```bash
helm uninstall plane-mcp --namespace plane-mcp
kubectl delete ns plane-mcp
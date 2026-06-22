# AWS EKS: deploying Artea

This is the EKS-specific path layered on top of the generic Helm guide
([kubernetes.md](kubernetes.md)). The chart is cloud-agnostic; a stock EKS
cluster only needs three things wired up before `helm install` works the way it
does locally:

1. **EBS CSI driver** so PersistentVolumeClaims actually provision (the Gitea
   store of record, plus the postgres/valkey/cache volumes).
2. **AWS Load Balancer Controller** so the gateway's optional Ingress gets a
   real internet-facing ALB.
3. **ACM certificate + Route 53 record** for TLS and DNS on your hostname.

Everything else — secrets, the bootstrap Job, upgrades, state, client setup —
is identical to [kubernetes.md](kubernetes.md); this guide links there instead
of repeating it.

**End state:** one internet-facing ALB terminates TLS and forwards to the
`artea-gateway` Service. Gitea, Verdaccio, devpi and policy-sync stay
cluster-internal (the architecture rule: the gateway is the single entrypoint).
`artea-gitea-data` lives on an EBS volume and is the only thing you must back
up.

## Prerequisites

Tools on your workstation:

- [`awscli`](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) v2, configured (`aws sts get-caller-identity` works)
- [`eksctl`](https://eksctl.io)
- `kubectl`
- `helm` ≥ 3.14
- `docker` (only to read image digests in step 4; any OCI tool works)

AWS side:

- An account where you can create EKS clusters, IAM roles/policies, and load
  balancers.
- A DNS zone you control. This guide uses Route 53 and the placeholder host
  `registry.example.com`.

Placeholders used below — substitute your own:

| Placeholder | Meaning |
|-------------|---------|
| `111122223333` | your AWS account ID |
| `us-east-1` | your region |
| `artea` | the EKS cluster name |
| `registry.example.com` | the public hostname clients will use |

## 1. Create the cluster (with OIDC + EBS CSI in one shot)

The EBS CSI driver is the #1 EKS gotcha: without it every PVC stays `Pending`
and Gitea/postgres never start. Declaring it as a managed add-on with
`wellKnownPolicies` lets `eksctl` create the cluster, the OIDC provider, and the
driver's IAM role together. Save as `cluster.yaml`:

```yaml
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: artea
  region: us-east-1
  version: "1.31"

iam:
  withOIDC: true

addons:
  - name: aws-ebs-csi-driver
    wellKnownPolicies:
      ebsCSIController: true
  - name: vpc-cni
  - name: coredns
  - name: kube-proxy

managedNodeGroups:
  - name: artea-ng
    instanceType: m5.large        # 2 vCPU / 8 GiB; fits the single-node stack
    desiredCapacity: 2
    minSize: 2
    maxSize: 3
    volumeSize: 50
```

```sh
eksctl create cluster -f cluster.yaml          # ~15–20 min
kubectl get nodes                              # 2 Ready
```

The whole single-node stack (Gitea + postgres + valkey + Verdaccio + devpi +
2× gateway + policy-sync + the bootstrap Job) fits comfortably on two
`m5.large`. `eksctl` tags the public subnets with
`kubernetes.io/role/elb=1`, which is what an internet-facing ALB needs for
subnet auto-discovery in step 6.

### A default StorageClass (required on EKS ≥ 1.30)

Don't skip this. Since **Kubernetes 1.30, EKS no longer marks any StorageClass
as the cluster default** — newly created clusters still get a `gp2` class, but
without the `is-default-class` annotation
([AWS 1.30 release notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-extended.html#kubernetes-1-30)).
The Artea chart and its Gitea/postgres/valkey/Verdaccio/devpi subcharts leave
`storageClass` empty and rely on a cluster default, so with none set **every PVC
stays `Pending` and the bootstrap Job never completes.**

Create a `gp3` default class (cheaper and faster than `gp2`). Save as
`gp3-storageclass.yaml`:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
parameters:
  type: gp3
```

```sh
kubectl apply -f gp3-storageclass.yaml
kubectl get storageclass                       # gp3 (default)
```

The chart's empty `storageClass: ""` now binds to `gp3`; no per-PVC overrides
are needed. EBS volumes are zonal (RWO, single-AZ); `WaitForFirstConsumer`
creates each volume in its pod's AZ and keeps reschedules there — exactly right
for the single-replica stateful pods.

(Alternatively, let the EBS CSI **add-on** create the default class for you by
adding `configurationValues: '{"defaultStorageClass":{"enabled":true}}'` to its
entry in `cluster.yaml` — requires driver ≥ v1.31.0, which the 1.31 add-on
ships. The explicit manifest above is preferred here so the class name and
parameters are pinned in your repo.)

## 2. Install the AWS Load Balancer Controller

This controller turns the chart's `gateway.ingress` into a real ALB. Install it
once per cluster.

```sh
# IAM policy the controller needs (pin a released version of the policy doc)
curl -fsSL -o alb-iam-policy.json \
  https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.9.2/docs/install/iam_policy.json

aws iam create-policy \
  --policy-name AWSLoadBalancerControllerIAMPolicy \
  --policy-document file://alb-iam-policy.json

# IRSA service account bound to that policy
eksctl create iamserviceaccount \
  --cluster artea --region us-east-1 \
  --namespace kube-system --name aws-load-balancer-controller \
  --role-name AmazonEKSLoadBalancerControllerRole \
  --attach-policy-arn arn:aws:iam::111122223333:policy/AWSLoadBalancerControllerIAMPolicy \
  --approve

helm repo add eks https://aws.github.io/eks-charts && helm repo update
helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName=artea \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller

kubectl -n kube-system rollout status deploy/aws-load-balancer-controller
```

(If `create-policy` errors because the policy already exists, reuse the existing
ARN — it's account-wide.)

## 3. Request a TLS certificate (ACM)

The ALB terminates TLS using an ACM certificate, so Artea runs with
`gateway.ingress.externalTLS: true` (TLS ends before the Ingress) and an
`https://` base URL.

```sh
aws acm request-certificate \
  --domain-name registry.example.com \
  --validation-method DNS \
  --region us-east-1                 # must be the cluster's region for an ALB
```

Validate it (add the CNAME ACM gives you; if the zone is in Route 53,
`aws acm describe-certificate` shows the record, or use the console's "Create
records in Route 53" button). Wait until status is `ISSUED`, then note the
certificate ARN:

```sh
aws acm list-certificates --region us-east-1 \
  --query "CertificateSummaryList[?DomainName=='registry.example.com'].CertificateArn" --output text
```

## 4. Pin the Artea image digests

The four Artea images are **public** on GHCR, so no ECR mirroring is required —
EKS nodes can pull them directly. The chart requires production installs to pin
them by digest (`requireDigest: true`). Read the current digests for the tag you
want to deploy:

```sh
for img in devpi policy-sync bootstrap verdaccio-assets; do
  digest=$(docker buildx imagetools inspect "ghcr.io/yisding/artea-$img:main" \
    --format '{{.Manifest.Digest}}')
  printf '%-18s %s\n' "$img" "$digest"
done
```

Copy each `sha256:…` into the values file in step 6. (`crane digest` or
`skopeo inspect` work too.) To mirror into ECR for an air-gapped or
pull-policy-restricted setup, `docker pull` by digest, retag to
`<acct>.dkr.ecr.<region>.amazonaws.com/...`, push, and point the chart's
`*.image.repository` at ECR — the digest stays the same.

> Quick evaluation only: skip digests by setting `requireDigest: false` on each
> image and deploying the mutable `:main` tag. Don't do this for anything you
> rely on — mutable tags defeat reproducible rollbacks.

## 5. Set real secrets

Every value under `secrets:` defaults to a `change-me-*` dev placeholder and the
chart refuses to render with them. The full secret contract (what each key is,
how `POLICY_SYNC_TOKEN` is minted in-cluster by the bootstrap Job, how
`helm upgrade` preserves it) is in
[kubernetes.md → Secrets](kubernetes.md#secrets) and
[the chart README](../../deploy/helm/artea/README.md#secrets) — read that once.
Generate strong values, e.g.:

```sh
openssl rand -base64 24    # run per secret: adminPassword, dev1Password, devpiRootPassword, webhookSecret
```

For a managed-secrets setup, store these in AWS Secrets Manager and sync them in
with the [External Secrets Operator](https://external-secrets.io/) rather than
keeping them in a values file. Bring-your-own-Secret support in the chart itself
is not implemented yet (tracked in kubernetes.md).

## 6. Install Artea

Put everything EKS-specific into one values file. Save as `values-eks.yaml`:

```yaml
global:
  baseUrl: https://registry.example.com   # must match the hostname clients use
  privateNamespace: artea                  # Gitea org + npm scope for private pkgs

secrets:
  adminPassword: <from step 5>
  dev1Password: <from step 5>
  devpiRootPassword: <from step 5>
  webhookSecret: <from step 5>

gateway:
  ingress:
    enabled: true
    className: alb
    host: registry.example.com
    externalTLS: true                      # ALB terminates TLS; no tls: block
    annotations:
      alb.ingress.kubernetes.io/scheme: internet-facing
      alb.ingress.kubernetes.io/target-type: ip
      alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
      alb.ingress.kubernetes.io/ssl-redirect: '443'
      alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:us-east-1:111122223333:certificate/<cert-id>
      # Unauthenticated 200 endpoint the gateway exposes for exactly this:
      alb.ingress.kubernetes.io/healthcheck-path: /-/artea-gateway/health

# Digests from step 4
devpi:
  image:
    digest: sha256:<devpi-digest>
policySync:
  image:
    digest: sha256:<policy-sync-digest>
bootstrap:
  image:
    digest: sha256:<bootstrap-digest>
verdaccio:
  pluginAssets:
    image:
      digest: sha256:<verdaccio-assets-digest>
```

Install:

```sh
helm dependency update deploy/helm/artea         # pulls the Gitea + Verdaccio subcharts
helm install artea deploy/helm/artea \
  --namespace artea --create-namespace \
  -f values-eks.yaml

kubectl -n artea logs -f job/artea-bootstrap     # idempotent bootstrap hook
```

On a fresh cluster the first install also pulls postgres/valkey images and
provisions EBS volumes, so give the bootstrap Job a few minutes (it waits for
Gitea health and the first successful policy sync;
`bootstrap.activeDeadlineSeconds` defaults to 1200s).

Notes:

- `externalTLS: true` is required because the ALB (not the Ingress object) holds
  the cert; the chart validation enforces `https://` base URL + host + (`tls` or
  `externalTLS`).
- `target-type: ip` routes the ALB straight to gateway pod IPs (VPC CNI gives
  pods real VPC addresses) — no `NodePort` hop.
- The gateway keeps all routing logic (`auth_request`, the PyPI 404-fallback,
  PEP 503 normalization). The ALB does TLS + host routing only; never port any
  of that into ALB annotations.

## 7. Point DNS at the ALB

After the controller provisions the ALB, the Ingress reports its hostname:

```sh
kubectl -n artea get ingress artea-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}{"\n"}'
# e.g. k8s-artea-arteagat-abc123-456.us-east-1.elb.amazonaws.com
```

Create a Route 53 **A / Alias** record for `registry.example.com` targeting that
ALB (alias records resolve to the ALB's changing IPs for free). Console:
*Create record → Alias → Alias to Application/Network Load Balancer → your
region → the ALB above*. CLI users can `aws elbv2 describe-load-balancers` to
get the ALB's `CanonicalHostedZoneId` and `aws route53 change-resource-record-sets`
with an alias target. To automate this, run
[ExternalDNS](https://kubernetes-sigs.github.io/external-dns/) and add a
`external-dns.alpha.kubernetes.io/hostname` annotation instead.

## 8. Verify

```sh
# DNS resolves to the ALB and TLS is the ACM cert
curl -sS https://registry.example.com/-/artea-gateway/health     # -> ok

# clients reach Gitea's package landing through the gateway
open https://registry.example.com
```

Then follow [getting-started.md](getting-started.md) for the first publish, and
[clients-npm.md](clients-npm.md) / [clients-python.md](clients-python.md) for
client config — they're identical to the local Colima setup; only the base URL
changes from `http://localhost:8080` to `https://registry.example.com`. For real users, sign
in as the admin and create accounts manually or via [Okta/OIDC](okta.md); add
package publishers to the `developers` team and have them mint PATs in Gitea.

## State and backups

`artea-gitea-data` (an EBS volume) is the only store of record — back it up.
The Verdaccio and devpi PVCs are disposable pull-through caches; deleting them is
safe (devpi re-seeds fail-closed and Verdaccio re-fetches on demand). See
[kubernetes.md → State](kubernetes.md#state) and
[operations.md](operations.md) for the backup contract.

Snapshot the Gitea volume with EBS snapshots:

```sh
# find the EBS volume backing the PVC
VOL=$(kubectl -n artea get pv \
  "$(kubectl -n artea get pvc artea-gitea-data -o jsonpath='{.spec.volumeName}')" \
  -o jsonpath='{.spec.csi.volumeHandle}')
aws ec2 create-snapshot --volume-id "$VOL" \
  --description "artea-gitea-data $(date +%F)" --region us-east-1
```

For scheduled snapshots, use AWS Backup or the
[CSI VolumeSnapshot](https://docs.aws.amazon.com/eks/latest/userguide/csi-snapshot-controller.html)
controller against the `artea-gitea-data` PVC.

## Upgrades

Identical to the generic guide — see
[kubernetes.md → Upgrades](kubernetes.md#upgrades-and-upstream-bumps-r7). In
short: bump pins, refresh digests (step 4), then
`helm upgrade artea deploy/helm/artea -n artea -f values-eks.yaml`. The
bootstrap Job re-runs as a post-upgrade hook; `helm rollback` works (the Gitea
database PVC is not rolled back).

## Teardown

```sh
helm uninstall artea -n artea

# PVCs (and their EBS volumes) are NOT deleted by uninstall — this DESTROYS the
# Gitea store of record. Snapshot first if you might want it back.
kubectl -n artea delete pvc --all
kubectl delete namespace artea

# delete the Ingress before the cluster so the controller cleans up the ALB
helm uninstall aws-load-balancer-controller -n kube-system
eksctl delete cluster -f cluster.yaml
```

If `eksctl delete cluster` stalls on the VPC, a leftover ALB/security group is
usually the cause — confirm the Ingress (and thus the ALB) was removed first.

## Troubleshooting (EKS-specific)

- **PVCs stuck `Pending` / pods stuck `ContainerCreating`** → the EBS CSI driver
  isn't installed or its IAM role is wrong. `kubectl get pods -n kube-system | grep ebs`
  and `kubectl describe pvc -n artea`. Re-check step 1's `addons` block.
- **Ingress has no `ADDRESS` after a few minutes** → the Load Balancer
  Controller. `kubectl -n kube-system logs deploy/aws-load-balancer-controller`.
  Common causes: missing/incorrect `certificate-arn`, the ACM cert isn't
  `ISSUED` in the cluster's region, or public subnets aren't tagged
  `kubernetes.io/role/elb=1` (eksctl tags them; custom VPCs may not).
- **ALB returns 503 / targets unhealthy** → the target-group health check.
  Confirm `alb.ingress.kubernetes.io/healthcheck-path: /-/artea-gateway/health`
  is set (that path returns `200 ok` without auth; the default `/` path may
  redirect). Check target health in the EC2 console → Target Groups.
- **`https://` works but package URLs point elsewhere** → `global.baseUrl` must
  exactly equal the public hostname. It drives Gitea's `ROOT_URL` and devpi's
  outside-url; a mismatch produces tarball/file URLs that don't resolve back
  through the gateway.
- **PVCs stuck `Pending` with no events / no default class** → EKS ≥ 1.30 ships
  no default StorageClass. `kubectl get sc` should list `gp3 (default)`; if not,
  apply the class from step 1.
- **Bootstrap / policy-sync issues** → same as
  [kubernetes.md → Troubleshooting](kubernetes.md#troubleshooting).

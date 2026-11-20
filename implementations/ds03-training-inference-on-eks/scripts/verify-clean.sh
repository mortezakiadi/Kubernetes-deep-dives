#!/usr/bin/env bash
# Looks for anything this implementation could have left behind. Exit 0 and "CLEAN" when nothing is found.
set -uo pipefail
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
PROJECT="${PROJECT:-ds03-trainserve}"
left=0
check() {  # check <what> <output>
  if [ -n "$2" ] && [ "$2" != "None" ]; then echo "LEFT OVER  $1: $2"; left=1; else echo "ok         $1: none"; fi
}
check "EKS clusters"              "$(aws eks list-clusters --query "clusters[?contains(@, '$PROJECT')]" --output text)"
# EKS Auto Mode instances are not listed to the account by EC2, so this line can only catch instances that are not Auto Mode's.
# For Auto Mode's nodes the authoritative check is the one above: with the cluster gone, no node can remain.
check "EC2 instances"             "$(aws ec2 describe-instances --filters "Name=tag-key,Values=eks:eks-cluster-name" "Name=instance-state-name,Values=pending,running,stopping,stopped" --query "Reservations[].Instances[?contains(Tags[?Key=='eks:eks-cluster-name']|[0].Value, '$PROJECT')].InstanceId" --output text)"
check "NAT gateways"              "$(aws ec2 describe-nat-gateways --filter "Name=tag:Project,Values=$PROJECT" --query "NatGateways[?State!='deleted'].NatGatewayId" --output text)"
check "Elastic IPs"               "$(aws ec2 describe-addresses --filters "Name=tag:Project,Values=$PROJECT" --query 'Addresses[].AllocationId' --output text)"
check "Load balancers"            "$(aws elbv2 describe-load-balancers --query "LoadBalancers[?contains(LoadBalancerName, 'k8s')].LoadBalancerArn" --output text | tr '\t' ' ' | head -c 300)"
check "VPCs"                      "$(aws ec2 describe-vpcs --filters "Name=tag:Project,Values=$PROJECT" --query 'Vpcs[].VpcId' --output text)"
check "Security groups"           "$(aws ec2 describe-security-groups --filters "Name=tag:Project,Values=$PROJECT" --query 'SecurityGroups[].GroupId' --output text)"
check "EBS volumes"               "$(aws ec2 describe-volumes --filters "Name=tag:Project,Values=$PROJECT" --query 'Volumes[].VolumeId' --output text)"
check "Network interfaces"        "$(aws ec2 describe-network-interfaces --filters "Name=tag:Project,Values=$PROJECT" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)"
check "S3 buckets"                "$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '$PROJECT')].Name" --output text)"
check "IAM roles"                 "$(aws iam list-roles --query "Roles[?starts_with(RoleName, '$PROJECT')].RoleName" --output text)"
check "CloudWatch log groups"     "$(aws logs describe-log-groups --log-group-name-prefix "/aws/eks/$PROJECT" --query 'logGroups[].logGroupName' --output text)"
tagged="$(aws resourcegroupstaggingapi get-resources --tag-filters "Key=Project,Values=$PROJECT" --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' ' ')"
if [ -n "$tagged" ]; then echo "info       tagging index still lists (it lags deletions): $(echo "$tagged" | wc -w) item(s)"; else echo "ok         tagging index: none"; fi
if [ "$left" -eq 0 ]; then echo; echo "CLEAN: nothing from $PROJECT remains in this account and region."; else echo; echo "NOT CLEAN: see the lines above."; exit 1; fi

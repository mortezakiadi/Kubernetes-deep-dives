#!/usr/bin/env bash
# Looks for anything this implementation could have left behind in the account. Exit 0 and "CLEAN" when nothing is found.
set -uo pipefail
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
PROJECT="${PROJECT:-ds01-sandboxing}"
left=0
check() {  # check <what> <output>
  if [ -n "$2" ] && [ "$2" != "None" ]; then echo "LEFT OVER  $1: $2"; left=1; else echo "ok         $1: none"; fi
}

check "EKS clusters"              "$(aws eks list-clusters --query "clusters[?contains(@, '$PROJECT')]" --output text)"
check "EC2 instances (not terminated)" "$(aws ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId' --output text)"
check "EC2 instances created by Auto Mode" "$(aws ec2 describe-instances --filters "Name=tag-key,Values=eks:eks-cluster-name" "Name=tag:eks:eks-cluster-name,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId' --output text)"
check "NAT gateways"              "$(aws ec2 describe-nat-gateways --filter "Name=tag:Project,Values=$PROJECT" --query "NatGateways[?State!='deleted'].NatGatewayId" --output text)"
check "Elastic IPs"               "$(aws ec2 describe-addresses --filters "Name=tag:Project,Values=$PROJECT" --query 'Addresses[].AllocationId' --output text)"
check "VPCs"                      "$(aws ec2 describe-vpcs --filters "Name=tag:Project,Values=$PROJECT" --query 'Vpcs[].VpcId' --output text)"
check "Security groups"           "$(aws ec2 describe-security-groups --filters "Name=tag:Project,Values=$PROJECT" --query 'SecurityGroups[].GroupId' --output text)"
check "EBS volumes"               "$(aws ec2 describe-volumes --filters "Name=tag:Project,Values=$PROJECT" --query 'Volumes[].VolumeId' --output text)"
check "Network interfaces"        "$(aws ec2 describe-network-interfaces --filters "Name=tag:Project,Values=$PROJECT" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)"
check "ECR repositories"          "$(aws ecr describe-repositories --query "repositories[?contains(repositoryName, 'ds01-platform')].repositoryName" --output text 2>/dev/null)"
check "IAM roles"                 "$(aws iam list-roles --query "Roles[?starts_with(RoleName, '$PROJECT')].RoleName" --output text)"
check "CloudWatch log groups"     "$(aws logs describe-log-groups --log-group-name-prefix "/aws/eks/$PROJECT" --query 'logGroups[].logGroupName' --output text)"
check "Anything tagged Project=$PROJECT" "$(aws resourcegroupstaggingapi get-resources --tag-filters "Key=Project,Values=$PROJECT" --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' ' ')"

if [ "$left" -eq 0 ]; then echo; echo "CLEAN: nothing from $PROJECT remains in this account and region."; else echo; echo "NOT CLEAN: see the lines above."; exit 1; fi

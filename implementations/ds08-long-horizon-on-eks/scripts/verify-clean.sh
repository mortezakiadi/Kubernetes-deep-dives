#!/usr/bin/env bash
# Looks for anything this implementation could have left behind. Exit 0 and "CLEAN" when nothing is found.
set -uo pipefail
export AWS_REGION=us-east-2 AWS_DEFAULT_REGION=us-east-2 AWS_PAGER=""
PROJECT="${PROJECT:-ds08-longhorizon}"
left=0
check() {  # check <what> <output>
  if [ -n "$2" ] && [ "$2" != "None" ]; then echo "LEFT OVER  $1: $2"; left=1; else echo "ok         $1: none"; fi
}
check "EKS clusters"              "$(aws eks list-clusters --query "clusters[?starts_with(@, 'ds08-')]" --output text)"
# Managed node group instances are ordinary EC2 instances, tagged by EKS with the cluster's name.
check "EC2 instances"             "$(aws ec2 describe-instances --filters "Name=tag:eks:cluster-name,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down" --query 'Reservations[].Instances[].InstanceId' --output text)"
check "EC2 instances (Project)"   "$(aws ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId' --output text)"
check "Auto Scaling groups"       "$(aws autoscaling describe-auto-scaling-groups --query "AutoScalingGroups[?Tags[?Key=='eks:cluster-name' && Value=='$PROJECT']].AutoScalingGroupName" --output text)"
check "Launch templates"          "$(aws ec2 describe-launch-templates --filters "Name=tag:eks:cluster-name,Values=$PROJECT" --query 'LaunchTemplates[].LaunchTemplateName' --output text)"
check "NAT gateways"              "$(aws ec2 describe-nat-gateways --filter "Name=tag:Project,Values=$PROJECT" --query "NatGateways[?State!='deleted'].NatGatewayId" --output text)"
check "Elastic IPs"               "$(aws ec2 describe-addresses --filters "Name=tag:Project,Values=$PROJECT" --query 'Addresses[].AllocationId' --output text)"
check "VPCs"                      "$(aws ec2 describe-vpcs --filters "Name=tag:Project,Values=$PROJECT" --query 'Vpcs[].VpcId' --output text)"
check "VPC endpoints"             "$(aws ec2 describe-vpc-endpoints --filters "Name=tag:Project,Values=$PROJECT" --query "VpcEndpoints[?State!='deleted'].VpcEndpointId" --output text)"
check "Security groups"           "$(aws ec2 describe-security-groups --filters "Name=tag:aws:eks:cluster-name,Values=$PROJECT" --query 'SecurityGroups[].GroupId' --output text)"
check "EBS volumes"               "$(aws ec2 describe-volumes --filters "Name=tag:eks:cluster-name,Values=$PROJECT" --query 'Volumes[].VolumeId' --output text)"
check "CSI volumes"               "$(aws ec2 describe-volumes --filters "Name=tag-key,Values=ebs.csi.aws.com/cluster" --query 'Volumes[].VolumeId' --output text)"
check "CSI snapshots"             "$(aws ec2 describe-snapshots --owner-ids self --filters "Name=tag-key,Values=ebs.csi.aws.com/cluster" --query 'Snapshots[].SnapshotId' --output text)"
check "Network interfaces"        "$(aws ec2 describe-network-interfaces --filters "Name=tag:cluster.k8s.amazonaws.com/name,Values=$PROJECT" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)"
check "S3 buckets"                "$(aws s3api list-buckets --query "Buckets[?starts_with(Name, 'ds08-')].Name" --output text)"
check "IAM roles"                 "$(aws iam list-roles --query "Roles[?starts_with(RoleName, 'ds08-')].RoleName" --output text)"
check "CloudWatch log groups"     "$(aws logs describe-log-groups --query "logGroups[?contains(logGroupName, 'ds08-')].logGroupName" --output text)"
tagged="$(aws resourcegroupstaggingapi get-resources --tag-filters "Key=Project,Values=$PROJECT" --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' ' ')"
if [ -n "$tagged" ]; then echo "info       tagging index still lists (it lags deletions): $(echo "$tagged" | wc -w | tr -d ' ') item(s)"; else echo "ok         tagging index: none"; fi
if [ "$left" -eq 0 ]; then echo; echo "CLEAN: nothing from $PROJECT remains in this account and region."; else echo; echo "NOT CLEAN: see the lines above."; exit 1; fi

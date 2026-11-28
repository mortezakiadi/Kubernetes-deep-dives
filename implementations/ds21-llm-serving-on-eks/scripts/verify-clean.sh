#!/usr/bin/env bash
# Looks for anything of this project in the region. Prints CLEAN only when nothing is left.
set -uo pipefail
export AWS_PAGER=""
PROJECT="ds21-llm-serving"
R="${REGION:-us-east-1}"
left=0
check() { # name, value
  if [ -n "$2" ] && [ "$2" != "None" ]; then echo "LEFT OVER  $1: $2"; left=1; else echo "ok         $1: none"; fi
}
echo "--- $R"
check "EKS clusters"        "$(aws eks list-clusters --region $R --query "clusters[?contains(@, '$PROJECT')]" --output text)"
check "EC2 instances"       "$(aws ec2 describe-instances --region $R --filters "Name=tag:Project,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId' --output text)"
check "NAT gateways"        "$(aws ec2 describe-nat-gateways --region $R --filter "Name=tag:Project,Values=$PROJECT" --query "NatGateways[?State!='deleted'].NatGatewayId" --output text)"
check "Elastic IPs"         "$(aws ec2 describe-addresses --region $R --filters "Name=tag:Project,Values=$PROJECT" --query 'Addresses[].AllocationId' --output text)"
check "VPC endpoints"       "$(aws ec2 describe-vpc-endpoints --region $R --filters "Name=tag:Project,Values=$PROJECT" --query "VpcEndpoints[?State!='deleted'].VpcEndpointId" --output text)"
check "VPCs"                "$(aws ec2 describe-vpcs --region $R --filters "Name=tag:Project,Values=$PROJECT" --query 'Vpcs[].VpcId' --output text)"
check "Security groups"     "$(aws ec2 describe-security-groups --region $R --filters "Name=tag:Project,Values=$PROJECT" --query 'SecurityGroups[].GroupId' --output text)"
check "Load balancers"      "$(aws elbv2 describe-load-balancers --region $R --query "LoadBalancers[?contains(LoadBalancerName, 'k8s-')].LoadBalancerArn" --output text 2>/dev/null)"
check "Network interfaces"  "$(aws ec2 describe-network-interfaces --region $R --filters "Name=tag:Project,Values=$PROJECT" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)"
check "EBS volumes"         "$(aws ec2 describe-volumes --region $R --filters "Name=tag:Project,Values=$PROJECT" --query 'Volumes[].VolumeId' --output text)"
check "Launch templates"    "$(aws ec2 describe-launch-templates --region $R --query "LaunchTemplates[?starts_with(LaunchTemplateName, '$PROJECT')].LaunchTemplateName" --output text)"
check "CloudWatch log groups" "$(aws logs describe-log-groups --region $R --log-group-name-prefix /aws/eks/$PROJECT --query 'logGroups[].logGroupName' --output text)"
echo "--- global"
check "S3 buckets"          "$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '$PROJECT')].Name" --output text)"
check "IAM roles"           "$(aws iam list-roles --query "Roles[?starts_with(RoleName, '$PROJECT')].RoleName" --output text)"
if [ "$left" -eq 0 ]; then echo; echo "CLEAN: nothing from $PROJECT remains in this account."; else echo; echo "NOT CLEAN: see the lines above."; exit 1; fi

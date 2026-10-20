#!/usr/bin/env bash
# tests/fresh-ec2.sh [--keep] [--type c5.metal] [--image 26.04] [--subnet S]: what
# tests/fresh-machine.sh does, on a throwaway EC2 bare-metal instance instead
# of a Multipass VM (fcvm needs /dev/kvm, which on AWS only *.metal instances
# have):
#
#   1. launch an Ubuntu instance with its own key pair and security group
#      (SSH from this machine's address only), terminating itself after
#      2 hours whatever happens. It goes in the default VPC, or, without
#      one, in a public subnet (one that assigns public addresses and routes
#      to an internet gateway) of any VPC that offers the instance type;
#      --subnet picks one
#   2. install this tree with install.sh --source, as a release would be
#   3. fcvm setup -y, then again after re-login, which boots a first VM
#   4. tests/run lint unit integration, from the installed copy
#   5. a real reboot: a VM with --restart unless-stopped must come back, and
#      must have been stopped cleanly at shutdown
#   6. terminate the instance and delete the key pair and security group
#      (unless --keep)
#
# Needs the aws CLI configured for an account that may run the instance type
# (c5.metal: 96 vCPUs, about $4 an hour; a run takes 30-45 minutes, most of
# it the instance's boot and the kernel build). Logs go to the directory
# printed at the start.
set -euo pipefail
cd "$(dirname "$0")/.."

keep=0 type=c5.metal image=26.04 subnet=""
while [ $# -gt 0 ]; do
    case $1 in
        --keep)   keep=1; shift ;;
        --type)   type=$2; shift 2 ;;
        --image)  image=$2; shift 2 ;;
        --subnet) subnet=$2; shift 2 ;;
        *)        echo "usage: tests/fresh-ec2.sh [--keep] [--type c5.metal] [--image 26.04] [--subnet SUBNET]" >&2; exit 2 ;;
    esac
done
command -v aws >/dev/null || { echo "needs the aws CLI" >&2; exit 1; }

run=fcvm-fresh-$(date +%Y%m%d-%H%M%S)
log=${FRESH_LOG_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/$run.XXXX")}
key=$log/key.pem
step() { printf '\n\e[1;34m== %s\e[0m\n' "$*"; }
instance="" sg=""
cleanup() {
    if [ $keep = 1 ] && [ -n "$instance" ]; then
        echo "kept: ssh -i $key ubuntu@$ip   (terminate: aws ec2 terminate-instances --instance-ids $instance)"
        return
    fi
    aws ec2 delete-key-pair --key-name "$run" >/dev/null 2>&1 || true
    if [ -n "$instance" ]; then
        aws ec2 terminate-instances --instance-ids "$instance" >/dev/null 2>&1 || true
        # a metal instance can take over ten minutes to terminate, longer than `aws ec2 wait` allows
        for _ in $(seq 1 60); do
            [ "$(aws ec2 describe-instances --instance-ids "$instance" --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)" = terminated ] && break
            sleep 20
        done
    fi
    [ -z "$sg" ] || aws ec2 delete-security-group --group-id "$sg" >/dev/null 2>&1 ||
        echo "could not delete security group $sg; delete it by hand" >&2
}
trap cleanup EXIT

step "preparing ($run; logs in $log)"
case $image in
    26.04) pattern="ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-amd64-server-*" ;;
    24.04) pattern="ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" ;;
    *) echo "image: 24.04 or 26.04" >&2; exit 2 ;;
esac
ami=$(aws ec2 describe-images --owners 099720109477 --filters "Name=name,Values=$pattern" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
[ "$ami" != None ] || { echo "no Ubuntu $image AMI found" >&2; exit 1; }
zones=" $(aws ec2 describe-instance-type-offerings --location-type availability-zone --filters "Name=instance-type,Values=$type" \
    --query 'InstanceTypeOfferings[].Location' --output text | tr '\t' ' ') "
if [ -z "$subnet" ]; then
    vpc=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
    [ "$vpc" != None ] || vpc=""
    # public subnets (a public address on launch) in a zone that offers the type
    while read -r id zone v; do
        [[ $zones == *" $zone "* ]] || continue
        [ -z "$vpc" ] || [ "$v" = "$vpc" ] || continue
        # ...with a route to an internet gateway (its own table, or the VPC's main one)
        igw=$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$id" \
            --query 'RouteTables[].Routes[?starts_with(GatewayId, `igw-`)].GatewayId | [0]' --output text)
        [ -n "$igw" ] && [ "$igw" != None ] || igw=$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$v" "Name=association.main,Values=true" \
            --query 'RouteTables[].Routes[?starts_with(GatewayId, `igw-`)].GatewayId | [0]' --output text)
        [ -n "$igw" ] && [ "$igw" != None ] || continue
        subnet=$id; break
    done < <(aws ec2 describe-subnets --filters Name=map-public-ip-on-launch,Values=true \
        --query 'Subnets[].[SubnetId,AvailabilityZone,VpcId]' --output text)
    [ -n "$subnet" ] || { echo "no public subnet in a zone offering $type (give one with --subnet)" >&2; exit 1; }
fi
vpc=$(aws ec2 describe-subnets --subnet-ids "$subnet" --query 'Subnets[0].VpcId' --output text)
me=$(curl -fsS https://checkip.amazonaws.com)
aws ec2 create-key-pair --key-name "$run" --query KeyMaterial --output text > "$key"
chmod 600 "$key"
sg=$(aws ec2 create-security-group --group-name "$run" --description "fcvm fresh-machine test, SSH from $me" --vpc-id "$vpc" \
    --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$sg" --protocol tcp --port 22 --cidr "$me/32" >/dev/null
echo "ami $ami, $type in $subnet, ssh from $me"

step "launching $type (self-terminates in 2 h)"
# shellcheck disable=SC2016
instance=$(aws ec2 run-instances --image-id "$ami" --instance-type "$type" --key-name "$run" --security-group-ids "$sg" \
    --subnet-id "$subnet" --associate-public-ip-address --instance-initiated-shutdown-behavior terminate \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":40,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$run},{Key=purpose,Value=fcvm-fresh-machine-test}]" \
    --user-data '#!/bin/sh
shutdown -h +120' \
    --query 'Instances[0].InstanceId' --output text)
echo "instance $instance"
aws ec2 wait instance-running --instance-ids "$instance"
ip=$(aws ec2 describe-instances --instance-ids "$instance" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "running at $ip; waiting for SSH (a metal instance takes several minutes to boot)"
ssh_opts=(-i "$key" -o StrictHostKeyChecking=no -o UserKnownHostsFile="$log/known_hosts" -o ConnectTimeout=10 -o ServerAliveInterval=30)
in_vm() { ssh "${ssh_opts[@]}" "ubuntu@$ip" -- bash -lc "$(printf '%q' "$1")"; }   # a login shell: PATH, groups as a user gets them
for i in $(seq 1 120); do ssh "${ssh_opts[@]}" "ubuntu@$ip" true 2>/dev/null && break; sleep 10; [ "$i" -lt 120 ] || { echo "no SSH after 20 minutes" >&2; exit 1; }; done
in_vm 'cloud-init status --wait >/dev/null 2>&1 || true; echo "$(nproc) CPUs, $(free -g | awk "/Mem/ {print \$2}") GB, $(lsb_release -ds)"; ls -l /dev/kvm'

step "installing this tree ($(git describe --always --dirty 2>/dev/null || echo 'no git'))"
mkdir "$log/fcvm"
git ls-files -co --exclude-standard | tar -cf - -T - | tar -xf - -C "$log/fcvm"
tar -czf "$log/fcvm.tar.gz" -C "$log" fcvm
rm -rf "$log/fcvm"
scp "${ssh_opts[@]}" -q "$log/fcvm.tar.gz" "ubuntu@$ip:/tmp/fcvm.tar.gz"
in_vm 'tar -xzf /tmp/fcvm.tar.gz -C /tmp && sh /tmp/fcvm/install.sh --source /tmp/fcvm'

step "fcvm setup -y (first run: packages, jailer, service, kernel)"
in_vm 'fcvm setup -y' 2>&1 | tee "$log/setup1.log"
step "fcvm setup -y (after re-login: boots a first VM)"
in_vm 'fcvm setup -y' 2>&1 | tee "$log/setup2.log"
grep -q 'a VM booted Linux' "$log/setup2.log" || { echo "the test VM didn't boot" >&2; exit 1; }

step "tests/run lint unit integration (from the installed copy)"
in_vm 'cd ~/.local/lib/fcvm/current && tests/run lint unit integration -v' 2>&1 | tee "$log/tests.log"

step "reboot: a VM with --restart unless-stopped comes back"
in_vm 'fcvm create fresh-web alpine-latest --idle --restart unless-stopped && fcvm start fresh-web'
in_vm 'systemctl cat fcvm | grep -E "ExecStop|KillMode|TimeoutStop"; date -u' | tee "$log/reboot.log"
# from inside, as a host is rebooted: `aws ec2 reboot-instances` hard-resets an
# instance that hasn't shut down within four minutes, which would hide the result
in_vm 'sudo systemctl reboot' || true
sleep 45
for _ in $(seq 1 90); do in_vm 'fcvm exec fresh-web -- true' 2>/dev/null && break; sleep 10; done
in_vm 'fcvm exec fresh-web -- echo "fresh-web is back"'
# A clean stop at shutdown leaves an exit record with Firecracker's status 0
# and not stale; a VM torn down with the host leaves a stale one.
in_vm 'date -u; fcvm inspect fresh-web | jq -c .last_exit; journalctl --list-boots -q | tail -3; echo "== fcvm unit, previous boot:"; journalctl -b -1 -u fcvm -o short-iso 2>/dev/null | tail -15; echo "== shutdown, previous boot:"; journalctl -b -1 -o short-iso 2>/dev/null | grep -iE "stopp|shutdown|reboot|fcvm|firecracker" | tail -40' | tee -a "$log/reboot.log"
in_vm 'fcvm inspect fresh-web | jq -e ".last_exit.stale == false and .last_exit.fc_status == 0" >/dev/null' ||
    { echo "the VM wasn't stopped cleanly at shutdown (see $log/reboot.log)" >&2; exit 1; }

step "all good on a fresh Ubuntu $image ($type)"

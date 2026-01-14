# Wendy Sites Setup Guide

Service is deployed to Orange Pi. Complete these steps to finish setup:

## 1. Terraform - Create Route 53 Hosted Zone

```bash
cd infra/terraform
terraform plan
terraform apply
```

Save the `wendy_monster_nameservers` output - you'll need these for step 4.

## 2. Update Caddy on Lightsail

```bash
ssh -i ~/.ssh/lightsail-west-2.pem ec2-user@44.255.209.109
sudo vi /etc/caddy/Caddyfile
```

Add this block:
```
wendy.monster, www.wendy.monster {
    reverse_proxy 100.120.250.100:8910
}
```

Then reload:
```bash
sudo systemctl reload caddy
```

## 3. Update hollingsbot

Add to `~/hollingsbot3/.env`:
```
WENDY_DEPLOY_TOKEN=4e583dc13976470aee8febbfc1f0524c08e9125e4245ea0ee493e771e88d7073
```

Rebuild/restart the wendy_proxy container to pick up the new env var.

## 4. Update Domain NS Records

At your domain registrar for wendy.monster, set the nameservers to the values from terraform output (step 1).

## Verification

Once DNS propagates:
```bash
curl https://wendy.monster/test/
```

Should return: `<html><body><h1>Hello from Wendy!</h1></body></html>`

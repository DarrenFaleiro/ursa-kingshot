# Deploying to Azure

The bot runs on one small Linux VM with Docker. Caddy sits in front and gives the website
free HTTPS. Do these one at a time - each step tells you what you should see.

## 0. Before you start: costs

- A **new Azure free account** includes 750 hours/month of a B1s (or B2ats v2) VM for 12 months,
  which covers one VM running all month.
- Small extras (disk, public IP) may not all be covered. **Set a budget alert first** (step 1).
- After 12 months a B1s costs roughly $8-9/month in UK South.

## 1. Install the Azure CLI and log in (on your Mac)

```bash
brew install azure-cli
az login
```

Set a budget alert (emails you when spend passes £5):

1. Azure portal -> **Cost Management** -> **Budgets** -> **Add**
2. Amount: 5, alert at 80% and 100%, your email.

## 2. Create a resource group

```bash
az group create --name kingshot-rg --location uksouth
```

## 3. Create the VM

Pick a unique label - it becomes your web address: `<label>.uksouth.cloudapp.azure.com`

```bash
az vm create \
  --resource-group kingshot-rg \
  --name kingshot-vm \
  --image Ubuntu2404 \
  --size Standard_B1s \
  --admin-username azureuser \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --public-ip-address-dns-name <label>
```

The output shows `"fqdn": "<label>.uksouth.cloudapp.azure.com"` - that's your domain.

## 4. Open the web ports

```bash
az vm open-port --resource-group kingshot-rg --name kingshot-vm --port 80,443 --priority 1010
```

## 5. Connect to the VM

```bash
ssh azureuser@<label>.uksouth.cloudapp.azure.com
```

## 6. Install Docker (on the VM)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker azureuser
exit
```

Log back in with the `ssh` command from step 5 so the group change applies. Check:

```bash
docker --version
```

## 7. Add swap (the B1s only has 1 GB of memory)

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 8. Copy the bot to the VM (run this on your Mac, in the folder that contains kingshot-bot)

```bash
scp -r kingshot-bot azureuser@<label>.uksouth.cloudapp.azure.com:~/
```

## 9. Set the Azure values in .env (on the VM)

```bash
cd ~/kingshot-bot
nano .env
```

Change these two lines:

```
PUBLIC_URL=https://<label>.uksouth.cloudapp.azure.com
DOMAIN=<label>.uksouth.cloudapp.azure.com
```

## 10. Add the Azure address to Discord login

Developer Portal -> your app -> **OAuth2** -> **Redirects** -> **Add Redirect**:

```
https://<label>.uksouth.cloudapp.azure.com/auth/callback
```

Save changes.

## 11. Start it (on the VM)

```bash
docker compose --profile https up -d --build
docker compose logs -f
```

Open `https://<label>.uksouth.cloudapp.azure.com` - the first visit can take ~30 seconds
while Caddy gets the certificate.

## Updating later

Copy the new files up (step 8), then on the VM:

```bash
cd ~/kingshot-bot
docker compose --profile https up -d --build
```

Your data in `data/` and your `.env` are kept.

## Stopping everything (so it costs nothing)

```bash
az group delete --name kingshot-rg
```

This deletes the VM, disk and IP. Download `data/kingshot.db` first if you want to keep it.
